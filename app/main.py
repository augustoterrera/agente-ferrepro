from __future__ import annotations

import logging
import secrets
import traceback

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from . import meta, notifier

from .chatwoot import (
    ChatwootError,
    conversation_labels,
    detect_agent_intrusion,
    extract_message_event,
    parse_chatwoot_payload,
    verify_chatwoot_signature,
    verify_chatwoot_webhook_token,
)
from .chatwoot_service import chatwoot_event_key, persist_incoming_chatwoot_event
from .config import settings

app = FastAPI(title="agente-ferrepro")
logger = logging.getLogger(__name__)


@app.exception_handler(Exception)
async def _alerta_telegram(request: Request, exc: Exception) -> JSONResponse:
    """Solo captura excepciones NO manejadas (500). Las HTTPException tienen su handler propio
    de FastAPI, así que esto no se dispara con 4xx/5xx esperados. notifier nunca lanza."""
    notifier.notify_error(
        "excepción no manejada en la API",
        detalle=traceback.format_exc(),
        contexto={"method": request.method, "path": request.url.path},
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


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
        "has_meta": bool(settings.meta_access_token and settings.meta_phone_number_id and settings.meta_catalog_id),
        "has_meta_webhook": bool(
            settings.meta_app_secret and settings.meta_verify_token and settings.meta_webhook_forward_url
        ),
    }


@app.get("/webhooks/meta", response_class=PlainTextResponse)
def verify_meta_webhook(
    mode: str = Query(default="", alias="hub.mode"),
    token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
) -> str:
    if mode != "subscribe" or not settings.meta_verify_token or not secrets.compare_digest(
        token, settings.meta_verify_token
    ):
        raise HTTPException(status_code=403, detail="Token de verificación inválido")
    return challenge


@app.post("/webhooks/meta")
async def meta_webhook(request: Request) -> dict[str, object]:
    raw_body = await request.body()
    if not meta.verify_webhook_signature(raw_body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Firma de Meta inválida")
    try:
        orders = await run_in_threadpool(meta.relay_webhook, raw_body)
    except meta.MetaError as exc:
        logger.exception("meta_webhook_relay_failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "orders": orders}


async def _alertar_si_hay_intromision(payload: dict) -> None:
    """Nunca puede romper la atención al cliente: va envuelta y solo emite una alerta."""
    try:
        intrusion = detect_agent_intrusion(
            payload, settings.chatwoot_sucursal_agent_ids, settings.chatwoot_bot_agent_id
        )
        if intrusion is None:
            return
        if intrusion.motivo == "sin_asignar":
            titulo = "Una sucursal contestó una conversación sin asignar"
            contexto = {
                "sucursal": f"{intrusion.sender_name} (id {intrusion.sender_id})",
                "conversación": intrusion.conversation_id,
                "detalle": "no estaba derivada a nadie: la atendía el bot",
            }
        else:
            titulo = "Una sucursal contestó una conversación de otra"
            contexto = {
                "contestó": f"{intrusion.sender_name} (id {intrusion.sender_id})",
                "asignada a": f"{intrusion.assignee_name} (id {intrusion.assignee_id})",
                "conversación": intrusion.conversation_id,
            }
        logger.warning(
            "agent_intrusion",
            extra={"conversation_id": intrusion.conversation_id, "motivo": intrusion.motivo},
        )
        # A threadpool: mandar la alerta es E/S bloqueante y no debe frenar el event loop.
        await run_in_threadpool(notifier.notify_warning, titulo, contexto)
    except Exception:
        logger.exception("alerta_de_intromision_fallo")


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
        # Los mensajes de los agentes caen acá (no son "incoming"). Chatwoot Community no puede
        # ocultar conversaciones —los roles custom son de pago—, así que lo que no se puede
        # prevenir se audita: si una sucursal contesta lo que no le toca, avisamos.
        await _alertar_si_hay_intromision(payload)
        return {"ok": True, "handled": False, "reason": ignore_reason}

    # Gate de humano: si la conversación tiene bot_apagado (lo pone un humano en Chatwoot),
    # no hacemos nada. Las labels vienen en el payload, no hace falta pegarle a la API.
    if settings.bot_apagado_label in conversation_labels(payload):
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


@app.get("/admin/retargeting-stats")
def retargeting_stats(x_admin_token: str = Header(default="")) -> dict[str, object]:
    """Funnel del retargeting: enviados, cuántos contestaron y cuántos llegaron a etapa compra.
    Sin ADMIN_TOKEN configurado el endpoint no existe (404), así no queda abierto por olvido."""
    if not settings.admin_token:
        raise HTTPException(status_code=404, detail="Not Found")
    if not secrets.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="Token inválido")
    from . import chat_memory

    return {
        "ok": True,
        "enabled": settings.retargeting_enabled,
        "dry_run": settings.retargeting_dry_run,
        **chat_memory.retargeting_stats(settings.channel),
    }
