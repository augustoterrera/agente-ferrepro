# Despliegue (producción)

Stack: FastAPI (webhook) + Celery workers + beat + Redis + Caddy (HTTPS automático), todo en
`docker compose`. Datos en Supabase (REST). Sin Postgres directo.

## Requisitos
- VPS con Docker + Docker Compose.
- Un (sub)dominio con registro **A** apuntando a la IP del VPS (ej. `bot.tudominio.com`).
- Puertos **80** y **443** abiertos (Caddy saca el certificado por HTTP-01).
- Salida a internet hacia Supabase, OpenAI y Chatwoot.

## Pasos
1. Cloná el repo en el VPS.
2. Creá `.env` a partir de `.env.example` y completá:
   - `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
   - `OPENAI_API_KEY`
   - `CHATWOOT_URL`, `CHATWOOT_ACCOUNT_ID`, `CHATWOOT_ACCESS_TOKEN`, `CHATWOOT_WEBHOOK_SECRET`
   - `DOMAIN=bot.tudominio.com`
   - `REQUIRE_WEBHOOK_SECRET=true`
   - (opcional) `REDIS_URL` / `CELERY_RESULT_BACKEND` si reusás otro Redis.
3. Levantá con el profile de producción (incluye Caddy):
   ```bash
   docker compose --profile prod up -d --build
   ```
4. Caddy provisiona el HTTPS solo (puede tardar ~1 min la primera vez). Verificá:
   ```bash
   curl https://$DOMAIN/health     # → {"ok":true, ...}
   ```
5. En Chatwoot: Settings → Integrations → **Webhooks** → agregar:
   ```
   https://TU_DOMINIO/webhooks/chatwoot?token=EL_VALOR_DE_CHATWOOT_WEBHOOK_SECRET
   ```
   suscrito al evento **Message created**.
6. Probá mandando un mensaje al inbox y seguí:
   ```bash
   docker compose logs -f worker_messages worker_outbound
   ```

## Operación
- **Logs:** `docker compose logs -f <servicio>` (api, worker_messages, worker_outbound, beat, caddy).
- **Actualizar código:** `git pull && docker compose --profile prod up -d --build`.
- **Solo cambió el prompt** (`app/prompts/ferrepro.md`): aplica solo (se relee por turno y está montado), sin reiniciar.
- **Escalar:** subí `--concurrency` de los workers o corré más réplicas del worker de mensajes.

## Notas
- El `api` no publica puerto al host: el ingress es Caddy. Para debug: `docker compose exec api ...`.
- La cola es durable aunque se reinicie Redis: jobs y outbox viven en Supabase y el beat
  re-despacha lo pendiente (sweepers cada 1–15 min).
- `apagar_bot` (etiqueta en Chatwoot) silencia el bot en esa conversación; el clasificador
  setea compra/asesoramiento/sin_stock/sin_etiqueta solo.
