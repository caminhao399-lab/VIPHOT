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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("viphot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BRAVOPAY_API_KEY = os.getenv("BRAVOPAY_API_KEY", "").strip()
BRAVOPAY_WEBHOOK_SECRET = os.getenv("BRAVOPAY_WEBHOOK_SECRET", "").strip()
BRAVOPAY_BASE_URL = os.getenv(
    "BRAVOPAY_BASE_URL", "https://bravopay.club/api/v1"
).strip().rstrip("/")

PLANS = {
    "essential": {"name": "VIP Essencial", "amount_cents": 800},
    "premium": {"name": "VIP Premium", "amount_cents": 1490},
    "acervo": {"name": "VIP Premium + Acervo", "amount_cents": 1690},
    "full": {"name": "Acesso Full + Bônus", "amount_cents": 2390},
}

# In-memory task registry. Payment state itself is kept at BravoPay and can be
# recovered by GET /transactions/{id}; no card or CPF is stored here.
payment_tasks: dict[str, asyncio.Task] = {}


def money(cents: int) -> str:
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def clean_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def valid_document(value: str) -> bool:
    digits = clean_digits(value)
    return len(digits) in (11, 14)


def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 VIP Essencial — R$ 8,00", callback_data="plan:essential")],
            [InlineKeyboardButton("🔴 VIP Premium — R$ 14,90", callback_data="plan:premium")],
            [InlineKeyboardButton("🔒 VIP Premium + Acervo — R$ 16,90", callback_data="plan:acervo")],
            [InlineKeyboardButton("🎁 Acesso Full + Bônus — R$ 23,90", callback_data="plan:full")],
        ]
    )


def payment_keyboard(tx_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Verificar pagamento", callback_data=f"check:{tx_id}")]]
    )


async def bravo_request(method: str, path: str, *, json: Any | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    if not BRAVOPAY_API_KEY:
        raise RuntimeError("BRAVOPAY_API_KEY não configurada no Render")

    request_headers = {
        "Authorization": f"Bearer {BRAVOPAY_API_KEY}",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)

    timeout = httpx.Timeout(25.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            f"{BRAVOPAY_BASE_URL}{path}",
            json=json,
            headers=request_headers,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"error": {"message": response.text[:500]}}
        return response.status_code, data


async def create_pix(plan_key: str, chat_id: int, name: str, email: str, document: str) -> dict[str, Any]:
    plan = PLANS[plan_key]
    order_id = f"viphot:{chat_id}:{uuid.uuid4().hex[:16]}"
    payload: dict[str, Any] = {
        "amount_cents": plan["amount_cents"],
        "method": "pix",
        "customer": {
            "name": name,
            "email": email,
            "cpf": clean_digits(document),
        },
        "description": f"{plan['name']} - acesso 18+",
        "external_reference": order_id,
        "metadata": {
            "telegram_chat_id": str(chat_id),
            "plan": plan_key,
        },
        "expires_in": 3600,
    }
    idempotency_key = f"viphot-{uuid.uuid4().hex}"
    status, data = await bravo_request(
        "POST",
        "/transactions",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    if status >= 400:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("message") or f"BravoPay HTTP {status}"
        raise RuntimeError(message)
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
    await application.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ PAGAMENTO CONFIRMADO!\n\n"
            f"Valor: {money(amount)}\n"
            f"Transação: {tx_id}\n\n"
            "Seu pagamento foi confirmado pela BravoPay.\n"
            "O próximo passo de liberação do acesso será configurado depois que o PIX estiver funcionando 100%."
        ),
    )


async def poll_payment(application: Application, chat_id: int, tx_id: str) -> None:
    try:
        for _ in range(360):  # up to 60 minutes, matching the PIX expiration
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
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ O PIX ficou com status {status}. Use /start para gerar uma nova cobrança.",
                )
                return
    finally:
        payment_tasks.pop(tx_id, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    context.user_data.clear()
    await update.message.reply_text(
        "🔞 Área exclusiva para maiores de 18 anos.\n\n"
        "Escolha seu plano para gerar um PIX pela BravoPay:",
        reply_markup=plans_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Operação cancelada. Use /start para começar novamente.")


async def choose_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    _, plan_key = query.data.split(":", 1)
    if plan_key not in PLANS:
        await query.edit_message_text("Plano inválido. Use /start novamente.")
        return

    context.user_data.clear()
    context.user_data["plan"] = plan_key
    context.user_data["step"] = "name"
    plan = PLANS[plan_key]
    await query.edit_message_text(
        f"Você escolheu: {plan['name']} — {money(plan['amount_cents'])}\n\n"
        "Digite seu nome completo:\n\n"
        "/cancel para cancelar."
    )


async def receive_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    step = context.user_data.get("step")
    text = (update.message.text or "").strip()

    if step == "name":
        if len(text) < 3:
            await update.message.reply_text("Digite seu nome completo.")
            return
        context.user_data["name"] = text[:120]
        context.user_data["step"] = "email"
        await update.message.reply_text("Agora digite seu e-mail:")
        return

    if step == "email":
        if not valid_email(text):
            await update.message.reply_text("E-mail inválido. Digite um e-mail válido:")
            return
        context.user_data["email"] = text.lower()[:200]
        context.user_data["step"] = "document"
        await update.message.reply_text("Digite seu CPF ou CNPJ (somente números ou com máscara):")
        return

    if step == "document":
        if not valid_document(text):
            await update.message.reply_text("CPF/CNPJ inválido. Envie 11 dígitos (CPF) ou 14 dígitos (CNPJ):")
            return

        plan_key = context.user_data.get("plan")
        if plan_key not in PLANS:
            context.user_data.clear()
            await update.message.reply_text("Sessão expirada. Use /start novamente.")
            return

        await update.message.reply_text("⏳ Gerando seu PIX pela BravoPay...")
        try:
            tx = await create_pix(
                plan_key,
                update.effective_chat.id,
                context.user_data["name"],
                context.user_data["email"],
                text,
            )
        except Exception as exc:
            log.exception("Erro ao criar PIX")
            await update.message.reply_text(
                "❌ Não foi possível gerar o PIX agora.\n\n"
                f"Motivo retornado pela integração: {exc}\n\n"
                "Tente novamente com /start."
            )
            return

        tx_id = str(tx.get("id", ""))
        copy_paste = ((tx.get("pix") or {}).get("copy_paste") or "").strip()
        if not tx_id or not copy_paste:
            log.error("Resposta BravoPay sem tx.id ou pix.copy_paste")
            await update.message.reply_text("❌ A BravoPay não retornou os dados completos do PIX. Tente novamente.")
            return

        qr = qrcode.make(copy_paste)
        image = io.BytesIO()
        qr.save(image, format="PNG")
        image.seek(0)

        plan = PLANS[plan_key]
        await update.message.reply_photo(
            photo=image,
            caption=(
                f"💳 PIX gerado\n\n"
                f"Plano: {plan['name']}\n"
                f"Valor: {money(plan['amount_cents'])}\n\n"
                "Escaneie o QR Code ou copie o código abaixo.\n"
                "Depois do pagamento, use o botão para verificar."
            ),
            reply_markup=payment_keyboard(tx_id),
        )
        await update.message.reply_text(f"<code>{copy_paste}</code>", parse_mode="HTML")

        context.user_data.clear()
        task = asyncio.create_task(poll_payment(context.application, update.effective_chat.id, tx_id))
        payment_tasks[tx_id] = task
        return

    await update.message.reply_text("Use /start para começar.")


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
        expected = hmac.new(
            BRAVOPAY_WEBHOOK_SECRET.encode(), signed, hashlib.sha256
        ).hexdigest()
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

    if event.get("type") != "transaction.paid":
        return JSONResponse({"ok": True})

    tx = event.get("data") or {}
    external_reference = str(tx.get("external_reference", ""))
    match = re.match(r"^viphot:(-?\d+):", external_reference)
    if match:
        chat_id = int(match.group(1))
        try:
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ PAGAMENTO CONFIRMADO!\n\n"
                    f"Valor: {money(int(tx.get('amount_cents', 0) or 0))}\n"
                    f"Transação: {tx.get('id', '')}\n\n"
                    "A confirmação foi recebida diretamente da BravoPay."
                ),
            )
        except Exception:
            log.exception("Falha ao avisar usuário pelo webhook")

    return JSONResponse({"ok": True})


telegram_app: Application


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN não configurado no Render")

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("cancel", cancel))
    telegram_app.add_handler(CallbackQueryHandler(choose_plan, pattern=r"^plan:"))
    telegram_app.add_handler(CallbackQueryHandler(check_payment, pattern=r"^check:"))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_details))

    # Cleanly use polling for this first deployment. Removing any previous
    # webhook prevents the classic Telegram getUpdates/webhook conflict.
    await telegram_app.initialize()
    await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)

    log.info("VIPHOT online | Telegram configured: True | BravoPay key configured: %s", bool(BRAVOPAY_API_KEY))
    yield

    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(title="VIPHOT", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "VIPHOT"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/bravopay")
async def bravopay_webhook(request: Request) -> JSONResponse:
    return await handle_webhook(request)
