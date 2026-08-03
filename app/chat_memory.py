from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import supabase
from .models import AgentMessage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

# Store de memoria conversacional sobre Supabase REST. La lógica atómica (upsert de
# conversación, lock pesimista, dedup de eventos, estado de jobs) vive en funciones SQL
# (security definer) que invocamos como RPC: una llamada = una transacción atómica en el
# server. El CRUD simple (mensajes, outbox, historial) va por el table API de PostgREST.
# Sin maquinaria de slots: el diseño es search-and-present, el estado lo lleva el historial.


@dataclass
class Conversation:
    id: int
    channel: str
    external_conversation_id: str
    account_id: str | None = None
    state: dict[str, Any] | None = None


def _q(value: Any) -> str:
    """Escapa un valor para un filtro PostgREST (eq.<value>)."""
    return str(value)


# ── Entrada (webhook) ──────────────────────────────────────────────────────
def persist_incoming_event(
    *,
    event_key: str,
    channel: str,
    external_conversation_id: str,
    external_contact_id: str | None,
    account_id: str | None,
    external_message_id: str | None,
    content: str,
    raw_payload: dict,
    max_attempts: int,
) -> tuple[bool, Conversation, int | None]:
    """Dedup + conversación + mensaje + job en una sola transacción."""
    result = supabase.rpc(
        "chat_persist_incoming_event",
        {
            "p_event_key": event_key,
            "p_channel": channel,
            "p_external_conversation_id": external_conversation_id,
            "p_external_contact_id": external_contact_id,
            "p_account_id": account_id,
            "p_external_message_id": external_message_id,
            "p_content": content,
            "p_raw_payload": raw_payload or {},
            "p_max_attempts": max_attempts,
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("conversation"), dict):
        raise supabase.SupabaseError("chat_persist_incoming_event devolvió una respuesta inválida")
    job_id = result.get("job_id")
    return bool(result.get("is_new")), _conversation(result["conversation"]), int(job_id) if job_id is not None else None


def mark_event_received(
    event_key: str, channel: str, external_conversation_id: str, external_message_id: str | None, raw_payload: dict
) -> bool:
    """True si es nuevo; False si ya se procesó (dedup por event_key)."""
    return bool(
        supabase.rpc(
            "mark_chat_event_received",
            {
                "p_event_key": event_key,
                "p_channel": channel,
                "p_external_conversation_id": external_conversation_id,
                "p_external_message_id": external_message_id,
                "p_raw_payload": raw_payload or {},
            },
        )
    )


def update_event_status(event_key: str, status: str, error: str | None = None) -> None:
    supabase.rpc("update_chat_event_status", {"p_event_key": event_key, "p_status": status, "p_error": error})


def get_or_create_conversation(
    channel: str, external_conversation_id: str, external_contact_id: str | None = None, account_id: str | None = None
) -> Conversation:
    row = supabase.rpc(
        "get_or_create_chat_conversation",
        {
            "p_channel": channel,
            "p_external_conversation_id": external_conversation_id,
            "p_external_contact_id": external_contact_id,
            "p_account_id": account_id,
        },
    )
    # La función devuelve la fila (PostgREST la entrega como objeto o lista de 1).
    if isinstance(row, list):
        row = row[0]
    return _conversation(row)


def enqueue_webhook_job(
    event_key: str, channel: str, external_conversation_id: str, external_message_id: str | None, raw_payload: dict
) -> int:
    return int(
        supabase.rpc(
            "enqueue_chat_webhook_job",
            {
                "p_event_key": event_key,
                "p_channel": channel,
                "p_external_conversation_id": external_conversation_id,
                "p_external_message_id": external_message_id,
                "p_raw_payload": raw_payload or {},
            },
        )
    )


def update_job_status(job_id: int, status: str, error: str | None = None) -> None:
    supabase.rpc("update_chat_webhook_job_status", {"p_job_id": job_id, "p_status": status, "p_error": error})


def update_jobs(channel: str, external_conversation_id: str, status: str, *, error: str | None = None, worker_id: str | None = None) -> None:
    """Transición de estado de los jobs activos de una conversación (queued/processing/retry)."""
    patch: dict[str, Any] = {"status": status}
    if error is not None:
        patch["error"] = error[:500]
    if status == "processing":
        patch["started_at"] = _now()
        patch["locked_at"] = _now()
        if worker_id:
            patch["worker_id"] = worker_id
    if status in ("completed", "failed", "skipped"):
        patch["finished_at"] = _now()
        patch["completed_at"] = _now()
    supabase.update(
        "chat_webhook_jobs",
        f"channel=eq.{channel}&external_conversation_id=eq.{external_conversation_id}&status=in.(queued,processing,retry)",
        patch,
    )


def update_events(channel: str, external_conversation_id: str, status: str, *, error: str | None = None) -> None:
    patch: dict[str, Any] = {"status": status}
    if error is not None:
        patch["error"] = error[:500]
    supabase.update(
        "chat_processed_events",
        f"channel=eq.{channel}&external_conversation_id=eq.{external_conversation_id}&status=in.(received,processing)",
        patch,
    )


def get_conversation(conversation_id: int) -> Conversation:
    rows = supabase.select("chat_conversations", f"id=eq.{conversation_id}&limit=1")
    if not rows:
        raise ValueError(f"conversación {conversation_id} no existe")
    return _conversation(rows[0])


# ── Mensajes ────────────────────────────────────────────────────────────────
def add_message(
    conversation_id: int,
    role: str,
    content: str,
    *,
    external_message_id: str | None = None,
    processing_status: str = "processed",
    raw_payload: dict | None = None,
    tool_calls: list | None = None,
) -> None:
    supabase.insert(
        "chat_messages",
        {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "external_message_id": external_message_id,
            "processing_status": processing_status,
            "raw_payload": raw_payload or {},
            "tool_calls": tool_calls or [],
        },
        return_row=False,
    )


def pending_messages(conversation_id: int, limit: int = 50) -> list[dict]:
    return supabase.select(
        "chat_messages",
        f"conversation_id=eq.{conversation_id}&processing_status=eq.pending&role=eq.user"
        f"&order=created_at.asc&limit={limit}",
    )


def set_message_content(message_id: int, content: str) -> None:
    """Reemplaza el contenido de un mensaje (ej. guardar la transcripción de un audio)."""
    supabase.update("chat_messages", f"id=eq.{message_id}", {"content": content})


def mark_messages_processed(message_ids: list[int]) -> None:
    if not message_ids:
        return
    ids = ",".join(str(i) for i in message_ids)
    supabase.update("chat_messages", f"id=in.({ids})", {"processing_status": "processed"})


def recent_history(conversation_id: int, limit: int = 16, exclude_ids: set[int] | None = None) -> list[AgentMessage]:
    rows = supabase.select(
        "chat_messages",
        f"conversation_id=eq.{conversation_id}&role=in.(user,assistant)&order=created_at.desc&limit={limit + len(exclude_ids or [])}",
    )
    rows.reverse()
    exclude = exclude_ids or set()
    return [AgentMessage(role=r["role"], content=r["content"]) for r in rows if r["id"] not in exclude][-limit:]


# ── Outbox ──────────────────────────────────────────────────────────────────
def create_outbox(
    conversation_id: int,
    external_conversation_id: str,
    channel: str,
    content: str,
    idempotency_key: str,
    raw_payload: dict | None = None,
) -> dict | None:
    # idempotency_key único: si ya existe (reintento del turno), PostgREST devuelve 409.
    # Lo tratamos como idempotente: devolvemos el outbox existente en vez de duplicar el envío.
    try:
        return supabase.insert(
            "chat_outbox_messages",
            {
                "conversation_id": conversation_id,
                "external_conversation_id": external_conversation_id,
                "channel": channel,
                "content": content,
                "idempotency_key": idempotency_key,
                "raw_payload": raw_payload or {},
            },
        )
    except supabase.SupabaseError as exc:
        if "409" not in str(exc):
            raise
        existing = supabase.select("chat_outbox_messages", f"idempotency_key=eq.{idempotency_key}&limit=1")
        return existing[0] if existing else None


def get_outbox(outbox_id: int) -> dict | None:
    rows = supabase.select("chat_outbox_messages", f"id=eq.{outbox_id}&limit=1")
    return rows[0] if rows else None


def mark_outbox_processing(outbox_id: int, stale_minutes: int = 15) -> bool:
    """Claim atómico; permite recuperar un processing cuyo lease venció."""
    return bool(
        supabase.rpc(
            "chat_claim_outbox",
            {"p_outbox_id": outbox_id, "p_stale_minutes": stale_minutes},
        )
    )


def pending_outbox(channel: str, limit: int = 100, stale_minutes: int = 15) -> list[dict]:
    return list(
        supabase.rpc(
            "chat_due_outbox_messages",
            {
                "p_channel": channel,
                "p_stale_minutes": stale_minutes,
                "p_limit": limit,
            },
        )
        or []
    )


def mark_outbox_sent(outbox_id: int, raw_payload: dict | None = None) -> None:
    supabase.rpc(
        "chat_mark_outbox_sent",
        {"p_outbox_id": outbox_id, "p_raw_payload": raw_payload or {}},
    )


def mark_outbox_retry_or_failed(outbox_id: int, error: str) -> str:
    return str(
        supabase.rpc(
            "chat_fail_outbox_attempt",
            {"p_outbox_id": outbox_id, "p_error": error},
        )
        or "failed"
    )


def cancel_retargeting_if_resumed(outbox_id: int) -> bool:
    return bool(supabase.rpc("chat_cancel_retargeting_if_resumed", {"p_outbox_id": outbox_id}))


# ── Locks ───────────────────────────────────────────────────────────────────
def acquire_lock(channel: str, external_conversation_id: str, lock_seconds: int = 60) -> bool:
    return bool(
        supabase.rpc(
            "acquire_chat_conversation_lock",
            {
                "p_channel": channel,
                "p_external_conversation_id": external_conversation_id,
                "p_lock_seconds": lock_seconds,
            },
        )
    )


def release_lock(channel: str, external_conversation_id: str) -> None:
    supabase.rpc(
        "release_chat_conversation_lock",
        {"p_channel": channel, "p_external_conversation_id": external_conversation_id},
    )


# ── Sweepers (beat) ──────────────────────────────────────────────────────────
def requeue_stale_jobs(stale_minutes: int = 15, limit: int = 100) -> list[int]:
    return list(supabase.rpc("requeue_stale_chat_webhook_jobs", {"p_stale_minutes": stale_minutes, "p_limit": limit}) or [])


def due_job_conversation_ids(limit: int = 100) -> list[int]:
    return list(supabase.rpc("due_chat_webhook_job_conversations", {"p_limit": limit}) or [])


def cleanup_expired_locks() -> int:
    return int(supabase.rpc("cleanup_expired_chat_conversation_locks", {}) or 0)


# ── Estado de la conversación ───────────────────────────────────────────────
def merge_state(conversation_id: int, patch: dict[str, Any]) -> None:
    """Merge shallow sobre chat_conversations.state (jsonb ||): no pisa las otras claves."""
    supabase.rpc("chat_merge_conversation_state", {"p_conversation_id": conversation_id, "p_patch": patch})


def save_classification(
    conversation_id: int, stage: str, flags: list[str], labels: list[str], extra: dict[str, Any] | None = None
) -> None:
    """Espeja etapa/flags/labels en el state. Es la fuente barata del pre-filtro de retargeting:
    sin esto, elegir candidatos exigiría una llamada a la API de Chatwoot por conversación."""
    patch: dict[str, Any] = {
        "stage": stage,
        "flags": list(flags),
        "labels": list(labels),
        "classified_at": _now(),
    }
    merge_state(conversation_id, {**patch, **(extra or {})})


def last_user_payload(conversation_id: int) -> dict:
    """raw_payload del último mensaje del cliente (de ahí sale el nombre del contacto)."""
    rows = supabase.select(
        "chat_messages",
        f"conversation_id=eq.{conversation_id}&role=eq.user&order=created_at.desc&limit=1&select=raw_payload",
    )
    return (rows[0].get("raw_payload") or {}) if rows else {}


# ── Retargeting ─────────────────────────────────────────────────────────────
def retargeting_candidates(
    channel: str,
    *,
    window_hours: float,
    min_silence_hours: float,
    state_key: str,
    limit: int,
) -> list[dict]:
    """Capa 1 de la selección: el bot habló último, el cliente calló, la ventana de 24h de
    WhatsApp sigue abierta y la conversación nunca fue evaluada. Vienen ordenados por ventana
    más próxima a cerrar; cuál mandar AHORA lo decide momento_de_enviar(). Ver migración 002."""
    return list(
        supabase.rpc(
            "chat_retargeting_candidates",
            {
                "p_channel": channel,
                "p_window_hours": window_hours,
                "p_min_silence_hours": min_silence_hours,
                "p_state_key": state_key,
                "p_limit": limit,
            },
        )
        or []
    )


def commit_retargeting(
    *,
    conversation_id: int,
    external_conversation_id: str,
    channel: str,
    content: str,
    idempotency_key: str,
    state_key: str,
    motivo: str,
    last_assistant_at: str,
) -> dict:
    """Encola el follow-up de forma atómica: revalida que nadie haya escrito después del último
    mensaje del bot y, en la misma transacción, crea el outbox + la marca de estado. El historial
    se agrega recién al confirmar la entrega. Devuelve {status: creado|ya_existia|retomada,
    outbox_id}. Ver migración 002."""
    resultado = supabase.rpc(
        "chat_retargeting_commit",
        {
            "p_conversation_id": conversation_id,
            "p_external_conversation_id": str(external_conversation_id),
            "p_channel": channel,
            "p_content": content,
            "p_idempotency_key": idempotency_key,
            "p_state_key": state_key,
            "p_motivo": motivo,
            "p_last_assistant_at": last_assistant_at,
        },
    )
    return resultado if isinstance(resultado, dict) else {}


def mark_retargeting(conversation_id: int, state_key: str, decision: str, motivo: str, mensaje: str | None = None) -> None:
    """Marca decisiones sin envío: descartes y resultados del dry-run."""
    merge_state(
        conversation_id,
        {state_key: {"decision": decision, "motivo": motivo[:300], "mensaje": mensaje, "at": _now()}},
    )


def retargeting_sent_count(channel: str, hours: float = 24) -> int:
    return int(supabase.rpc("chat_retargeting_sent_count", {"p_channel": channel, "p_hours": hours}) or 0)


def retargeting_stats(channel: str) -> dict:
    return supabase.rpc("chat_retargeting_stats", {"p_channel": channel}) or {}


def _conversation(row: dict[str, Any]) -> Conversation:
    return Conversation(
        id=int(row["id"]),
        channel=row["channel"],
        external_conversation_id=str(row["external_conversation_id"]),
        account_id=row.get("account_id"),
        state=row.get("state"),
    )


if __name__ == "__main__":
    # Self-check vivo contra Supabase: crea una conversación de prueba, ejercita el flujo
    # y limpia al final. Channel propio para no tocar data real.
    from .config import settings

    if not (settings.supabase_url and settings.supabase_service_key):
        print("self-check: SALTEADO (faltan SUPABASE_*)")
        raise SystemExit

    ch, ext = "selftest", "conv-selfcheck-1"
    supabase.delete("chat_processed_events", f"channel=eq.{ch}")  # limpieza previa idempotente
    supabase.delete("chat_conversations", f"channel=eq.{ch}")

    conv = get_or_create_conversation(ch, ext, account_id="test")
    assert conv.id and conv.channel == ch
    conv2 = get_or_create_conversation(ch, ext)  # idempotente
    assert conv2.id == conv.id

    assert mark_event_received("evt-1", ch, ext, "m1", {"x": 1}) is True
    assert mark_event_received("evt-1", ch, ext, "m1", {"x": 1}) is False  # dedup

    add_message(conv.id, "user", "tenés taladros?", external_message_id="m1", processing_status="pending")
    pend = pending_messages(conv.id)
    assert len(pend) == 1 and pend[0]["content"] == "tenés taladros?"

    assert acquire_lock(ch, ext, 30) is True
    assert acquire_lock(ch, ext, 30) is False  # ya bloqueado
    release_lock(ch, ext)
    assert acquire_lock(ch, ext, 30) is True
    release_lock(ch, ext)

    add_message(conv.id, "assistant", "Sí, mirá estas opciones...")
    mark_messages_processed([m["id"] for m in pend])
    assert pending_messages(conv.id) == []
    hist = recent_history(conv.id)
    assert [m.role for m in hist] == ["user", "assistant"]

    ob = create_outbox(conv.id, ext, ch, "Sí, mirá estas opciones...", "idem-1")
    assert ob and ob["status"] == "pending"
    assert len(pending_outbox(ch)) == 1
    assert mark_outbox_processing(ob["id"]) is True  # mark_outbox_sent exige el claim previo
    mark_outbox_sent(ob["id"])
    assert pending_outbox(ch) == []

    # Intake atómico (migración 002): es el camino de TODOS los mensajes entrantes, así que si
    # esta RPC no está aplicada en Supabase el bot deja de responder. Se valida acá a propósito.
    ext2 = "conv-selfcheck-2"
    is_new, conv3, job_id = persist_incoming_event(
        event_key="evt-intake-1", channel=ch, external_conversation_id=ext2,
        external_contact_id="c1", account_id="test", external_message_id="m9",
        content="tenés amoladoras?", raw_payload={"x": 1}, max_attempts=5,
    )
    assert is_new is True and conv3.id and job_id
    repetido, conv4, job2 = persist_incoming_event(  # mismo delivery → dedup, sin job nuevo
        event_key="evt-intake-1", channel=ch, external_conversation_id=ext2,
        external_contact_id="c1", account_id="test", external_message_id="m9",
        content="tenés amoladoras?", raw_payload={"x": 1}, max_attempts=5,
    )
    assert repetido is False and conv4.id == conv3.id and job2 is None
    pend2 = pending_messages(conv3.id)
    assert len(pend2) == 1 and pend2[0]["content"] == "tenés amoladoras?", pend2

    # Estado de la conversación (lo usa el clasificador en cada turno y el retargeting).
    merge_state(conv3.id, {"stage": "interesado", "labels": ["interesado"]})
    merge_state(conv3.id, {"flags": []})
    assert get_conversation(conv3.id).state.get("stage") == "interesado"  # el merge no pisa
    assert retargeting_sent_count(ch) == 0
    assert isinstance(retargeting_stats(ch), dict)
    assert isinstance(retargeting_candidates(
        ch, window_hours=24, min_silence_hours=2, state_key="retargeting", limit=5
    ), list)

    # limpieza
    supabase.delete("chat_processed_events", f"channel=eq.{ch}")
    supabase.delete("chat_conversations", f"channel=eq.{ch}")
    print("self-check vivo: OK (chat_memory REST end-to-end)")
