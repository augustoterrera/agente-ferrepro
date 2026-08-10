from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import settings
from .supabase import SupabaseError, insert, select

logger = logging.getLogger(__name__)


class MetaError(RuntimeError):
    pass


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    if not (settings.meta_app_secret and signature):
        return False
    expected = "sha256=" + hmac.new(
        settings.meta_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def relay_webhook(raw_body: bytes) -> int:
    if not (settings.meta_app_secret and settings.meta_webhook_forward_url):
        raise MetaError("Faltan META_APP_SECRET / META_WEBHOOK_FORWARD_URL.")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise MetaError("Meta envió un webhook inválido.") from exc
    if not isinstance(payload, dict):
        raise MetaError("Meta envió un webhook inválido.")

    # ANTES de reenviar: Chatwoot nos va a rebotar su propio webhook y el worker va a buscar el
    # referral para atarlo a la conversación. Si guardáramos después, ganaría la carrera y la
    # conversación quedaría sin atribuir.
    save_ad_referrals(payload)

    transformed, order_count = transform_order_messages(payload)
    body = json.dumps(transformed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + hmac.new(
        settings.meta_app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    request = urllib.request.Request(
        settings.meta_webhook_forward_url,
        data=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status >= 300:
                raise MetaError(f"Chatwoot respondió HTTP {response.status} al webhook de Meta.")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise MetaError(f"Chatwoot webhook HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MetaError(f"No se pudo reenviar el webhook de Meta a Chatwoot: {exc.reason}") from exc
    return order_count


def extract_ad_referrals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Referrals de anuncios click-to-WhatsApp: Meta los adosa al primer mensaje que manda el
    cliente después de tocar el aviso. Solo vienen en ese mensaje, nunca más en la charla."""
    referrals: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                referral = message.get("referral")
                phone = "".join(ch for ch in str(message.get("from") or "") if ch.isdigit())
                if not isinstance(referral, dict) or not phone:
                    continue
                referrals.append(
                    {
                        "phone": phone,
                        "wa_message_id": message.get("id"),
                        "source_id": referral.get("source_id"),
                        "source_type": referral.get("source_type"),
                        "source_url": referral.get("source_url"),
                        "headline": referral.get("headline"),
                        "ctwa_clid": referral.get("ctwa_clid"),
                        # Meta agrega campos al referral cada tanto; el crudo evita perderlos.
                        "raw": referral,
                    }
                )
    return referrals


def save_ad_referrals(payload: dict[str, Any]) -> int:
    """Fire-and-forget: un Supabase caído NO puede cortar la entrega del mensaje al cliente.
    El costo es cero en el camino normal — solo hay referral en charlas que nacen de un aviso."""
    referrals = extract_ad_referrals(payload)
    guardados = 0
    for referral in referrals:
        try:
            insert("chat_ad_referrals", referral, return_row=False)
            guardados += 1
        except SupabaseError as exc:
            # 409 = ya lo guardamos (Meta reintenta webhooks); el resto es problema real.
            if "409" not in str(exc):
                logger.warning(
                    "ad_referral_save_failed",
                    extra={"source_id": referral.get("source_id"), "error": str(exc)},
                )
    return guardados


def transform_order_messages(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    orders = 0
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict) or message.get("type") != "order":
                    continue
                message["type"] = "text"
                message["text"] = {"body": catalog_order_text(message.get("order") or {})}
                message.pop("order", None)
                orders += 1
    return payload, orders


def catalog_order_text(order: dict[str, Any]) -> str:
    items = [item for item in order.get("product_items") or [] if isinstance(item, dict)][:30]
    retailer_ids = [str(item.get("product_retailer_id")) for item in items if item.get("product_retailer_id")]
    if not retailer_ids:
        return "Seleccioné productos del catálogo y quiero comprarlos."

    ids = ",".join(dict.fromkeys(retailer_ids))
    variants = select("ferrepro_variantes", f"id=in.({ids})&select=id,product_id")
    product_ids = [str(row["product_id"]) for row in variants if row.get("product_id") is not None]
    products = (
        select(
            "ferrepro_productos",
            f"id=in.({','.join(dict.fromkeys(product_ids))})&select=id,name,canonical_url",
        )
        if product_ids
        else []
    )
    product_by_id = {str(row["id"]): str(row.get("name") or "Producto") for row in products}
    url_by_id = {str(row["id"]): str(row.get("canonical_url") or "") for row in products}
    name_by_retailer = {
        str(row["id"]): product_by_id.get(str(row.get("product_id")), f"Producto {row['id']}")
        for row in variants
    }
    url_by_retailer = {
        str(row["id"]): url_by_id.get(str(row.get("product_id")), "")
        for row in variants
    }
    lines = ["Seleccioné estos productos del catálogo y quiero comprarlos:"]
    for item in items:
        retailer_id = str(item.get("product_retailer_id") or "")
        quantity = item.get("quantity") or 1
        lines.append(f"- {quantity} x {name_by_retailer.get(retailer_id, f'Producto {retailer_id}')}")
        if url := url_by_retailer.get(retailer_id):
            lines.append(f"  Link: {url}")
    return "\n".join(lines)


def product_list_payload(to: str, product_ids: list[str | int], *, body: str, header: str = "Productos") -> dict[str, Any]:
    if not settings.meta_catalog_id:
        raise MetaError("Falta META_CATALOG_ID.")
    ids = [str(pid) for pid in product_ids[:30]]
    if not ids:
        raise MetaError("No hay productos para enviar.")
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "product_list",
            "header": {"type": "text", "text": header[:60]},
            "body": {"text": body[:1024]},
            "action": {
                "catalog_id": settings.meta_catalog_id,
                "sections": [
                    {
                        "title": "Opciones",
                        "product_items": [{"product_retailer_id": pid} for pid in ids],
                    }
                ],
            },
        },
    }


def single_product_payload(to: str, product_id: str | int, *, body: str) -> dict[str, Any]:
    if not settings.meta_catalog_id:
        raise MetaError("Falta META_CATALOG_ID.")
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "product",
            "body": {"text": body[:1024]},
            "action": {"catalog_id": settings.meta_catalog_id, "product_retailer_id": str(product_id)},
        },
    }


def product_retailer_ids_by_product(product_ids: list[int]) -> dict[int, str]:
    if not product_ids:
        return {}
    ids = ",".join(str(int(pid)) for pid in product_ids[:30])
    rows = select("ferrepro_variantes", f"product_id=in.({ids})&select=product_id,id,position&order=position.asc")
    first_by_product: dict[int, str] = {}
    for row in rows:
        first_by_product.setdefault(int(row["product_id"]), str(row["id"]))
    return first_by_product


def product_retailer_ids(product_ids: list[int]) -> list[str]:
    first_by_product = product_retailer_ids_by_product(product_ids)
    return [first_by_product[pid] for pid in product_ids if pid in first_by_product]


def product_retailer_id(product_id: int) -> str | None:
    ids = product_retailer_ids([product_id])
    return ids[0] if ids else None


def available_catalog_retailer_ids(retailer_ids: list[str]) -> set[str]:
    if not retailer_ids:
        return set()
    if not (settings.meta_access_token and settings.meta_catalog_id):
        raise MetaError("Faltan META_ACCESS_TOKEN / META_CATALOG_ID.")
    ids = list(dict.fromkeys(str(value) for value in retailer_ids))[:30]
    query = urllib.parse.urlencode(
        {
            "fields": "retailer_id,visibility,availability,capability_to_review_status",
            "filter": json.dumps({"retailer_id": {"is_any": ids}}),
            "limit": len(ids),
        }
    )
    url = (
        f"https://graph.facebook.com/{settings.meta_graph_version}/"
        f"{settings.meta_catalog_id}/products?{query}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {settings.meta_access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = (json.load(resp) or {}).get("data") or []
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise MetaError(f"Meta Catalog HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MetaError(f"No se pudo consultar el catálogo de Meta: {exc.reason}") from exc
    except (json.JSONDecodeError, AttributeError) as exc:
        raise MetaError("Meta Catalog devolvió una respuesta inválida") from exc
    return {
        str(row["retailer_id"])
        for row in rows
        if isinstance(row, dict)
        and row.get("retailer_id")
        and str(row.get("visibility") or "").lower() == "published"
        and str(row.get("availability") or "").lower() not in {"out of stock", "discontinued"}
        and any(
            str(capability.get("key") or "").upper() == "WHATSAPP"
            and str(capability.get("value") or "").upper() == "APPROVED"
            for capability in row.get("capability_to_review_status") or []
            if isinstance(capability, dict)
        )
    }


def send_whatsapp(payload: dict[str, Any]) -> dict[str, Any]:
    if not (settings.meta_access_token and settings.meta_phone_number_id):
        raise MetaError("Faltan META_ACCESS_TOKEN / META_PHONE_NUMBER_ID.")
    url = f"https://graph.facebook.com/{settings.meta_graph_version}/{settings.meta_phone_number_id}/messages"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.meta_access_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise MetaError(f"Meta Graph HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MetaError(f"No se pudo conectar a Meta Graph: {exc.reason}") from exc


if __name__ == "__main__":
    settings.meta_catalog_id = "cat_1"
    payload = product_list_payload("5493815555555", [1, "2"], body="Te paso opciones")
    items = payload["interactive"]["action"]["sections"][0]["product_items"]
    assert items == [{"product_retailer_id": "1"}, {"product_retailer_id": "2"}]
    print("meta self-check: OK")
