"""Reservas de la reunión (Google Calendar) → conversación de WhatsApp.

La página de citas de Google no le avisa a nadie cuando el cliente reserva: el evento aparece en
el calendario y nada más. Este módulo lo lee con una cuenta de servicio (solo lectura sobre el
calendario compartido con ella) y lo vincula con la conversación por el teléfono que el cliente
deja en el formulario de reserva.

El orquestador (sweep_agenda) vive en app/tasks/chatwoot_tasks.py, igual que el retargeting.
Probar las credenciales: docker compose exec worker_outbound python -m app.agenda [--raw]
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings

logger = logging.getLogger(__name__)

TZ = ZoneInfo("America/Argentina/Tucuman")
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
MAX_PAGINAS = 10

# Texto de la confirmación del PASO 4 del prompt. Si el bot ya la mandó en el chat (el cliente
# avisó que agendó antes de que el sweep viera la reserva), no se repite.
MARCA_CONFIRMACION = "factura de luz reciente"


class AgendaError(RuntimeError):
    pass


def habilitada() -> bool:
    return bool(settings.google_service_account_json and settings.google_calendar_id)


# ── Lectura del calendario ──────────────────────────────────────────────────
@dataclass(frozen=True)
class Reserva:
    event_id: str
    cancelada: bool
    # Un evento cancelado puede venir solo con id y status: el resto queda vacío.
    inicio: datetime | None = None
    nombre: str | None = None
    email: str | None = None
    telefonos: tuple[str, ...] = ()
    titulo: str = ""


_token_cache: tuple[str, float] | None = None


def proximas_reservas(now: datetime | None = None) -> list[Reserva]:
    """Reservas que todavía no terminaron, más las canceladas (showDeleted) para enterarnos."""
    now = now or datetime.now(timezone.utc)
    params = {
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=settings.agenda_horizonte_dias)).isoformat(),
        "singleEvents": "true",
        "showDeleted": "true",
        "orderBy": "startTime",
        "maxResults": "250",
    }
    url = EVENTS_URL.format(calendar_id=urllib.parse.quote(settings.google_calendar_id or "", safe=""))
    reservas: list[Reserva] = []
    for _ in range(MAX_PAGINAS):
        data = _http("GET", f"{url}?{urllib.parse.urlencode(params)}", token=_access_token())
        for evento in data.get("items") or []:
            reserva = parse_evento(evento)
            if reserva is not None:
                reservas.append(reserva)
        if not data.get("nextPageToken"):
            break
        params["pageToken"] = data["nextPageToken"]
    return reservas


def parse_evento(evento: dict[str, Any]) -> Reserva | None:
    """None si el evento no es una reserva: eventos de todo el día, sin invitados (bloqueos
    propios) o que no matchean AGENDA_TITULO cuando está configurado."""
    event_id = str(evento.get("id") or "")
    if not event_id:
        return None
    if evento.get("status") == "cancelled":
        return Reserva(event_id=event_id, cancelada=True)
    inicio = parse_dt((evento.get("start") or {}).get("dateTime"))
    if inicio is None:
        return None
    titulo = str(evento.get("summary") or "")
    if settings.agenda_titulo and settings.agenda_titulo.lower() not in titulo.lower():
        return None
    invitados = [
        a
        for a in evento.get("attendees") or []
        if isinstance(a, dict) and not (a.get("organizer") or a.get("self") or a.get("resource"))
    ]
    if not invitados:
        return None
    texto = "\n".join([str(evento.get("description") or ""), *[str(a.get("comment") or "") for a in invitados]])
    return Reserva(
        event_id=event_id,
        cancelada=False,
        inicio=inicio,
        nombre=str(invitados[0].get("displayName") or "").strip() or None,
        email=str(invitados[0].get("email") or "").strip() or None,
        telefonos=tuple(extraer_telefonos(texto)),
        titulo=titulo,
    )


def _access_token() -> str:
    """Token OAuth de la cuenta de servicio (JWT firmado → token). Se cachea en el proceso hasta
    un minuto antes de vencer."""
    global _token_cache
    if _token_cache and _token_cache[1] > time.time() + 60:
        return _token_cache[0]
    from google.auth import crypt, jwt

    info = _service_account_info()
    token_uri = str(info.get("token_uri") or TOKEN_URI)
    now = int(time.time())
    try:
        assertion = jwt.encode(
            crypt.RSASigner.from_service_account_info(info),
            {"iss": info["client_email"], "scope": SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600},
        )
    except (KeyError, ValueError) as exc:
        raise AgendaError(f"Credenciales de Google inválidas: {exc}") from exc
    body = urllib.parse.urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion.decode("ascii")}
    ).encode("ascii")
    data = _http("POST", token_uri, body=body, content_type="application/x-www-form-urlencoded")
    if not data.get("access_token"):
        raise AgendaError("Google no devolvió access_token")
    _token_cache = (str(data["access_token"]), now + int(data.get("expires_in") or 3600))
    return _token_cache[0]


def _service_account_info() -> dict[str, Any]:
    """GOOGLE_SERVICE_ACCOUNT_JSON acepta el JSON entero (en una línea) o la ruta al archivo."""
    raw = (settings.google_service_account_json or "").strip()
    try:
        if not raw.startswith("{"):
            raw = Path(raw).read_text(encoding="utf-8")
        info = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AgendaError(f"No se pudo leer GOOGLE_SERVICE_ACCOUNT_JSON: {exc}") from exc
    if not isinstance(info, dict):
        raise AgendaError("GOOGLE_SERVICE_ACCOUNT_JSON no es un objeto JSON")
    return info


def _http(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    token: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AgendaError(f"Google {method} HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AgendaError(f"No se pudo conectar a Google: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise AgendaError("Google devolvió JSON inválido") from exc


def parse_dt(value: object) -> datetime | None:
    """Siempre en UTC: el state se filtra en PostgREST comparando el ISO como texto, y Google
    devuelve el offset del calendario (-03:00). Mezclados, un turno de hoy quedaba "en el pasado"."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)).astimezone(timezone.utc)


# ── Teléfonos ───────────────────────────────────────────────────────────────
# Secuencias de dígitos con separadores típicos (espacios, guiones, paréntesis, puntos). Sin
# saltos de línea: si no, el teléfono se pega con el número de la línea siguiente.
_TELEFONO_RE = re.compile(r"\+?\d[\d \t().-]{5,}\d")


def extraer_telefonos(texto: str) -> list[str]:
    """Candidatos a teléfono escritos en la reserva, solo dígitos. Puede traer números que no son
    teléfonos (un ID de Zoom, por ejemplo): no importa, solo cuentan si coinciden con un chat."""
    vistos: list[str] = []
    for match in _TELEFONO_RE.finditer(texto or ""):
        digitos = _digitos(match.group(0))
        if 7 <= len(digitos) <= 15 and digitos not in vistos:
            vistos.append(digitos)
    return vistos


def mismo_telefono(escrito: str, whatsapp: str) -> bool:
    """¿El teléfono que el cliente escribió en el formulario es el de su WhatsApp?

    WhatsApp lo da completo (5493815551234) y la gente lo escribe como le sale: 381 555-1234,
    0381 15 555-1234, +54 9 381..., o solo 555-1234. Se comparan las formas nacionales de 10
    dígitos (característica + número, sin 0 ni 15)."""
    a, b = _digitos(escrito), _digitos(whatsapp)
    if len(a) < 7 or len(b) < 7:
        return False
    if a == b:
        return True
    nacional_b = _nacional_ar(b)
    if nacional_b & _nacional_ar(a):
        return True
    # Número extranjero o argentino sin código de país, pero con el resto completo.
    if len(a) >= 10 and b.endswith(a):
        return True
    # Solo el número local, sin característica (con o sin el 15 adelante).
    local = a[2:] if a.startswith("15") and len(a) in (9, 10) else a
    return 7 <= len(local) <= 8 and any(n.endswith(local) for n in nacional_b)


def _nacional_ar(digitos: str) -> set[str]:
    d = digitos[2:] if digitos.startswith("00") else digitos
    if d.startswith("54"):
        d = d[2:]
        if d.startswith("9"):
            d = d[1:]
    d = d.lstrip("0")
    if len(d) == 10:
        return {d}
    if len(d) == 12:
        # Celular escrito a la antigua: característica (2 a 4 dígitos) + 15 + número.
        return {d[:k] + d[k + 2 :] for k in (2, 3, 4) if d[k : k + 2] == "15"}
    return set()


def _digitos(texto: str) -> str:
    return "".join(ch for ch in str(texto or "") if ch.isdigit())


# ── Mensajes ────────────────────────────────────────────────────────────────
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def hora(inicio: datetime) -> str:
    local = inicio.astimezone(TZ)
    return f"{local.hour}" if local.minute == 0 else f"{local.hour}:{local.minute:02d}"


def describir_turno(inicio: datetime) -> str:
    """'jueves 29/10 a las 17 hs', en hora de Tucumán."""
    local = inicio.astimezone(TZ)
    return f"{_DIAS[local.weekday()]} {local.day}/{local.month} a las {hora(inicio)} hs"


def texto_confirmacion(inicio: datetime) -> str:
    return (
        f"Genial, me llegó tu turno: {describir_turno(inicio)}. Ya tenés la invitación con el Meet en tu "
        f"mail. Tené a mano una {MARCA_CONFIRMACION} (una foto sirve) y pensá qué cosas no pueden "
        "quedarse sin energía."
    )


def texto_recordatorio_agenda() -> str:
    """Lo que dice la plantilla de recordatorio para agendar (el cuerpo vive en Meta). Se guarda
    en el historial para que el bot entienda la respuesta del cliente ("a la tarde")."""
    return (
        "¿Pudiste elegir el horario para la reunión de 20 min con FerrePro? Lo agendás acá: "
        f"{(settings.calendar_booking_url or '').strip()} . Si preferís que te lo reservemos "
        "nosotros, contestanos mañana o tarde."
    )


def turno_vigente(state: dict[str, Any] | None, now: datetime | None = None) -> str | None:
    """Turno ya reservado y por delante, para que el agente no le vuelva a mandar el link."""
    agenda = (state or {}).get("agenda")
    if not isinstance(agenda, dict) or agenda.get("estado") != "agendado":
        return None
    inicio = parse_dt(agenda.get("inicio"))
    if inicio is None or inicio <= (now or datetime.now(timezone.utc)):
        return None
    return describir_turno(inicio)


def plantilla_payload(to: str, nombre: str, parametros: list[str] | None = None) -> dict[str, Any]:
    """Plantilla aprobada de WhatsApp: lo único que se puede mandar pasadas las 24 h del último
    mensaje del cliente. Las variables van en el cuerpo, en orden ({{1}}, {{2}}...)."""
    template: dict[str, Any] = {"name": nombre, "language": {"code": settings.whatsapp_template_language}}
    if parametros:
        template["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in parametros]}
        ]
    return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}


if __name__ == "__main__":
    import sys

    # Puro, sin red.
    assert mismo_telefono("381 555-1234", "5493815551234")
    assert mismo_telefono("0381 15 555-1234", "5493815551234")
    assert mismo_telefono("+54 9 381 555 1234", "5493815551234")
    assert mismo_telefono("15 5551234", "5493815551234")
    assert not mismo_telefono("381 555-9999", "5493815551234")
    print("agenda puro: OK")

    if not habilitada():
        print("calendario: SALTEADO (faltan GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_CALENDAR_ID)")
        raise SystemExit
    # Vivo: muestra qué reservas ve el sweep y qué teléfono les sacó. Con --raw imprime el JSON
    # crudo de cada evento, para ver dónde guarda Google las respuestas del formulario.
    raw = "--raw" in sys.argv
    reservas = proximas_reservas()
    print(f"calendario {settings.google_calendar_id}: {len(reservas)} reserva(s)")
    for r in reservas:
        turno = describir_turno(r.inicio) if r.inicio else "-"
        print(f"  {r.event_id} | {'CANCELADA' if r.cancelada else turno} | {r.nombre} | {r.email} | tel={r.telefonos}")
    if raw:
        token = _access_token()
        url = EVENTS_URL.format(calendar_id=urllib.parse.quote(settings.google_calendar_id or "", safe=""))
        query = urllib.parse.urlencode({"timeMin": datetime.now(timezone.utc).isoformat(), "singleEvents": "true", "maxResults": "5"})
        print(json.dumps(_http("GET", f"{url}?{query}", token=token), ensure_ascii=False, indent=2))
