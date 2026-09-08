import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

BRAVOPAY_API_KEY = os.getenv("BRAVOPAY_API_KEY", "").strip()
BRAVOPAY_BASE_URL = os.getenv("BRAVOPAY_BASE_URL", "https://bravopay.club/api/v1").strip().rstrip("/")
BRAVOPAY_PRODUCT_ID = os.getenv("BRAVOPAY_PRODUCT_ID", "").strip()

PLANS = {
    "essential": {"name": "VIP Essencial", "amount_cents": 800},
    "premium": {"name": "VIP Premium", "amount_cents": 1490},
    "acervo": {"name": "VIP Premium + Acervo", "amount_cents": 1690},
    "full": {"name": "Acesso Full + Bônus", "amount_cents": 2390},
}

UTM_KEYS = ("source", "medium", "campaign", "content", "term", "fbclid", "ttclid", "gclid")

router = APIRouter()


class Customer(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=8, max_length=30)
    cpf: str = Field(min_length=11, max_length=18)


class CheckoutRequest(BaseModel):
    plan_id: str
    customer: Customer
    utm: dict[str, Any] = Field(default_factory=dict)


def digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def clean_utm(value: dict[str, Any]) -> dict[str, str]:
    result = {}
    for key in UTM_KEYS:
        raw = value.get(key)
        if raw is not None and str(raw).strip():
            result[key] = str(raw).strip()[:500]
    return result


async def bravo(method: str, path: str, *, payload: dict | None = None, idempotency: str | None = None):
    if not BRAVOPAY_API_KEY:
        raise HTTPException(500, "BRAVOPAY_API_KEY não configurada no Render.")
    headers = {
        "Authorization": f"Bearer {BRAVOPAY_API_KEY}",
        "Content-Type": "application/json",
    }
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.request(method, f"{BRAVOPAY_BASE_URL}{path}", json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Erro de comunicação com BravoPay: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {"error": {"message": response.text[:500]}}
    if response.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise HTTPException(502, err.get("message") or f"BravoPay HTTP {response.status_code}")
    return data


@router.get("/checkout")
async def checkout_page():
    return FileResponse(os.path.join(STATIC_DIR, "checkout.html"))


@router.get("/obrigado")
async def obrigado_page():
    return FileResponse(os.path.join(STATIC_DIR, "obrigado.html"))


@router.post("/api/checkout/payment")
async def create_checkout_payment(data: CheckoutRequest):
    plan = PLANS.get(data.plan_id)
    if not plan:
        raise HTTPException(400, "Plano inválido.")

    cpf = digits(data.customer.cpf)
    phone = digits(data.customer.phone)
    if len(cpf) not in (11, 14):
        raise HTTPException(400, "CPF/CNPJ inválido.")
    if len(phone) < 10:
        raise HTTPException(400, "Telefone inválido.")

    external_reference = f"web:{uuid.uuid4().hex}"
    payload: dict[str, Any] = {
        "amount_cents": plan["amount_cents"],
        "method": "pix",
        "customer": {
            "name": data.customer.name.strip(),
            "email": str(data.customer.email),
            "phone": phone,
            "cpf": cpf,
        },
        "description": f"{plan['name']} - acesso 18+",
        "external_reference": external_reference,
        "utm": clean_utm(data.utm),
        "metadata": {"checkout": "viphot_web", "plan": data.plan_id},
    }

    # Quando UTMify for usado, envie o ID REAL do produto BravoPay.
    if BRAVOPAY_PRODUCT_ID:
        payload["product_id"] = BRAVOPAY_PRODUCT_ID

    tx = await bravo(
        "POST",
        "/transactions",
        payload=payload,
        idempotency=f"viphot-web-{uuid.uuid4().hex}",
    )
    pix = tx.get("pix") or {}
    copy_paste = str(pix.get("copy_paste") or "").strip()
    if not tx.get("id") or not copy_paste:
        raise HTTPException(502, "BravoPay não retornou o PIX completo.")

    return {
        "success": True,
        "transaction_id": tx["id"],
        "status": tx.get("status", "PENDING"),
        "amount_cents": tx.get("amount_cents", plan["amount_cents"]),
        "pix": {"copy_paste": copy_paste, "expires_at": pix.get("expires_at")},
    }


@router.get("/api/checkout/payment/{transaction_id}")
async def checkout_payment_status(transaction_id: str):
    if not transaction_id or len(transaction_id) > 200:
        raise HTTPException(400, "ID de transação inválido.")
    tx = await bravo("GET", f"/transactions/{transaction_id}")
    return {
        "id": tx.get("id", transaction_id),
        "status": str(tx.get("status", "UNKNOWN")).upper(),
        "paid": str(tx.get("status", "")).upper() == "PAID",
    }
