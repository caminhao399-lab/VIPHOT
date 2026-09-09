import asyncio
import hashlib
import hmac
import io
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import qrcode
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import BotCommand, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.checkout import router as checkout_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("viphot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BRAVOPAY_API_KEY = os.getenv("BRAVOPAY_API_KEY", "").strip()
BRAVOPAY_WEBHOOK_SECRET = os.getenv("BRAVOPAY_WEBHOOK_SECRET", "").strip()
BRAVOPAY_BASE_URL = os.getenv("BRAVOPAY_BASE_URL", "https://bravopay.club/api/v1").strip().rstrip("/")

PLANS = {
    "essential": {"name": "VIP Essencial", "amount_cents": 800},
    "premium": {"name": "VIP Premium", "amount_cents": 1490},
    "acervo": {"name": "VIP Premium + Acervo", "amount_cents": 1690},
    "full": {"name": "Acesso Full + Bônus", "amount_cents": 2390},
}

payment_tasks: dict[str, asyncio.Task] = {}
telegram_app: Application | None = None


def money(cents: int) -> str:
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 VIP Essencial — R$ 8,00", callback_data="plan:essential")],
        [InlineKeyboardButton("🔴 VIP Premium — R$ 14,90", callback_data="plan:premium")],
        [InlineKeyboardButton("🔒 VIP Premium + Acervo — R$ 16,90", callback_data="plan:acervo")],
        [InlineKeyboardButton("🎁 Acesso Full + Bônus — R$ 23,90", callback_data="plan:full")],
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


async def bravo_request(method: str, path: str, *, json: Any | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    if not BRAVOPAY_API_KEY:
        raise RuntimeError("BRAVOPAY_API_KEY não configurada no Render")
    request_headers = {"Authorization": f"Bearer {BRAVOPAY_API_KEY}", "Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    timeout = httpx.Timeout(25.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, f"{BRAVOPAY_BASE_URL}{path}", json=json, headers=request_headers)
        try:
            data = response.json()
        except ValueError:
            data = {"error": {"message": response.text[:500]}}
        return response.status_code, data


async def create_pix(plan_key: str, chat_id: int) -> dict[str, Any]:
    plan = PLANS[plan_key]
    order_id = f"viphot:{chat_id}:{uuid.uuid4().hex[:16]}"
    payload: dict[str, Any] = {
        "amount_cents": plan["amount_cents"],
        "method": "pix",
        "description": f"{plan['name']} - acesso 18+",
        "external_reference": order_id,
        "metadata": {"telegram_chat_id": str(chat_id), "plan": plan_key},
        "expires_in": 3600,
    }
    status, data = await bravo_request("POST", "/transactions", json=payload, headers={"Idempotency-Key": f"viphot-{uuid.uuid4().hex}"})
    if status >= 400:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(error.get("message") or f"BravoPay HTTP {status}")
    return data


async def get_transaction(tx_id: str) -> dict[str, Any]:
    status, data = await bravo_request("GET", f"/transactions/{tx_id}")
    if status >= 400:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(error.get("message") or f"BravoPay HTTP {status}")
    return data


async def notify_paid(application: Application, chat_id: int, tx: dict[str, Any]) -> None:
    tx_id = str(tx.get("id", ""))
    amount = int(tx.get("amount_cents", 0) or 0)
    await application.bot.send_message(chat_id=chat_id, text=(
        "✅ PAGAMENTO CONFIRMADO!\n\n"
        f"Valor: {money(amount)}\n"
        f"Transação: {tx_id}\n\n"
        "Seu pagamento foi confirmado pela BravoPay."
    ))


async def poll_payment(application: Application, chat_id: int, tx_id: str) -> None:
    try:
        for _ in range(360):
            await asyncio.sleep(10)
            try:
                tx = await get_transaction(tx_id)
            except Exception as exc:
                log.warning("Falha ao consultar %s: %s", tx_id, exc)
                continue
            status = str(tx.get("status", "")).upper()
            if status == "PAID":
                await notify_paid(application, chat_id, tx)
                return
            if status in {"EXPIRED", "FAILED", "CANCELED", "REFUNDED", "CHARGEBACK"}:
                await application.bot.send_message(chat_id=chat_id, text=f"⚠️ O PIX ficou com status {status}. Use /assinar para gerar uma nova cobrança.")
                return
    finally:
        payment_tasks.pop(tx_id, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    context.user_data.clear()
    await update.message.reply_text(
        "🔞 Área exclusiva para maiores de 18 anos.\n\nBem-vindo ao VIPHOT. Escolha uma opção:",
        reply_markup=menu_keyboard(),
    )


async def assinar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    context.user_data.clear()
    await update.message.reply_text("⭐ <b>Assinar acesso VIP</b>\n\nEscolha seu plano:", reply_markup=plans_keyboard(), parse_mode="HTML")


async def meu_acesso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    await update.message.reply_text(
        "📅 <b>Meu acesso</b>\n\nSeu acesso é atualizado após a confirmação do pagamento.\nSe você acabou de pagar, aguarde a confirmação da BravoPay.",
        parse_mode="HTML",
        reply_markup=menu_keyboard(),
    )


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
    await query.edit_message_text(
        "📅 <b>Meu acesso</b>\n\nSeu acesso é atualizado após a confirmação do pagamento.\nSe você acabou de pagar, aguarde a confirmação da BravoPay.",
        reply_markup=menu_keyboard(),
        parse_mode="HTML",
    )


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

    plan = PLANS[plan_key]
    await query.edit_message_text(f"⏳ Gerando seu PIX pela BravoPay...\n\nPlano: {plan['name']}\nValor: {money(plan['amount_cents'])}")
    try:
        tx = await create_pix(plan_key, update.effective_chat.id)
    except Exception as exc:
        log.exception("Erro ao criar PIX")
        await query.message.reply_text(f"❌ Não foi possível gerar o PIX agora.\n\nMotivo retornado pela integração: {exc}\n\nTente novamente em /assinar.")
        return

    tx_id = str(tx.get("id", ""))
    copy_paste = ((tx.get("pix") or {}).get("copy_paste") or "").strip()
    if not tx_id or not copy_paste:
        log.error("Resposta BravoPay sem tx.id ou pix.copy_paste")
        await query.message.reply_text("❌ A BravoPay não retornou os dados completos do PIX. Tente novamente.")
        return

    qr = qrcode.make(copy_paste)
    image = io.BytesIO()
    qr.save(image, format="PNG")
    image.seek(0)

    await query.message.reply_text("PIX gerado com sucesso ✅\n\n" f"Plano: {plan['name']}\n\n" f"Valor: {money(plan['amount_cents'])}")
    await query.message.reply_text("✅ Como realizar o pagamento:\n\n1. Abra o aplicativo do seu banco.\n2. Selecione “Pagar” ou “PIX”.\n3. Escolha “PIX Copia e Cola”.\n4. Cole a chave da mensagem abaixo...")
    await query.message.reply_text("Copie o código abaixo:")
    await query.message.reply_text(f"<code>{copy_paste}</code>", parse_mode="HTML")
    await query.message.reply_text("Após efetuar o pagamento, clique no botão abaixo 👇", reply_markup=payment_keyboard(tx_id, copy_paste))
    await query.message.reply_photo(photo=image, caption="📲 QR Code do PIX")

    context.user_data.clear()
    task = asyncio.create_task(poll_payment(context.application, update.effective_chat.id, tx_id))
    payment_tasks[tx_id] = task


async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_chat:
        return
    await query.answer("Consultando a BravoPay...")
    tx_id = query.data.split(":", 1)[1]
    try:
        tx = await get_transaction(tx_id)
    except Exception as exc:
        await query.message.reply_text(f"❌ Não consegui consultar a transação agora: {exc}")
        return
    status = str(tx.get("status", "UNKNOWN")).upper()
    if status == "PAID":
        await notify_paid(context.application, update.effective_chat.id, tx)
    elif status == "PENDING":
        await query.message.reply_text("⏳ Ainda aguardando a confirmação do PIX.")
    else:
        await query.message.reply_text(f"Status atual do PIX: {status}")


def verify_webhook(raw_body: bytes, header: str) -> bool:
    if not BRAVOPAY_WEBHOOK_SECRET or not header:
        return False
    try:
        parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
        timestamp = int(parts.get("t", "0"))
        signature = parts.get("v1", "")
        if not timestamp or abs(time.time() - timestamp) > 300:
            return False
        signed = f"{timestamp}.".encode() + raw_body
        expected = hmac.new(BRAVOPAY_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError):
        return False


async def handle_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    signature = request.headers.get("BravoPay-Signature") or request.headers.get("X-Bravopay-Signature", "")
    if not verify_webhook(raw_body, signature):
        return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)
    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    event_type = event.get("type") or event.get("event")
    if event_type != "transaction.paid":
        return JSONResponse({"ok": True})
    tx = event.get("data") or event.get("transaction") or {}
    external_reference = str(tx.get("external_reference", ""))
    match = re.match(r"^viphot:(-?\d+):", external_reference)
    if match:
        chat_id = int(match.group(1))
        if telegram_app is None:
            log.warning("Pagamento confirmado, mas Telegram não está configurado para notificar o chat %s", chat_id)
        else:
            try:
                await telegram_app.bot.send_message(chat_id=chat_id, text=(
                    "✅ PAGAMENTO CONFIRMADO!\n\n"
                    f"Valor: {money(int(tx.get('amount_cents', 0) or 0))}\n"
                    f"Transação: {tx.get('id', '')}\n\n"
                    "A confirmação foi recebida diretamente da BravoPay."
                ))
            except Exception:
                log.exception("Falha ao avisar usuário pelo webhook")
    else:
        log.info("BravoPay transaction.paid recebido para checkout web: %s", external_reference)
    return JSONResponse({"ok": True})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if not BOT_TOKEN:
        telegram_app = None
        log.warning("BOT_TOKEN não configurado; iniciando somente o servidor web/checkout")
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

    await telegram_app.initialize()
    await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
    await telegram_app.bot.set_my_commands([
        BotCommand("start", "Iniciar"),
        BotCommand("assinar", "Assinar acesso VIP"),
        BotCommand("meuacesso", "Consultar meu acesso"),
    ])
    log.info("VIPHOT online | Telegram configured: True | BravoPay key configured: %s", bool(BRAVOPAY_API_KEY))
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()
    telegram_app = None


app = FastAPI(title="VIPHOT", version="1.2.0", lifespan=lifespan)
app.include_router(checkout_router)
app.add_api_route("/webhooks/bravopay", handle_webhook, methods=["POST"])


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "VIPHOT"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
