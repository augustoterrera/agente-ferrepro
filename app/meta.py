from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import settings
from .supabase import select


class MetaError(RuntimeError):
    pass


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
            "fields": "retailer_id,visibility,availability",
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
