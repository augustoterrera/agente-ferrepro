"""Notificador de errores por Telegram. Stdlib only, nunca lanza, no-op si no está configurado.

Config (en .env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERT_PROJECT.
Prueba: python -m app.notifier "mensaje de prueba"
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger("notifier")
_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX = 4000


def enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(text: str, parse_mode: str = "HTML") -> bool:
    token, chat = settings.telegram_bot_token, settings.telegram_chat_id
    if not (token and chat):
        log.debug("telegram no configurado; alerta omitida")
        return False
    try:
        data = json.dumps(
            {"chat_id": chat, "text": text[:_MAX], "parse_mode": parse_mode, "disable_web_page_preview": True}
        ).encode("utf-8")
        req = urllib.request.Request(_API.format(token=token), data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # nunca romper el flujo por una alerta
        log.warning("no pude enviar alerta a Telegram: %s", exc)
        return False


def notify_error(titulo: str, detalle: object = None, contexto: dict | None = None) -> bool:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lineas = [f"🔴 <b>{_esc(settings.alert_project)}</b> — {_esc(titulo)}", f"<i>{ts}</i>"]
    for key, value in (contexto or {}).items():
        lineas.append(f"• <b>{_esc(key)}:</b> {_esc(value)}")
    if detalle:
        lineas.append("")
        lineas.append(f"<pre>{_esc(str(detalle)[:1500])}</pre>")
    return send("\n".join(lineas))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level="INFO")
    msg = sys.argv[1] if len(sys.argv) > 1 else "prueba de alerta"
    if not enabled():
        print("Telegram NO configurado (faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        sys.exit(2)
    ok = notify_error("test de notificador", detalle=msg, contexto={"origen": "manual"})
    print("enviado OK" if ok else "falló el envío (ver logs)")
    sys.exit(0 if ok else 1)
