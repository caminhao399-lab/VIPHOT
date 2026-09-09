import asyncio
import hashlib
import hmac
import io
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import qrcode
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import BotCommand, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("viphot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GGPIX_API_KEY = os.getenv("GGPIX_API_KEY", "").strip()
GGPIX_BASE_URL = os.getenv("GGPIX_BASE_URL", "https://ggpixapi.com/api/v1").strip().rstrip("/")
GGPIX_WEBHOOK_SECRET = os.getenv("GGPIX_WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
VIDEO_FILE_ID = os.getenv("VIDEO_FILE_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "/tmp/viphot.sqlite3").strip()

PLANS = {
    "essential": {"name": "VIP Essencial", "amount_cents": 1290},
    "premium": {"name": "VIP Premium", "amount_cents": 1890},
    "acervo": {"name": "VIP Premium + Acervo", "amount_cents": 2090},
    "full": {"name": "Acesso Full + Bônus", "amount_cents": 2990},
}

payment_tasks: dict[str, asyncio.Task] = {}
reminder_tasks: dict[int, asyncio.Task] = {}
telegram_app: Application | None = None


def money(cents: int) -> str:
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS reminder_state (telegram_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, stopped INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    return conn


def reminder_start(user_id: int) -> None:
    with closing(db()) as conn:
        conn.execute("INSERT OR REPLACE INTO reminder_state(telegram_id, started_at, stopped) VALUES(?,?,0)", (user_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()


def reminder_stop(user_id: int) -> None:
    with closing(db()) as conn:
        conn.execute("UPDATE reminder_state SET stopped=1 WHERE telegram_id=?", (user_id,))
        conn.commit()
    task = reminder_tasks.pop(user_id, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


def reminder_is_stopped(user_id: int) -> bool:
    with closing(db()) as conn:
        row = conn.execute("SELECT stopped FROM reminder_state WHERE telegram_id=?", (user_id,)).fetchone()
    return bool(row and row["stopped"])


def reminder_elapsed_start(user_id: int) -> datetime | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT started_at, stopped FROM reminder_state WHERE telegram_id=?", (user_id,)).fetchone()
    if not row or row["stopped"]:
        return None
    try:
        return datetime.fromisoformat(row["started_at"])
    except ValueError:
        return None


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 VIP Essencial — R$ 12,90", callback_data="plan:essential")],
        [InlineKeyboardButton("🔴 VIP Premium — R$ 18,90", callback_data="plan:premium")],
        [InlineKeyboardButton("⭐ VIP Premium + Acervo — R$ 20,90", callback_data="plan:acervo")],
        [InlineKeyboardButton("👑 Acesso Full + Bônus — R$ 29,90", callback_data="plan:full")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back")],
    ])


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Assinar acesso VIP", callback_data="buy")],
        [InlineKeyboardButton("📅 Meu acesso", callback_data="status")],
    ])


def payment_keyboard(tx_id: str, pix_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copiar Código", copy_text=CopyTextButton(text=pix_code))],
        [InlineKeyboardButton("✅ Verificar Status", callback_data=f"check:{tx_id}")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="back")],
    ])


def promo_text() -> str:
    return ("<b>🔥 VOCÊ ESTÁ A UM CLIQUE DO CONTEÚDO VIP EXCLUSIVO</b> 😈\n\n"
            "🟢 <b>OFERTA ESPECIAL</b>\n\n"
            "🌸 Criadoras adultas\n"
            "⭐ Conteúdo exclusivo\n"
            "🎥 Vídeos e atualizações frequentes\n"
            "💋 Conteúdo sensual para maiores de 18\n"
            "🔥 Conteúdo premium e novidades\n"
            "🔒 Área privada para assinantes\n\n"
            "🎁 <b>BÔNUS APÓS A COMPRA</b>\n"
            "• Novidades exclusivas\n"
            "• Conteúdo premium adicional\n"
            "• Atualizações para assinantes\n\n"
            "⚠️ <b>SERVIÇO EXCLUSIVO PARA MAIORES DE 18 ANOS.</b>\n\n"
            "🚨 <b>APROVEITE A OFERTA ESPECIAL</b>")


def reminder_text() -> str:
    return ("👋 <b>Sua oferta VIP ainda está disponível.</b>\n\n"
            "Você iniciou o acesso, mas ainda não concluiu a assinatura.\n\n"
            "🔥 Aproveite a oferta especial.\n"
            "⭐ Conteúdo exclusivo para adultos\n"
            "🔒 Área privada para assinantes\n"
            "🎁 Bônus e novidades\n\n"
            "⚠️ Exclusivo para maiores de 18 anos.")


async def send_reminder(user_id: int) -> None:
    if telegram_app is None or not VIDEO_FILE_ID or reminder_is_stopped(user_id):
        return
    try:
        await telegram_app.bot.send_video(chat_id=user_id, video=VIDEO_FILE_ID, caption=reminder_text(), reply_markup=menu_keyboard())
    except Exception:
        log.exception("Falha ao enviar lembrete para %s", user_id)


async def reminder_worker(user_id: int) -> None:
    try:
        for delay_minutes in (30, 60, 90, 120):
            started = reminder_elapsed_start(user_id)
            if started is None:
                return
            target = started + timedelta(minutes=delay_minutes)
            await asyncio.sleep(max(0.0, (target - datetime.now(timezone.utc)).total_seconds()))
            if reminder_is_stopped(user_id):
                return
            await send_reminder(user_id)
        reminder_stop(user_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Falha no ciclo de lembretes para %s", user_id)
    finally:
        if reminder_tasks.get(user_id) is asyncio.current_task():
            reminder_tasks.pop(user_id, None)


def start_reminders(user_id: int) -> None:
    if telegram_app is None or not VIDEO_FILE_ID:
        return
    reminder_stop(user_id)
    reminder_start(user_id)
    reminder_tasks[user_id] = asyncio.create_task(reminder_worker(user_id))


async def ggpix_request(method: str, path: str, *, json: Any | None = None) -> tuple[int, Any]:
    if not GGPIX_API_KEY:
        raise RuntimeError("GGPIX_API_KEY não configurada no Render")
    headers = {"Content-Type": "application/json", "X-API-Key": GGPIX_API_KEY}
    timeout = httpx.Timeout(25.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, f"{GGPIX_BASE_URL}{path}", json=json, headers=headers)
        try:
            data = response.json()
        except ValueError:
            data = {"error": response.text[:500]}
        return response.status_code, data


async def create_pix(plan_key: str, chat_id: int, payer_name: str, payer_document: str) -> dict[str, Any]:
    plan = PLANS[plan_key]
    payload: dict[str, Any] = {
        "amountCents": plan["amount_cents"],
        "description": f"{plan['name']} - acesso 18+",
        "payerName": payer_name,
        "payerDocument": payer_document,
        "externalId": f"viphot:{chat_id}:{uuid.uuid4().hex[:16]}",
    }
    if WEBHOOK_URL:
        payload["webhookUrl"] = WEBHOOK_URL.rstrip("/") + "/webhooks/pix"
    status, data = await ggpix_request("POST", "/pix/in", json=payload)
    if status >= 400:
        message = data.get("error") if isinstance(data, dict) else None
        if isinstance(message, dict):
            message = message.get("message")
        raise RuntimeError(str(message or f"GGPIX HTTP {status}"))
    return data


async def get_transaction(tx_id: str) -> dict[str, Any]:
    status, data = await ggpix_request("GET", f"/transactions/{tx_id}")
    if status >= 400:
        message = data.get("error") if isinstance(data, dict) else None
        if isinstance(message, dict):
            message = message.get("message")
        raise RuntimeError(str(message or f"GGPIX HTTP {status}"))
    return data


async def notify_paid(application: Application, chat_id: int, tx: dict[str, Any]) -> None:
    reminder_stop(chat_id)
    amount = int(tx.get("amount", tx.get("amountCents", 0)) or 0)
    await application.bot.send_message(
        chat_id=chat_id,
        text=("✅ <b>PAGAMENTO CONFIRMADO!</b>\n\n"
              f"Valor: {money(amount)}\n\n"
              "Seu pagamento foi confirmado com sucesso.\n"
              "A liberação do acesso é realizada pelo sistema GGPIX."),
        parse_mode="HTML",
    )


async def poll_payment(application: Application, chat_id: int, tx_id: str) -> None:
    try:
        for _ in range(360):
            await asyncio.sleep(10)
            try:
                tx = await get_transaction(tx_id)
            except Exception as exc:
                log.warning("Falha ao consultar GGPIX %s: %s", tx_id, exc)
                continue
            status = str(tx.get("status", "")).upper()
            if status == "COMPLETE":
                await notify_paid(application, chat_id, tx)
                return
            if status in {"FAILED", "CANCELED", "EXPIRED"}:
                await application.bot.send_message(chat_id=chat_id, text="⚠️ O PIX não está mais disponível. Use /assinar para gerar uma nova cobrança.")
                return
    finally:
        payment_tasks.pop(tx_id, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    context.user_data.clear()
    start_reminders(update.effective_chat.id)
    if VIDEO_FILE_ID:
        await update.message.reply_video(video=VIDEO_FILE_ID, caption=promo_text(), reply_markup=menu_keyboard(), parse_mode="HTML")
    else:
        await update.message.reply_text(promo_text(), reply_markup=menu_keyboard(), parse_mode="HTML")


async def assinar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    context.user_data.clear()
    await update.message.reply_text("⭐ <b>Assinar acesso VIP</b>\n\nEscolha seu plano:", reply_markup=plans_keyboard(), parse_mode="HTML")


async def meu_acesso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    await update.message.reply_text("📅 <b>Meu acesso</b>\n\nO acesso é atualizado após a confirmação do pagamento pelo GGPIX.", parse_mode="HTML", reply_markup=menu_keyboard())


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await query.edit_message_text("⭐ <b>Assinar acesso VIP</b>\n\nEscolha seu plano:", reply_markup=plans_keyboard(), parse_mode="HTML")


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await query.edit_message_text("📅 <b>Meu acesso</b>\n\nO acesso é atualizado após a confirmação do pagamento pelo GGPIX.", reply_markup=menu_keyboard(), parse_mode="HTML")


async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("🔞 Área exclusiva para maiores de 18 anos.\n\nEscolha uma opção:", reply_markup=menu_keyboard())


async def choose_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_chat:
        return
    await query.answer()
    _, plan_key = query.data.split(":", 1)
    if plan_key not in PLANS:
        await query.edit_message_text("Plano inválido. Use /assinar novamente.")
        return
    context.user_data.clear()
    context.user_data["plan_key"] = plan_key
    context.user_data["checkout_step"] = "name"
    plan = PLANS[plan_key]
    await query.edit_message_text(
        f"⭐ <b>{plan['name']}</b>\nValor: {money(plan['amount_cents'])}\n\nPara gerar o PIX, informe seu <b>nome completo</b>:",
        parse_mode="HTML",
    )


async def collect_customer_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    step = context.user_data.get("checkout_step")
    text = (update.message.text or "").strip()
    if not step:
        return
    if step == "name":
        if len(text) < 2 or len(text) > 120:
            await update.message.reply_text("❌ Informe um nome válido para continuar.")
            return
        context.user_data["payer_name"] = text
        context.user_data["checkout_step"] = "document"
        await update.message.reply_text("Agora informe seu CPF ou CNPJ, somente números:")
        return
    if step != "document":
        return
    document = "".join(ch for ch in text if ch.isdigit())
    if len(document) not in (11, 14):
        await update.message.reply_text("❌ CPF/CNPJ inválido. Envie somente os números e tente novamente.")
        return
    plan_key = context.user_data.get("plan_key")
    payer_name = context.user_data.get("payer_name")
    if not plan_key or not payer_name:
        context.user_data.clear()
        await update.message.reply_text("Sessão expirada. Use /assinar novamente.")
        return

    context.user_data.clear()
    plan = PLANS[plan_key]
    await update.message.reply_text(f"⏳ Gerando seu PIX...\n\nPlano: {plan['name']}\nValor: {money(plan['amount_cents'])}")
    try:
        tx = await create_pix(plan_key, update.effective_chat.id, payer_name, document)
    except Exception:
        log.exception("Erro ao criar PIX no GGPIX")
        await update.message.reply_text("❌ Não foi possível gerar o PIX agora. Tente novamente em /assinar.")
        return

    tx_id = str(tx.get("id", ""))
    pix_code = str(tx.get("pixCopyPaste") or tx.get("pixCode") or "").strip()
    amount = int(tx.get("amount", plan["amount_cents"]) or plan["amount_cents"])
    if not tx_id or not pix_code:
        log.error("GGPIX não retornou id ou pixCopyPaste")
        await update.message.reply_text("❌ O GGPIX não retornou os dados completos do PIX. Tente novamente.")
        return

    qr = qrcode.make(pix_code)
    image = io.BytesIO()
    qr.save(image, format="PNG")
    image.seek(0)

    await update.message.reply_text("PIX gerado com sucesso ✅\n\n" f"Plano: {plan['name']}\n\n" f"Valor: {money(amount)}")
    await update.message.reply_text(
        "✅ Como realizar o pagamento:\n\n"
        "1. Abra o aplicativo do seu banco.\n"
        "2. Selecione “Pagar” ou “PIX”.\n"
        "3. Escolha “PIX Copia e Cola”.\n"
        "4. Cole o código da mensagem abaixo."
    )
    await update.message.reply_text("Copie o código abaixo:")
    await update.message.reply_text(f"<code>{pix_code}</code>", parse_mode="HTML")
    await update.message.reply_text("Após efetuar o pagamento, clique no botão abaixo 👇", reply_markup=payment_keyboard(tx_id, pix_code))
    await update.message.reply_photo(photo=image, caption="📲 QR Code do PIX")

    task = asyncio.create_task(poll_payment(context.application, update.effective_chat.id, tx_id))
    payment_tasks[tx_id] = task


async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_chat:
        return
    await query.answer("Consultando o status...")
    tx_id = query.data.split(":", 1)[1]
    try:
        tx = await get_transaction(tx_id)
    except Exception:
        await query.message.reply_text("❌ Não consegui consultar o status agora. Tente novamente em alguns segundos.")
        return
    status = str(tx.get("status", "UNKNOWN")).upper()
    if status == "COMPLETE":
        await notify_paid(context.application, update.effective_chat.id, tx)
    elif status == "PENDING":
        await query.message.reply_text("⏳ Ainda aguardando a confirmação do PIX.")
    else:
        await query.message.reply_text(f"Status atual do PIX: {status}")


def verify_webhook(raw_body: bytes, header: str) -> bool:
    if not GGPIX_WEBHOOK_SECRET:
        return True
    if not header:
        return False
    try:
        match = re.search(r"t=(\d+),v1=([a-f0-9]+)", header)
        if not match:
            return False
        timestamp = int(match.group(1))
        signature = match.group(2)
        if abs(time.time() - timestamp) > 300:
            return False
        expected = hmac.new(GGPIX_WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError):
        return False


async def handle_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")
    if not verify_webhook(raw_body, signature):
        return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)
    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    event_type = event.get("type") or event.get("event")
    if event_type not in {"PIX_IN", "pix.in", "transaction.complete", "payment.complete"}:
        return JSONResponse({"ok": True})

    tx = event.get("data") or event.get("transaction") or event
    if str(tx.get("status", "")).upper() != "COMPLETE":
        return JSONResponse({"ok": True})

    external_id = str(tx.get("externalId") or tx.get("external_id") or "")
    parts = external_id.split(":")
    if len(parts) >= 2 and parts[0] == "viphot":
        try:
            chat_id = int(parts[1])
        except ValueError:
            return JSONResponse({"ok": True})
        reminder_stop(chat_id)
        if telegram_app is not None:
            try:
                await notify_paid(telegram_app, chat_id, tx)
            except Exception:
                log.exception("Falha ao notificar pagamento confirmado")
    return JSONResponse({"ok": True})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN não configurado; servidor web ativo, Telegram desativado")
        yield
        return

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("assinar", assinar))
    telegram_app.add_handler(CommandHandler("meuacesso", meu_acesso))
    telegram_app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy$"))
    telegram_app.add_handler(CallbackQueryHandler(status_callback, pattern=r"^status$"))
    telegram_app.add_handler(CallbackQueryHandler(back_callback, pattern=r"^back$"))
    telegram_app.add_handler(CallbackQueryHandler(choose_plan, pattern=r"^plan:"))
    telegram_app.add_handler(CallbackQueryHandler(check_payment, pattern=r"^check:"))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_customer_data))

    await telegram_app.initialize()
    await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
    await telegram_app.bot.set_my_commands([
        BotCommand("start", "Iniciar"),
        BotCommand("assinar", "Assinar acesso VIP"),
        BotCommand("meuacesso", "Consultar meu acesso"),
    ])
    log.info("VIPHOT online | GGPIX configured: %s | Reminder video configured: %s", bool(GGPIX_API_KEY), bool(VIDEO_FILE_ID))
    yield

    for task in list(payment_tasks.values()) + list(reminder_tasks.values()):
        task.cancel()
    await asyncio.gather(*list(payment_tasks.values()), *list(reminder_tasks.values()), return_exceptions=True)
    payment_tasks.clear()
    reminder_tasks.clear()
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()
    telegram_app = None


app = FastAPI(title="VIPHOT", version="2.0.0", lifespan=lifespan)
app.add_api_route("/webhooks/pix", handle_webhook, methods=["POST"])


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "VIPHOT", "payment": "GGPIX"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
