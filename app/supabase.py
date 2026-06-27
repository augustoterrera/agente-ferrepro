from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import settings


class SupabaseError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    key = settings.supabase_service_key
    if not (settings.supabase_url and key):
        raise SupabaseError("Faltan SUPABASE_URL / SUPABASE_SERVICE_KEY.")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(method: str, path: str, *, body: Any = None, extra_headers: dict[str, str] | None = None) -> Any:
    url = settings.supabase_url.rstrip("/") + "/rest/v1" + path
    headers = _headers()
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SupabaseError(f"Supabase REST {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SupabaseError(f"No se pudo conectar a Supabase REST: {exc.reason}") from exc


def rpc(name: str, params: dict[str, Any]) -> Any:
    """Invoca una función SQL (RPC) vía PostgREST. Las chat_* atómicas/locks viven acá."""
    return _request("POST", f"/rpc/{name}", body=params)


def select(table: str, query: str = "") -> list[dict[str, Any]]:
    return _request("GET", f"/{table}?{query}") or []


def insert(table: str, row: dict[str, Any], *, return_row: bool = True) -> dict[str, Any] | None:
    prefer = "return=representation" if return_row else "return=minimal"
    rows = _request("POST", f"/{table}", body=row, extra_headers={"Prefer": prefer})
    return (rows or [None])[0] if return_row else None


def update(table: str, query: str, patch: dict[str, Any]) -> None:
    _request("PATCH", f"/{table}?{query}", body=patch, extra_headers={"Prefer": "return=minimal"})


def patch_returning(table: str, query: str, patch: dict[str, Any]) -> list[dict[str, Any]]:
    """PATCH que devuelve las filas afectadas. Para claims atómicos (ej. reclamar un outbox
    solo si está pending): si vuelve vacío, otro worker lo tomó primero."""
    return _request("PATCH", f"/{table}?{query}", body=patch, extra_headers={"Prefer": "return=representation"}) or []


def delete(table: str, query: str) -> None:
    _request("DELETE", f"/{table}?{query}", extra_headers={"Prefer": "return=minimal"})
