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
    conversation_id: int, external_conversation_id: str, channel: str, content: str, idempotency_key: str
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


def mark_outbox_processing(outbox_id: int) -> bool:
    """Reclama el outbox para envío. False si otro worker ya lo tomó (claim atómico)."""
    rows = supabase.patch_returning(
        "chat_outbox_messages", f"id=eq.{outbox_id}&status=in.(pending,retry)", {"status": "processing"}
    )
    return bool(rows)


def pending_outbox(channel: str, limit: int = 100) -> list[dict]:
    # Scopeado por channel: aunque la DB es propia de Ferrepro, no asumimos exclusividad.
    return supabase.select(
        "chat_outbox_messages",
        f"channel=eq.{channel}&status=in.(pending,retry)&order=created_at.asc&limit={limit}",
    )


def mark_outbox_sent(outbox_id: int, raw_payload: dict | None = None) -> None:
    supabase.update(
        "chat_outbox_messages",
        f"id=eq.{outbox_id}",
        {"status": "sent", "sent_at": _now(), "raw_payload": raw_payload or {}},
    )


def mark_outbox_retry_or_failed(outbox_id: int, error: str, max_attempts: int = 5) -> str:
    supabase.rpc("increment_chat_outbox_attempts", {"p_outbox_id": outbox_id})
    rows = supabase.select("chat_outbox_messages", f"id=eq.{outbox_id}&select=attempts,max_attempts")
    attempts = rows[0]["attempts"] if rows else max_attempts
    cap = rows[0]["max_attempts"] if rows else max_attempts
    status = "failed" if attempts >= cap else "retry"
    supabase.update("chat_outbox_messages", f"id=eq.{outbox_id}", {"status": status, "error": error[:500]})
    return status


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
    mark_outbox_sent(ob["id"])
    assert pending_outbox(ch) == []

    # limpieza
    supabase.delete("chat_processed_events", f"channel=eq.{ch}")
    supabase.delete("chat_conversations", f"channel=eq.{ch}")
    print("self-check vivo: OK (chat_memory REST end-to-end)")
