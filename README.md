# VIPHOT

Bot Telegram 18+ com geração de PIX pela BravoPay.

## Fluxo atual

`/start` → escolher plano → nome → e-mail → CPF/CNPJ → gerar PIX → QR Code + copia e cola → consultar status → confirmação `PAID`.

O código não contém tokens ou chaves. Configure os segredos somente nas variáveis de ambiente da Render.

## Variáveis obrigatórias

- `BOT_TOKEN`
- `BRAVOPAY_API_KEY`

## Variáveis opcionais

- `BRAVOPAY_BASE_URL` (padrão: `https://bravopay.club/api/v1`)
- `BRAVOPAY_WEBHOOK_SECRET` (secret gerado pela BravoPay ao cadastrar `/webhooks/bravopay`)

## Render

Build:

`pip install -r requirements.txt`

Start:

`uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## BravoPay

A API usa `POST /transactions` para criar PIX, `Idempotency-Key` para evitar cobranças duplicadas e `GET /transactions/{id}` para consulta. O webhook opcional usa HMAC-SHA256.
