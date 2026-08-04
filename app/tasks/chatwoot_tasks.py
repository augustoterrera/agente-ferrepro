from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import redis

from app import chat_memory, notifier, retargeting
from app.celery_app import celery_app
from app.chatwoot import (
    ChatwootError,
    build_chatwoot_client,
    catalog_private_note,
    detect_handoff_flags,
    should_handoff_to_agent,
)
from app.chatwoot_service import process_pending_conversation_messages, sync_crm_labels
from app.classifier import STAGE_LABELS, classify
from app.config import settings
from app.meta import (
    MetaError,
    available_catalog_retailer_ids,
    product_list_payload,
    product_retailer_ids_by_product,
    send_whatsapp,
    single_product_payload,
)
from app.search import SearchError
from app.supabase import SupabaseError


def _chatwoot_client_for(conv):
    client = build_chatwoot_client(settings.chatwoot_url, settings.chatwoot_access_token)
    account_id = conv.account_id or settings.chatwoot_account_id
    return client, account_id


def _handoff_if_needed(client, account_id, conv, content: str) -> bool:
    if not should_handoff_to_agent(content):
        return False
    # En un handoff el clasificador no corre, así que las flags comerciales cuyas plantillas
    # derivan a humano (mayorista/negociacion/sin_stock) las deducimos de la respuesta.
    flags = detect_handoff_flags(content)
    current = client.get_conversation_labels(account_id, conv.external_conversation_id)
    labels = list(current)
    for label in (settings.bot_apagado_label, *flags):
        if label not in labels:
            labels.append(label)
    applied = client.set_conversation_labels(account_id, conv.external_conversation_id, labels)
    if settings.chatwoot_assignee_id is not None:
        client.assign_conversation(account_id, conv.external_conversation_id, settings.chatwoot_assignee_id)
    # Espejamos en el CRM el set final de labels para que el dashboard no quede desincronizado.
    sync_crm_labels(conv.external_conversation_id, applied or labels)
    return True

logger = logging.getLogger(__name__)

# Excepciones transitorias que justifican reintentar la task (red/DB/LLM caídos un momento).
RETRYABLE = (SupabaseError, SearchError, ChatwootError)

# Reintento para los sweepers del beat: un 502/blip transitorio de Supabase se cura solo en
# pocos segundos (1+2+4s). Solo alerta si sigue fallando tras los retries (outage real).
SWEEPER_RETRY = dict(
    autoretry_for=(*RETRYABLE, redis.RedisError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)


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
            # El gate bot_apagado se aplica en el webhook (labels vienen en el payload), así que
            # si llegamos acá es porque el bot está habilitado para esta conversación.
            chat_memory.update_jobs(conv.channel, conv.external_conversation_id, "processing", worker_id=worker_id(task_id))
            processed = process_pending_conversation_messages(int(conversation_id))
            outbox_id = processed[0] if processed else None
            if processed is not None:
                outbox_id, should_classify = processed
                send_chatwoot_outbound_message.apply_async((str(outbox_id),), queue="chatwoot_outbound")
                # Clasificar y etiquetar la conversación (async, no frena el reply). Corre también
                # en handoff para no perder etapa/flags si la derivación apaga el bot.
                if should_classify:
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
    if outbox["status"] in ("sent", "failed", "cancelled"):
        if outbox["status"] == "sent":
            conv = chat_memory.get_conversation(outbox["conversation_id"])
            client, account_id = _chatwoot_client_for(conv)
            if client and account_id:
                _handoff_if_needed(client, account_id, conv, outbox["content"])
        return {"ok": outbox["status"] == "sent", "outbox_id": outbox_id, "status": f"already_{outbox['status']}"}

    previous_status = str(outbox["status"])
    if not chat_memory.mark_outbox_processing(int(outbox_id), settings.chatwoot_stale_processing_minutes):
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
        is_retargeting = str(outbox.get("idempotency_key") or "").startswith("retargeting:")
        meta_plan = _meta_product_plan(outbox)
        # Entrega dudosa: ya hubo intentos, o el claim anterior quedó colgado en processing. En
        # los dos casos el POST pudo haber llegado y haberse perdido solo la confirmación.
        # Aplica a TODO el outbound, no solo al retargeting: recibir dos veces la misma respuesta
        # es igual de molesto. El chequeo cuesta una llamada y solo corre en estos casos raros.
        uncertain_delivery = bool(outbox["attempts"]) or previous_status == "processing"
        already_delivered = uncertain_delivery and client.has_outgoing_message(
            account_id,
            outbox["external_conversation_id"],
            outbox["content"],
            created_after=str(outbox.get("created_at") or ""),
            private=False,
        )
        if already_delivered:
            logger.info("outbox_ya_estaba_en_chatwoot", extra={"outbox_id": outbox_id})
            chat_memory.mark_outbox_sent(int(outbox_id), {"chatwoot": {"deduplicado": True}})
        else:
            # La cola pudo demorarse y el cliente contestar antes del POST. La RPC serializa este
            # chequeo contra el intake; si retomó, el follow-up pendiente se cancela.
            if is_retargeting and chat_memory.cancel_retargeting_if_resumed(int(outbox_id)):
                logger.info("retargeting_cancelado_por_respuesta", extra={"outbox_id": outbox_id})
                return {"ok": True, "outbox_id": outbox_id, "status": "cancelled"}
            meta_response = None
            catalog_already_delivered = False
            remaining_already_delivered = False
            if uncertain_delivery and meta_plan is not None and "error" not in meta_plan:
                catalog_already_delivered = client.has_outgoing_message(
                    account_id,
                    outbox["external_conversation_id"],
                    str(meta_plan["catalog_text"]),
                    created_after=str(outbox.get("created_at") or ""),
                    private=True,
                )
                remaining_text = meta_plan.get("remaining_text")
                remaining_already_delivered = not remaining_text or client.has_outgoing_message(
                    account_id,
                    outbox["external_conversation_id"],
                    str(remaining_text),
                    created_after=str(outbox.get("created_at") or ""),
                    private=False,
                )

            if meta_plan is not None and "error" not in meta_plan:
                if catalog_already_delivered:
                    meta_response = {"deduplicado": True}
                else:
                    meta_response = _send_meta_product_plan(meta_plan, outbox.get("id"))

            if meta_response is not None and "error" not in meta_response and meta_plan is not None:
                try:
                    note = (
                        {"deduplicado": True}
                        if catalog_already_delivered
                        else client.create_outgoing_message(
                            account_id,
                            outbox["external_conversation_id"],
                            catalog_private_note(str(meta_plan["catalog_text"])),
                            private=True,
                        )
                    )
                except ChatwootError as exc:
                    logger.warning("meta_catalog_private_note_failed", extra={"outbox_id": outbox_id, "error": str(exc)})
                    note = {"error": str(exc)[:500]}
                remaining_response = None
                if meta_plan.get("remaining_text") and not remaining_already_delivered:
                    remaining_response = client.create_outgoing_message(
                        account_id,
                        outbox["external_conversation_id"],
                        str(meta_plan["remaining_text"]),
                    )
                chat_memory.mark_outbox_sent(
                    int(outbox_id),
                    {
                        "meta": {
                            "response": meta_response,
                            "product_ids": meta_plan["catalog_product_ids"],
                        },
                        "chatwoot_private": note,
                        "chatwoot_remaining": remaining_response,
                    },
                )
            else:
                response = client.create_outgoing_message(
                    account_id, outbox["external_conversation_id"], outbox["content"]
                )
                chat_memory.mark_outbox_sent(int(outbox_id), {"chatwoot": response, "meta": meta_plan})
    except Exception as exc:
        status = chat_memory.mark_outbox_retry_or_failed(int(outbox_id), str(exc))
        if status in {"sent", "cancelled"}:
            handoff = (
                _handoff_if_needed(client, account_id, conv, outbox["content"])
                if status == "sent"
                else False
            )
            return {
                "ok": status == "sent",
                "outbox_id": outbox_id,
                "status": f"already_{status}",
                "handoff": handoff,
            }
        if status == "failed":
            raise
        raise self.retry(exc=exc)
    handoff = _handoff_if_needed(client, account_id, conv, outbox["content"])
    return {"ok": True, "outbox_id": outbox_id, "status": "sent", "handoff": handoff}


def _meta_product_plan(outbox: dict) -> dict[str, object] | None:
    raw = outbox.get("raw_payload") if isinstance(outbox.get("raw_payload"), dict) else {}
    phone = raw.get("customer_phone")
    product_ids = list(
        dict.fromkeys(int(pid) for pid in raw.get("meta_product_product_ids") or [] if str(pid).isdigit())
    )[:10]
    if not (phone and product_ids and settings.meta_access_token and settings.meta_phone_number_id and settings.meta_catalog_id):
        return None
    try:
        retailer_by_product = product_retailer_ids_by_product(product_ids)
        available_retailer_ids = available_catalog_retailer_ids(list(retailer_by_product.values()))
        catalog_product_ids = [
            product_id
            for product_id in product_ids
            if retailer_by_product.get(product_id) in available_retailer_ids
        ]
        if not catalog_product_ids:
            return None
        remaining_product_ids = [product_id for product_id in product_ids if product_id not in catalog_product_ids]
        catalog_text = str(outbox["content"])
        remaining_text = None
        if remaining_product_ids:
            product_urls = {
                int(product_id): str(url)
                for product_id, url in (raw.get("meta_product_urls") or {}).items()
                if str(product_id).isdigit() and url
            }
            split = _split_catalog_content(
                str(outbox["content"]), product_ids, product_urls, set(catalog_product_ids)
            )
            if split is None:
                return None
            catalog_text, remaining_text = split
        retailer_ids = [retailer_by_product[product_id] for product_id in catalog_product_ids]
        payload = (
            product_list_payload(phone, retailer_ids, body="Te dejo los productos para verlos en WhatsApp")
            if len(retailer_ids) > 1
            else single_product_payload(phone, retailer_ids[0], body="Te dejo el producto para verlo en WhatsApp")
        )
        return {
            "payload": payload,
            "catalog_product_ids": catalog_product_ids,
            "catalog_text": catalog_text,
            "remaining_text": remaining_text,
        }
    except (SupabaseError, MetaError) as exc:
        logger.warning("meta_catalog_plan_failed", extra={"outbox_id": outbox.get("id"), "error": str(exc)})
        return {"error": str(exc)[:500]}


def _send_meta_product_plan(plan: dict[str, object], outbox_id: object = None) -> dict[str, object]:
    try:
        return send_whatsapp(plan["payload"])
    except MetaError as exc:
        logger.warning("meta_catalog_message_failed", extra={"outbox_id": outbox_id, "error": str(exc)})
        return {"error": str(exc)[:500]}


def _split_catalog_content(
    content: str,
    product_ids: list[int],
    product_urls: dict[int, str],
    catalog_product_ids: set[int],
) -> tuple[str, str] | None:
    remaining_product_ids = set(product_ids) - catalog_product_ids
    if not remaining_product_ids:
        return content, ""
    if any(product_id not in product_urls for product_id in product_ids):
        return None

    blocks = content.split("\n\n")
    block_products = [
        {product_id for product_id in product_ids if product_urls[product_id].rstrip("/") in block}
        for block in blocks
    ]
    if set().union(*block_products) != set(product_ids):
        return None
    if any(ids & catalog_product_ids and ids & remaining_product_ids for ids in block_products):
        return None

    catalog_text = "\n\n".join(
        block for block, ids in zip(blocks, block_products) if not ids or ids <= catalog_product_ids
    ).strip()
    first_remaining = next(i for i, ids in enumerate(block_products) if ids & remaining_product_ids)
    remaining_blocks = [
        block
        for i, (block, ids) in enumerate(zip(blocks, block_products))
        if i >= first_remaining and (not ids or ids <= remaining_product_ids)
    ]
    remaining_text = "\n\n".join(["También tenemos estas opciones 👇", *remaining_blocks]).strip()
    return catalog_text, remaining_text


@celery_app.task(name="app.tasks.chatwoot_tasks.classify_and_label_conversation", queue="chatwoot_outbound")
def classify_and_label_conversation(conversation_id: str) -> dict[str, object]:
    """Clasifica etapa (curioso/interesado/compra) + flags comerciales y las setea en Chatwoot.
    La etapa REEMPLAZA a la anterior; las flags se ACUMULAN (sticky). Preserva bot_apagado y
    etiquetas manuales."""
    conv = chat_memory.get_conversation(int(conversation_id))
    client, account_id = _chatwoot_client_for(conv)
    if not client or not account_id:
        return {"ok": False, "status": "chatwoot_not_configured"}

    result = classify(chat_memory.recent_history(int(conversation_id), settings.chatwoot_history_limit))
    reactivado = _retargeting_reply_patch(conv)
    try:
        current = client.get_conversation_labels(account_id, conv.external_conversation_id)
        # Preservamos todo lo que no sea etapa: flags previas (sticky), bot_apagado y manuales.
        labels = [lbl for lbl in current if lbl not in STAGE_LABELS]
        extra = (settings.reactivado_label,) if reactivado else ()
        for label in (result.stage, *result.flags, *extra):
            if label not in labels:
                labels.append(label)
        applied = client.set_conversation_labels(account_id, conv.external_conversation_id, labels)
    except ChatwootError as exc:
        logger.warning("set_label_failed", extra={"conversation_id": conversation_id, "error": str(exc)})
        return {"ok": False, "status": "label_api_failed", "stage": result.stage}
    # Espejar en el CRM el set final de labels para que el dashboard quede sincronizado (fire-and-forget).
    sync_crm_labels(conv.external_conversation_id, applied or labels)
    chat_memory.save_classification(
        int(conversation_id),
        result.stage,
        list(result.flags),
        list(applied or labels),
        extra={"retargeting": reactivado} if reactivado else None,
    )
    return {"ok": True, "conversation_id": conversation_id, "stage": result.stage, "flags": result.flags}


def _retargeting_reply_patch(conv) -> dict[str, object] | None:
    """Si a esta conversación le mandamos un follow-up y ahora el cliente escribió, devuelve el
    objeto `retargeting` actualizado (merge shallow → hay que reescribirlo completo).

    El clasificador corre solo cuando el cliente escribe, y el follow-up no lo dispara, así que
    llegar acá con un follow-up enviado ya implica que contestó. El número fino del funnel lo
    calcula chat_retargeting_stats comparando timestamps."""
    rt = (conv.state or {}).get("retargeting")
    if not isinstance(rt, dict) or rt.get("decision") != "enviado" or rt.get("respondio"):
        return None
    return {**rt, "respondio": True}


# ── Sweepers del beat (red de seguridad de la cola) ─────────────────────────
@celery_app.task(name="app.tasks.chatwoot_tasks.retry_stale_processing_jobs", queue="chatwoot_messages", **SWEEPER_RETRY)
def retry_stale_processing_jobs() -> dict[str, object]:
    ids = chat_memory.requeue_stale_jobs(settings.chatwoot_stale_processing_minutes)
    for cid in ids:
        process_chatwoot_conversation.apply_async((str(cid),), queue="chatwoot_messages")
    return {"ok": True, "requeued": len(ids)}


@celery_app.task(name="app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages", queue="chatwoot_outbound", **SWEEPER_RETRY)
def dispatch_pending_outbox_messages() -> dict[str, object]:
    rows = chat_memory.pending_outbox(
        settings.channel,
        stale_minutes=settings.chatwoot_stale_processing_minutes,
    )
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


# ── Retargeting ─────────────────────────────────────────────────────────────
SWEEP_LOCK_KEY = "chatwoot:retargeting:sweep"


@celery_app.task(name="app.tasks.chatwoot_tasks.sweep_retargeting", queue="chatwoot_outbound", **SWEEPER_RETRY)
def sweep_retargeting() -> dict[str, object]:
    """Follow-up ÚNICO a leads que se colgaron a mitad de charla.

    Tres capas de filtro: la RPC (silencio + ventana de 24h de WhatsApp + nunca evaluada), las
    reglas de app/retargeting.py (etiquetas, derivación a humano) y un LLM que decide si vale la
    pena y escribe el mensaje con el contexto real. Solo dentro del horario comercial de Tucumán.
    """
    if not settings.retargeting_enabled:
        return {"ok": True, "status": "disabled"}
    if not retargeting.en_horario_de_envio():
        return {"ok": True, "status": "fuera_de_horario"}

    client = build_chatwoot_client(settings.chatwoot_url, settings.chatwoot_access_token)
    if client is None:
        return {"ok": False, "status": "chatwoot_no_configurado"}

    dry_run = settings.retargeting_dry_run
    # En dry-run marcamos en otra clave del state: así, al prender el envío real, las mismas
    # conversaciones siguen siendo candidatas (la simulación no las quema).
    state_key = "retargeting_dryrun" if dry_run else "retargeting"
    # Sin cupo NO cortamos acá: igual recorremos para contar a los que se pierden y avisar.
    cupo = max(0, settings.retargeting_daily_cap - chat_memory.retargeting_sent_count(settings.channel))

    with _sweep_lock() as acquired:
        if not acquired:
            return {"ok": True, "status": "ya_corriendo"}
        return _run_sweep(client, state_key=state_key, dry_run=dry_run, cupo=cupo)


def _run_sweep(client, *, state_key: str, dry_run: bool, cupo: int) -> dict[str, object]:
    resumen = {"candidatos": 0, "enviados": 0, "descartados": 0, "diferidos": 0, "fallidos": 0, "perdidos_por_cap": 0}
    candidates = chat_memory.retargeting_candidates(
        settings.channel,
        window_hours=settings.retargeting_window_hours,
        min_silence_hours=settings.retargeting_min_silence_hours,
        state_key=state_key,
        limit=settings.retargeting_batch_limit,
    )
    for cand in candidates:
        resumen["candidatos"] += 1
        if resumen["enviados"] >= cupo:
            # Sin cupo. Solo contamos como perdido al que ya no tiene NINGUNA corrida por delante
            # (es_perdida_definitiva, no momento_de_enviar): si todavía le queda ventana, el
            # próximo sweep lo agarra con el cupo del día siguiente y no hay nada que alertar.
            cierre = _parse_ts(cand.get("window_closes_at"))
            if cierre is not None and retargeting.es_perdida_definitiva(cierre):
                resumen["perdidos_por_cap"] += 1
            continue
        resumen[_procesar_candidato(client, cand, state_key=state_key, dry_run=dry_run)] += 1

    logger.info("retargeting_sweep", extra={**resumen, "dry_run": dry_run})
    _alertar_leads_perdidos(resumen, cupo=cupo)
    return {"ok": True, "dry_run": dry_run, **resumen}


def _alertar_leads_perdidos(resumen: dict[str, int], *, cupo: int) -> None:
    """Un lead que se pierde tiene que doler, no pasar en un log. Los dos casos son terminales:
    el candidato estaba en su última corrida antes de que cerrara la ventana de 24h de Meta, así
    que no hay próximo sweep que lo recupere."""
    perdidos = resumen["perdidos_por_cap"] + resumen["fallidos"]
    if not perdidos:
        return
    notifier.notify_error(
        f"retargeting: {perdidos} lead(s) quedaron sin contactar",
        detalle=(
            "Se les cerró la ventana de 24h de Meta sin follow-up: no se les puede escribir "
            "nunca más.\n"
            f"- {resumen['perdidos_por_cap']} por el cap diario → subí RETARGETING_DAILY_CAP "
            f"(cupo restante de hoy era {cupo}).\n"
            f"- {resumen['fallidos']} por fallas técnicas (Chatwoot, redactor o lock ocupado)."
        ),
        contexto={"candidatos": resumen["candidatos"], "enviados": resumen["enviados"]},
    )


def _procesar_candidato(client, cand: dict, *, state_key: str, dry_run: bool) -> str:
    """Evalúa un candidato y, si corresponde, encola el follow-up.
    Devuelve 'enviados', 'descartados' o 'diferidos' (para el resumen del sweep)."""
    cid = int(cand["conversation_id"])
    state = cand.get("state") or {}

    # ¿Es esta la última corrida antes de que cierre la ventana de Meta? Si todavía queda otra,
    # esperamos: el recontacto se manda lo más tarde posible. Va primero de todo porque descarta
    # a casi todos los candidatos y no cuesta nada. Sin marcar: siguen siendo candidatos.
    cierre = _parse_ts(cand.get("window_closes_at"))
    if cierre is None or not retargeting.momento_de_enviar(cierre):
        return "diferidos"

    # Pre-filtro gratis con las etiquetas espejadas en el state por el clasificador.
    motivo = retargeting.veto_por_etiquetas(retargeting.state_labels(state))
    if motivo:
        return _descartar(cid, state_key, motivo)

    # Verdad al momento del envío: un humano pudo tomar la conversación (bot_apagado) después del
    # último mensaje, y ahí no nos metemos. Se chequea antes del LLM para no gastar tokens.
    account_id = cand.get("account_id") or settings.chatwoot_account_id
    try:
        labels = client.get_conversation_labels(account_id, cand["external_conversation_id"])
    except ChatwootError as exc:
        # Sin marcar: se reintenta. Solo es pérdida (y alerta) si ya no queda ninguna corrida.
        logger.warning("retargeting_labels_failed", extra={"conversation_id": cid, "error": str(exc)})
        return "fallidos" if retargeting.es_perdida_definitiva(cierre) else "diferidos"
    motivo = retargeting.veto_por_etiquetas(labels)
    if motivo:
        return _descartar(cid, state_key, motivo)

    history = chat_memory.recent_history(cid, settings.chatwoot_history_limit)
    stage = retargeting.state_stage(state)
    motivo = retargeting.veto_por_conversacion(history, stage)
    if motivo:
        return _descartar(cid, state_key, motivo)

    # El redactor tarda segundos: una falla técnica acá NO descarta al lead (se reintenta).
    try:
        followup = retargeting.compose_followup(
            history,
            nombre=retargeting.contact_name(chat_memory.last_user_payload(cid)),
            stage=stage,
            flags=retargeting.state_flags(state),
        )
    except retargeting.RedactorError as exc:
        logger.warning("retargeting_redactor_failed", extra={"conversation_id": cid, "error": str(exc)})
        return "fallidos" if retargeting.es_perdida_definitiva(cierre) else "diferidos"
    if not (followup.vale_la_pena and followup.mensaje):
        return _descartar(cid, state_key, followup.motivo)

    if dry_run:
        chat_memory.mark_retargeting(cid, state_key, "enviado", followup.motivo, followup.mensaje)
        logger.info("retargeting_simulado", extra={"conversation_id": cid, "mensaje": followup.mensaje})
        return "enviados"

    # Todo lo anterior (Chatwoot + LLM) llevó segundos sobre una foto de la DB, y el cliente pudo
    # contestar en el medio. La RPC comparte el lock de conversación con el intake y confirma
    # validación + outbox + estado en una transacción (ver migración 002).
    resultado = chat_memory.commit_retargeting(
        conversation_id=cid,
        external_conversation_id=cand["external_conversation_id"],
        channel=cand["channel"],
        content=followup.mensaje,
        idempotency_key=f"retargeting:{cid}",
        state_key=state_key,
        motivo=followup.motivo,
        last_assistant_at=str(cand.get("last_assistant_at") or ""),
    )
    estado = str(resultado.get("status") or "")
    if estado == "retomada":
        logger.info("retargeting_conversacion_retomada", extra={"conversation_id": cid})
        return _descartar(cid, state_key, "el cliente volvió a escribir mientras redactábamos")
    if estado not in {"creado", "ya_existia"}:
        raise SupabaseError(f"chat_retargeting_commit devolvió status inválido: {estado!r}")

    outbox_id = resultado.get("outbox_id")
    if outbox_id is None:
        raise SupabaseError("chat_retargeting_commit no devolvió outbox_id")
    send_chatwoot_outbound_message.apply_async((str(outbox_id),), queue="chatwoot_outbound")
    logger.info("retargeting_encolado", extra={"conversation_id": cid, "motivo": followup.motivo, "estado": estado})
    return "enviados"


def _parse_ts(value: object) -> datetime | None:
    """Timestamptz de PostgREST → datetime aware. Si viene raro, None (el candidato se difiere)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("retargeting_timestamp_invalido", extra={"value": value})
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _descartar(conversation_id: int, state_key: str, motivo: str) -> str:
    chat_memory.mark_retargeting(conversation_id, state_key, "descartado", motivo)
    logger.info("retargeting_descartado", extra={"conversation_id": conversation_id, "motivo": motivo})
    return "descartados"


@contextmanager
def _sweep_lock() -> Iterator[bool]:
    """Evita que dos sweeps solapados paguen dos veces las llamadas al LLM (el envío ya es
    idempotente por el idempotency_key del outbox). Sin Redis se frena: el cap diario es una
    barrera de seguridad y no conviene degradarlo a múltiples sweeps concurrentes."""
    client = _redis()
    value = worker_id()
    acquired = bool(client.set(SWEEP_LOCK_KEY, value, nx=True, ex=600))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if client.get(SWEEP_LOCK_KEY) == value:
                    client.delete(SWEEP_LOCK_KEY)
            except redis.RedisError:
                logger.warning("retargeting_sweep_lock_release_failed")
