from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from . import chat_memory, media, supabase
from .agent import AgentReply, run_agent_reply
from .chatwoot import (
    ChatwootMessageEvent,
    chatwoot_contact_id,
    chatwoot_contact_phone,
    is_handoff_acceptance,
    message_attachments,
)
from .config import settings

logger = logging.getLogger(__name__)
HANDOFF_ACCEPTED_REPLY = "Perfecto, te derivo con un vendedor de FerrePro para coordinarlo."


def chatwoot_event_key(headers: dict[str, str | None], conversation_id: int | str, message_id: int | str | None) -> str:
    delivery_id = headers.get("x-chatwoot-delivery")
    if delivery_id:
        return f"delivery:{delivery_id}"
    return f"message:{conversation_id}:{message_id}"


def outbox_idempotency_key(conversation_id: int | str, message_ids: list[int], content: str) -> str:
    digest = hashlib.sha256(
        json.dumps({"c": str(conversation_id), "m": message_ids, "t": content}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"chatwoot:{conversation_id}:{digest}"


def sync_crm_contact(conversation_external_id: int | str | None, payload: dict[str, Any]) -> None:
    """Registra/actualiza el contacto en el CRM (crm_contacts) — basta con el primer mensaje.
    Upsert idempotente por teléfono; no toca los flags llamada/venta. Fire-and-forget: corre en
    el worker (no en el webhook) y un Supabase caído no debe afectar la respuesta al cliente."""
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    phone = str(sender.get("phone_number") or contact.get("phone_number") or "").strip()
    if not phone:
        return

    name = str(sender.get("name") or contact.get("name") or "").strip() or None
    try:
        conversation_id = int(conversation_external_id)
    except (TypeError, ValueError):
        conversation_id = None

    try:
        supabase.rpc(
            "crm_set_contact_status",
            {
                "p_phone": phone,
                "p_name": name,
                "p_llamada": None,
                "p_venta": None,
                "p_conversation_id": conversation_id,
                "p_conversation_display_id": None,
            },
        )
    except supabase.SupabaseError as exc:
        logger.warning("crm_contact_sync_failed", extra={"conversation_id": conversation_id, "error": str(exc)})


def sync_crm_labels(conversation_external_id: int | str, labels: list[str]) -> None:
    """Espeja en crm_contacts TODAS las etiquetas de la conversación (unidas por coma, como el
    cached_label_list de Chatwoot) para que el dashboard quede sincronizado con Chatwoot. Keyed por
    conversation_id (el contacto ya fue registrado por sync_crm_contact). Fire-and-forget."""
    try:
        supabase.update(
            "crm_contacts",
            f"conversation_id=eq.{conversation_external_id}",
            {"conversation_labels": ",".join(labels)},
        )
    except supabase.SupabaseError as exc:
        logger.warning("crm_label_sync_failed", extra={"conversation_id": conversation_external_id, "error": str(exc)})


def persist_incoming_chatwoot_event(
    event_key: str, event: ChatwootMessageEvent, payload: dict[str, Any]
) -> tuple[bool, chat_memory.Conversation, int | None]:
    """Intake atómico. Devuelve (es_nuevo, conversación, job_id)."""
    is_new, conversation, job_id = chat_memory.persist_incoming_event(
        event_key=event_key,
        channel=settings.channel,
        external_conversation_id=event.conversation_id,
        external_contact_id=chatwoot_contact_id(payload),
        account_id=event.account_id or (str(settings.chatwoot_account_id) if settings.chatwoot_account_id else None),
        external_message_id=str(event.message_id) if event.message_id is not None else None,
        content=event.content,
        raw_payload=payload,
        max_attempts=settings.chatwoot_job_max_retries,
    )
    if is_new:
        logger.info(
            "chatwoot_webhook_queued",
            extra={"event_key": event_key, "conversation_id": conversation.id, "job_id": job_id},
        )
    return is_new, conversation, job_id


def process_pending_conversation_messages(conversation_id: int) -> int | None:
    """Junta los mensajes pending del cliente, corre el agente y deja la respuesta en el outbox.
    Devuelve el id del outbox a enviar, o None si no hubo nada que responder."""
    conv = chat_memory.get_conversation(conversation_id)
    pending = chat_memory.pending_messages(conversation_id)
    if not pending:
        chat_memory.update_jobs(conv.channel, conv.external_conversation_id, "completed")
        chat_memory.update_events(conv.channel, conv.external_conversation_id, "completed")
        return None

    # Registrar el contacto en el CRM (idempotente, fire-and-forget). Acá y no en el webhook:
    # mantiene Supabase fuera del camino crítico de intake.
    sync_crm_contact(conv.external_conversation_id, pending[-1].get("raw_payload") or {})

    message_ids = [m["id"] for m in pending]
    user_content, images, audio_transcripts = _collect_inputs(pending)
    if not user_content and not images:
        chat_memory.mark_messages_processed(message_ids)
        chat_memory.update_jobs(conv.channel, conv.external_conversation_id, "skipped")
        chat_memory.update_events(conv.channel, conv.external_conversation_id, "completed")
        return None

    history = chat_memory.recent_history(conversation_id, settings.chatwoot_history_limit, exclude_ids=set(message_ids))

    if not images and is_handoff_acceptance(user_content, history):
        reply = AgentReply(HANDOFF_ACCEPTED_REPLY, [])
    else:
        # Si el agente falla, propagamos: el estado retry/failed del job lo decide la task de
        # Celery según los reintentos. Los mensajes siguen en pending (nunca los marcamos
        # processing), así el retry los reprocesa.
        reply = run_agent_reply(user_content, history, images=images or None)
    answer = reply.text

    # Si el cliente escribió MIENTRAS generábamos, esta respuesta quedó vieja: dejamos los
    # mensajes en pending (nunca los marcamos processing) y cerramos el job. La task del
    # mensaje nuevo reprocesa todo junto y responde una sola vez.
    superseding = [m for m in chat_memory.pending_messages(conversation_id) if m["id"] not in set(message_ids)]
    if superseding:
        chat_memory.update_jobs(conv.channel, conv.external_conversation_id, "completed")
        chat_memory.update_events(conv.channel, conv.external_conversation_id, "completed")
        logger.info("chatwoot_turn_superseded", extra={"conversation_id": conversation_id})
        return None

    # Guardar transcripciones en sus mensajes (trazabilidad) solo en el camino exitoso,
    # para no re-transcribir si el turno se supersede.
    for message_id, transcript in audio_transcripts:
        chat_memory.set_message_content(message_id, transcript)

    chat_memory.add_message(conversation_id, "assistant", answer, processing_status="processed")
    chat_memory.mark_messages_processed(message_ids)
    outbox = chat_memory.create_outbox(
        conversation_id, conv.external_conversation_id, conv.channel, answer,
        outbox_idempotency_key(conv.external_conversation_id, message_ids, answer),
        raw_payload={
            "meta_product_product_ids": reply.product_ids,
            "customer_phone": chatwoot_contact_phone(pending[-1].get("raw_payload") or {}),
        },
    )
    chat_memory.update_jobs(conv.channel, conv.external_conversation_id, "completed")
    chat_memory.update_events(conv.channel, conv.external_conversation_id, "completed")
    return outbox["id"] if outbox else None


def _collect_inputs(
    pending: list[dict],
) -> tuple[str, list[tuple[bytes, str]], list[tuple[int, str]]]:
    """De los mensajes pending arma: texto (caption + transcripciones de audio), imágenes
    (bytes + media_type para visión) y las transcripciones a guardar luego."""
    text_parts: list[str] = []
    images: list[tuple[bytes, str]] = []
    audio_transcripts: list[tuple[int, str]] = []

    for m in pending:
        if (m.get("content") or "").strip():
            text_parts.append(m["content"].strip())
        for att in message_attachments(m.get("raw_payload") or {}):
            if att["type"] == "audio":
                try:
                    transcript = media.transcribe(media.download(att["url"]), extension=att.get("extension"))
                except media.MediaError:
                    transcript = ""
                if transcript:
                    text_parts.append(transcript)
                    audio_transcripts.append((m["id"], transcript))
                else:
                    text_parts.append("(el cliente envió un audio que no se pudo entender)")
            elif att["type"] == "image":
                try:
                    data = media.download(att["url"])
                    images.append((data, media.image_media_type(att.get("extension"), data)))
                except media.MediaError:
                    text_parts.append("(el cliente envió una imagen que no se pudo abrir)")
            else:
                text_parts.append(f"(el cliente envió un archivo adjunto: {att['type']})")

    user_content = "\n".join(text_parts).strip()
    if not user_content and images:
        user_content = "El cliente envió una o más imágenes. Miralas y ayudalo con lo que muestran."
    return user_content, images, audio_transcripts
