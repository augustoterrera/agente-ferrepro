from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from typing import Iterator

import redis

from app import chat_memory
from app.celery_app import celery_app
from app.chatwoot import ChatwootError, build_chatwoot_client, should_handoff_to_agent
from app.chatwoot_service import process_pending_conversation_messages, sync_crm_label
from app.classifier import CLASSIFIER_LABELS, classify
from app.config import settings
from app.search import SearchError
from app.supabase import SupabaseError


def _chatwoot_client_for(conv):
    client = build_chatwoot_client(settings.chatwoot_url, settings.chatwoot_access_token)
    account_id = conv.account_id or settings.chatwoot_account_id
    return client, account_id


def _handoff_if_needed(client, account_id, conv, content: str) -> bool:
    if not should_handoff_to_agent(content):
        return False
    current = client.get_conversation_labels(account_id, conv.external_conversation_id)
    labels = current if settings.apagar_bot_label in current else [*current, settings.apagar_bot_label]
    client.set_conversation_labels(account_id, conv.external_conversation_id, labels)
    if settings.chatwoot_assignee_id is not None:
        client.assign_conversation(account_id, conv.external_conversation_id, settings.chatwoot_assignee_id)
    return True

logger = logging.getLogger(__name__)

# Excepciones transitorias que justifican reintentar la task (red/DB/LLM caídos un momento).
RETRYABLE = (SupabaseError, SearchError, ChatwootError)

# Reintento para los sweepers del beat: un 502/blip transitorio de Supabase se cura solo en
# pocos segundos (1+2+4s). Solo alerta si sigue fallando tras los retries (outage real).
SWEEPER_RETRY = dict(autoretry_for=RETRYABLE, retry_backoff=True, retry_jitter=True, max_retries=3)


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def debounce_key(cid: int | str) -> str:
    return f"chatwoot:conversation:{cid}:debounce"


def lock_key(cid: int | str) -> str:
    return f"chatwoot:conversation:{cid}:lock"


def requeue_key(cid: int | str) -> str:
    return f"chatwoot:conversation:{cid}:requeue"


def worker_id(task_id: str | None = None) -> str:
    return f"{socket.gethostname()}:{task_id or 'unknown'}"


def set_conversation_debounce(cid: int | str) -> None:
    try:
        _redis().set(debounce_key(cid), str(time.time()), ex=max(1, settings.chatwoot_debounce_seconds))
    except redis.RedisError as exc:
        logger.warning("debounce_set_failed", extra={"conversation_id": cid, "error": str(exc)})


def _debounce_active(cid: int | str) -> bool:
    try:
        return bool(_redis().exists(debounce_key(cid)))
    except redis.RedisError:
        return False


def _debounce_ttl(cid: int | str) -> int:
    try:
        return max(0, int(_redis().ttl(debounce_key(cid))))
    except redis.RedisError:
        return 0


def _requeue_once(cid: int | str, countdown: int) -> bool:
    countdown = max(1, countdown)
    try:
        was_set = bool(_redis().set(requeue_key(cid), str(time.time()), nx=True, ex=countdown))
    except redis.RedisError:
        was_set = True
    if was_set:
        process_chatwoot_conversation.apply_async((str(cid),), queue="chatwoot_messages", countdown=countdown)
    return was_set


@contextmanager
def _conversation_lock(cid: int | str, task_id: str | None) -> Iterator[bool]:
    client = _redis()
    key, value = lock_key(cid), worker_id(task_id)
    acquired = bool(client.set(key, value, nx=True, ex=max(1, settings.chatwoot_lock_seconds)))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if client.get(key) == value:
                    client.delete(key)
            except redis.RedisError:
                logger.warning("lock_release_failed", extra={"conversation_id": cid})


@celery_app.task(
    bind=True,
    name="app.tasks.chatwoot_tasks.process_chatwoot_conversation",
    queue="chatwoot_messages",
    autoretry_for=RETRYABLE,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=settings.chatwoot_job_max_retries,
)
def process_chatwoot_conversation(self, conversation_id: str) -> dict[str, object]:
    task_id = self.request.id

    # Debounce: si el cliente está mandando mensajes seguidos, esperamos a que pare para
    # responder una sola vez con todo junto.
    if _debounce_active(conversation_id):
        _requeue_once(conversation_id, _debounce_ttl(conversation_id) + 1)
        return {"ok": True, "conversation_id": conversation_id, "status": "debounced"}

    with _conversation_lock(conversation_id, task_id) as acquired:
        if not acquired:
            _requeue_once(conversation_id, settings.chatwoot_debounce_retry_seconds)
            return {"ok": True, "conversation_id": conversation_id, "status": "lock_busy"}

        conv = chat_memory.get_conversation(int(conversation_id))
        # Segundo lock, en la DB: serializa aunque haya varios workers/procesos.
        if not chat_memory.acquire_lock(conv.channel, conv.external_conversation_id, settings.chatwoot_lock_seconds):
            _requeue_once(conversation_id, settings.chatwoot_debounce_retry_seconds)
            return {"ok": True, "conversation_id": conversation_id, "status": "db_lock_busy"}

        try:
            # El gate apagar_bot se aplica en el webhook (labels vienen en el payload), así que
            # si llegamos acá es porque el bot está habilitado para esta conversación.
            chat_memory.update_jobs(conv.channel, conv.external_conversation_id, "processing", worker_id=worker_id(task_id))
            outbox_id = process_pending_conversation_messages(int(conversation_id))
            if outbox_id is not None:
                send_chatwoot_outbound_message.apply_async((str(outbox_id),), queue="chatwoot_outbound")
                outbox = chat_memory.get_outbox(outbox_id)
                if outbox and not should_handoff_to_agent(outbox["content"]):
                    # Clasificar y etiquetar la conversación (async, no frena el reply).
                    classify_and_label_conversation.apply_async((str(conversation_id),), queue="chatwoot_outbound")
            return {"ok": True, "conversation_id": conversation_id, "outbox_id": outbox_id}
        except Exception as exc:
            status = "failed" if self.request.retries >= settings.chatwoot_job_max_retries else "retry"
            chat_memory.update_jobs(conv.channel, conv.external_conversation_id, status, error=str(exc))
            chat_memory.update_events(conv.channel, conv.external_conversation_id, status, error=str(exc))
            raise
        finally:
            chat_memory.release_lock(conv.channel, conv.external_conversation_id)


@celery_app.task(
    bind=True,
    name="app.tasks.chatwoot_tasks.send_chatwoot_outbound_message",
    queue="chatwoot_outbound",
    autoretry_for=RETRYABLE,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=settings.chatwoot_outbox_max_retries,
)
def send_chatwoot_outbound_message(self, outbox_id: str) -> dict[str, object]:
    outbox = chat_memory.get_outbox(int(outbox_id))
    if outbox is None:
        return {"ok": False, "outbox_id": outbox_id, "status": "not_found"}
    if outbox["status"] in ("sent", "failed"):
        if outbox["status"] == "sent":
            conv = chat_memory.get_conversation(outbox["conversation_id"])
            client, account_id = _chatwoot_client_for(conv)
            if client and account_id:
                _handoff_if_needed(client, account_id, conv, outbox["content"])
        return {"ok": outbox["status"] == "sent", "outbox_id": outbox_id, "status": f"already_{outbox['status']}"}

    if not chat_memory.mark_outbox_processing(int(outbox_id)):
        return {"ok": True, "outbox_id": outbox_id, "status": "already_claimed"}

    conv = chat_memory.get_conversation(outbox["conversation_id"])
    account_id = conv.account_id or settings.chatwoot_account_id
    client = build_chatwoot_client(settings.chatwoot_url, settings.chatwoot_access_token)
    if client is None or account_id is None:
        status = chat_memory.mark_outbox_retry_or_failed(int(outbox_id), "Chatwoot no configurado")
        if status == "failed":
            return {"ok": False, "outbox_id": outbox_id, "status": "failed"}
        raise self.retry(countdown=settings.chatwoot_debounce_retry_seconds)

    try:
        response = client.create_outgoing_message(account_id, outbox["external_conversation_id"], outbox["content"])
        chat_memory.mark_outbox_sent(int(outbox_id), response)
    except Exception as exc:
        status = chat_memory.mark_outbox_retry_or_failed(int(outbox_id), str(exc))
        if status == "failed":
            raise
        raise self.retry(exc=exc)
    handoff = _handoff_if_needed(client, account_id, conv, outbox["content"])
    return {"ok": True, "outbox_id": outbox_id, "status": "sent", "handoff": handoff}


@celery_app.task(name="app.tasks.chatwoot_tasks.classify_and_label_conversation", queue="chatwoot_outbound")
def classify_and_label_conversation(conversation_id: str) -> dict[str, object]:
    """Clasifica la conversación (compra/asesoramiento/sin_stock/sin_etiqueta) y la etiqueta en
    Chatwoot, preservando etiquetas manuales como apagar_bot."""
    conv = chat_memory.get_conversation(int(conversation_id))
    client, account_id = _chatwoot_client_for(conv)
    if not client or not account_id:
        return {"ok": False, "status": "chatwoot_not_configured"}

    label = classify(chat_memory.recent_history(int(conversation_id), settings.chatwoot_history_limit))
    sync_crm_label(conv.external_conversation_id, label)  # para el dashboard (fire-and-forget)
    try:
        current = client.get_conversation_labels(account_id, conv.external_conversation_id)
        preserved = [lbl for lbl in current if lbl not in CLASSIFIER_LABELS]
        client.set_conversation_labels(account_id, conv.external_conversation_id, [*preserved, label])
    except ChatwootError as exc:
        logger.warning("set_label_failed", extra={"conversation_id": conversation_id, "error": str(exc)})
        return {"ok": False, "status": "label_api_failed", "label": label}
    return {"ok": True, "conversation_id": conversation_id, "label": label}


# ── Sweepers del beat (red de seguridad de la cola) ─────────────────────────
@celery_app.task(name="app.tasks.chatwoot_tasks.retry_stale_processing_jobs", queue="chatwoot_messages", **SWEEPER_RETRY)
def retry_stale_processing_jobs() -> dict[str, object]:
    ids = chat_memory.requeue_stale_jobs(settings.chatwoot_stale_processing_minutes)
    for cid in ids:
        process_chatwoot_conversation.apply_async((str(cid),), queue="chatwoot_messages")
    return {"ok": True, "requeued": len(ids)}


@celery_app.task(name="app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages", queue="chatwoot_outbound", **SWEEPER_RETRY)
def dispatch_pending_outbox_messages() -> dict[str, object]:
    rows = chat_memory.pending_outbox(settings.channel)
    for row in rows:
        send_chatwoot_outbound_message.apply_async((str(row["id"]),), queue="chatwoot_outbound")
    return {"ok": True, "dispatched": len(rows)}


@celery_app.task(name="app.tasks.chatwoot_tasks.requeue_stuck_conversation_jobs", queue="chatwoot_messages", **SWEEPER_RETRY)
def requeue_stuck_conversation_jobs() -> dict[str, object]:
    ids = [*chat_memory.due_job_conversation_ids(), *chat_memory.requeue_stale_jobs(settings.chatwoot_stale_processing_minutes)]
    for cid in set(ids):
        set_conversation_debounce(cid)
        process_chatwoot_conversation.apply_async((str(cid),), queue="chatwoot_messages", countdown=settings.chatwoot_debounce_seconds)
    return {"ok": True, "requeued": len(set(ids))}


@celery_app.task(name="app.tasks.chatwoot_tasks.cleanup_expired_locks", queue="chatwoot_messages", **SWEEPER_RETRY)
def cleanup_expired_locks() -> dict[str, object]:
    return {"ok": True, "cleaned": chat_memory.cleanup_expired_locks()}
