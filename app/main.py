from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .chatwoot import (
    ChatwootError,
    conversation_labels,
    extract_message_event,
    parse_chatwoot_payload,
    verify_chatwoot_signature,
    verify_chatwoot_webhook_token,
)
from .chatwoot_service import chatwoot_event_key, persist_incoming_chatwoot_event
from .config import settings

app = FastAPI(title="agente-ferrepro")
logger = logging.getLogger(__name__)


@app.on_event("startup")
def startup() -> None:
    if not settings.supabase_url or not settings.supabase_service_key:
        raise RuntimeError("Faltan SUPABASE_URL / SUPABASE_SERVICE_KEY: el agente no puede leer ni persistir.")
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY ausente: el agente no podrá responder.")
    if not settings.chatwoot_webhook_secret:
        if settings.require_webhook_secret:
            raise RuntimeError(
                "REQUIRE_WEBHOOK_SECRET=true pero falta CHATWOOT_WEBHOOK_SECRET: el webhook aceptaría "
                "cualquier POST sin firmar. Abortando arranque inseguro."
            )
        logger.warning("Webhook SIN secret: acepta cualquier POST (inseguro para prod). Configurá CHATWOOT_WEBHOOK_SECRET.")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "has_supabase": bool(settings.supabase_url and settings.supabase_service_key),
        "has_openai": bool(settings.openai_api_key),
        "has_chatwoot": bool(settings.chatwoot_url and settings.chatwoot_access_token),
        "has_webhook_secret": bool(settings.chatwoot_webhook_secret),
    }


@app.post("/webhooks/chatwoot")
async def chatwoot_webhook(request: Request) -> dict[str, object]:
    raw_body = await request.body()

    verified = verify_chatwoot_signature(
        raw_body=raw_body,
        secret=settings.chatwoot_webhook_secret,
        signature=request.headers.get("x-chatwoot-signature"),
        timestamp=request.headers.get("x-chatwoot-timestamp"),
        tolerance_seconds=settings.chatwoot_webhook_timestamp_tolerance_seconds,
    )
    if not verified:
        verified = verify_chatwoot_webhook_token(settings.chatwoot_webhook_secret, request.query_params.get("token"))
    if not verified:
        raise HTTPException(status_code=401, detail="Firma de webhook inválida")

    try:
        payload = parse_chatwoot_payload(raw_body)
        event, ignore_reason = extract_message_event(payload, settings.chatwoot_history_limit)
    except ChatwootError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if event is None:
        return {"ok": True, "handled": False, "reason": ignore_reason}

    # Gate de humano: si la conversación tiene apagar_bot (lo pone un humano en Chatwoot),
    # no hacemos nada. Las labels vienen en el payload, no hace falta pegarle a la API.
    if settings.apagar_bot_label in conversation_labels(payload):
        return {"ok": True, "handled": False, "reason": "bot_off"}

    event_key = chatwoot_event_key(
        {k.lower(): v for k, v in request.headers.items()}, event.conversation_id, event.message_id
    )

    # persist_incoming hace varias llamadas REST (bloqueantes): a threadpool para no
    # frenar el event loop. Devolvemos rápido; el procesamiento real es async en Celery.
    try:
        is_new, conversation, job_id = await run_in_threadpool(
            persist_incoming_chatwoot_event, event_key, event, payload
        )
    except Exception as exc:
        logger.exception("persist_incoming_failed", extra={"event_key": event_key})
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not is_new:
        return {"ok": True, "handled": False, "status": "duplicate", "conversation_id": conversation.id}

    # Import local: evita que el arranque de la API dependa de Celery/Redis.
    from .tasks.chatwoot_tasks import process_chatwoot_conversation, set_conversation_debounce

    set_conversation_debounce(conversation.id)
    process_chatwoot_conversation.apply_async(
        (str(conversation.id),), queue="chatwoot_messages", countdown=settings.chatwoot_debounce_seconds
    )
    return {"ok": True, "handled": True, "status": "queued", "conversation_id": conversation.id, "job_id": job_id}


@app.get("/webhooks/chatwoot/health")
def webhook_health() -> dict[str, object]:
    return {"ok": True, "endpoint": "/webhooks/chatwoot", "channel": settings.channel}
