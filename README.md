# VIPHOT

Bot Telegram 18+ com geração de PIX pela GGPIX.

## Fluxo

`/start` → vídeo + boas-vindas → Assinar acesso VIP / Meu acesso → escolher plano → nome → CPF/CNPJ → gerar PIX → QR Code + copia e cola → consultar status → confirmação → lembretes de 30/60/90/120 minutos.

A confirmação e a liberação do acesso permanecem sob responsabilidade da configuração do GGPIX. O código apenas acompanha o status da cobrança para atualizar a conversa e interromper os lembretes após a confirmação.

O código não contém tokens ou chaves. Configure os segredos somente nas variáveis de ambiente da Render.

## Variáveis

Obrigatórias:

- `BOT_TOKEN`
- `GGPIX_API_KEY`

Opcionais:

- `GGPIX_BASE_URL` (padrão: `https://ggpixapi.com/api/v1`)
- `GGPIX_WEBHOOK_SECRET`
- `WEBHOOK_URL`
- `VIDEO_FILE_ID`
- `DB_PATH`

## Render

Build:

`pip install -r requirements.txt`

Start:

`uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## GGPIX

A API usa `POST /pix/in` para criar a cobrança PIX e `GET /transactions/{id}` para consultar o status. O código retornado em `pixCopyPaste` é usado no botão de cópia e no QR Code. Webhooks podem ser usados para confirmação imediata; quando `GGPIX_WEBHOOK_SECRET` estiver configurado, a assinatura HMAC-SHA256 é validada.
