from __future__ import annotations

import hashlib
import hmac
import json
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import AgentMessage


class ChatwootError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatwootMessageEvent:
    event: str
    message_id: int | str | None
    conversation_id: str
    account_id: str | None
    content: str
    history: list[AgentMessage]


@dataclass(frozen=True)
class ChatwootClient:
    base_url: str
    access_token: str
    timeout_seconds: int = 30

    def create_outgoing_message(self, account_id: int | str, conversation_id: int | str, content: str) -> dict[str, Any]:
        endpoint = (
            f"{self.base_url.rstrip('/')}/api/v1/accounts/{account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        body = {"content": content, "message_type": "outgoing", "private": False, "content_type": "text"}
        return self._request("POST", endpoint, body)

    def has_outgoing_message(
        self,
        account_id: int | str,
        conversation_id: int | str,
        content: str,
        *,
        created_after: str,
    ) -> bool:
        """¿Ya salió este texto en esta conversación después de `created_after`?

        La API de mensajes de Chatwoot no acepta clave de idempotencia: si el POST llega pero se
        pierde la confirmación, el reintento lo mandaría de nuevo y el cliente lo vería dos veces.
        Esto lo detecta antes de duplicar.

        Se acota a la ventana de este outbox porque comparar contra todo el historial daría falsos
        positivos cuando el bot repite una respuesta legítima."""
        endpoint = (
            f"{self.base_url.rstrip('/')}/api/v1/accounts/{account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        data = self._request("GET", endpoint)
        payload = (data or {}).get("payload")
        if not isinstance(payload, list):
            return False
        objetivo = content.strip()
        desde = _epoch(created_after)
        if desde is None:
            return False
        return any(
            _role(m) == "assistant"
            and str(m.get("content") or "").strip() == objetivo
            and (creado := _epoch(m.get("created_at"))) is not None
            and creado >= desde - 5
            for m in payload
            if isinstance(m, dict)
        )

    def get_conversation_labels(self, account_id: int | str, conversation_id: int | str) -> list[str]:
        endpoint = f"{self.base_url.rstrip('/')}/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
        data = self._request("GET", endpoint)
        return list((data or {}).get("payload") or [])

    def set_conversation_labels(self, account_id: int | str, conversation_id: int | str, labels: list[str]) -> list[str]:
        # La API REEMPLAZA el set completo de labels → hay que mandar las que se conservan también.
        endpoint = f"{self.base_url.rstrip('/')}/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
        data = self._request("POST", endpoint, {"labels": labels})
        return list((data or {}).get("payload") or [])

    def assign_conversation(self, account_id: int | str, conversation_id: int | str, assignee_id: int | str) -> dict[str, Any]:
        endpoint = f"{self.base_url.rstrip('/')}/api/v1/accounts/{account_id}/conversations/{conversation_id}/assignments"
        return self._request("POST", endpoint, {"assignee_id": assignee_id})

    def _request(self, method: str, endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"content-type": "application/json", "api_access_token": self.access_token},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ChatwootError(f"Chatwoot {method} HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ChatwootError(f"No se pudo conectar a la API de Chatwoot: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ChatwootError("Chatwoot devolvió JSON inválido") from exc


def build_chatwoot_client(base_url: str | None, access_token: str | None) -> ChatwootClient | None:
    if not base_url or not access_token:
        return None
    return ChatwootClient(base_url=base_url, access_token=access_token)


def verify_chatwoot_signature(
    raw_body: bytes,
    secret: str | None,
    signature: str | None,
    timestamp: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> bool:
    if not secret:
        return True  # sin secret configurado, no se verifica (avisar en arranque)
    if not signature or not timestamp:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    current_time = time.time() if now is None else now
    if tolerance_seconds > 0 and abs(current_time - signed_at) > tolerance_seconds:
        return False
    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_chatwoot_webhook_token(secret: str | None, token: str | None) -> bool:
    if not secret:
        return True
    if not token:
        return False
    return hmac.compare_digest(secret, token)


def parse_chatwoot_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ChatwootError("JSON de webhook inválido") from exc
    if not isinstance(payload, dict):
        raise ChatwootError("El payload del webhook debe ser un objeto JSON")
    return payload


def extract_message_event(payload: dict[str, Any], history_limit: int = 16) -> tuple[ChatwootMessageEvent | None, str | None]:
    if str(payload.get("event") or "") != "message_created":
        return None, "ignored_event"
    if str(payload.get("message_type") or "") != "incoming":
        return None, "ignored_non_incoming_message"
    if bool(payload.get("private")):
        return None, "ignored_private_message"
    attachments = message_attachments(payload)
    if str(payload.get("content_type") or "text") != "text" and not attachments:
        return None, "ignored_non_text_message"

    content = str(payload.get("content") or "").strip()
    if not content and not attachments:
        return None, "ignored_empty_message"

    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    conversation_id = conversation.get("id") or payload.get("conversation_id")
    account_id = account.get("id") or payload.get("account_id")
    if conversation_id is None:
        return None, "missing_conversation_id"

    return (
        ChatwootMessageEvent(
            event="message_created",
            message_id=payload.get("id"),
            conversation_id=str(conversation_id),
            account_id=str(account_id) if account_id is not None else None,
            content=content,
            history=extract_history(payload, history_limit),
        ),
        None,
    )


def extract_history(payload: dict[str, Any], limit: int) -> list[AgentMessage]:
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
    current_id = payload.get("id")
    history: list[AgentMessage] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if current_id is not None and message.get("id") == current_id:
            continue
        if bool(message.get("private")):
            continue
        if str(message.get("content_type") or "text") != "text":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        role = _role(message)
        if role is None:
            continue
        history.append(AgentMessage(role=role, content=content))
    return history[-max(0, limit):]


def _role(message: dict[str, Any]) -> str | None:
    message_type = message.get("message_type")
    if message_type in ("incoming", 0):
        return "user"
    if message_type in ("outgoing", 1):
        return "assistant"
    return None


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def message_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Adjuntos del mensaje (imágenes/audios/archivos) tal como los manda Chatwoot."""
    raw = payload.get("attachments")
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for att in raw:
        if not isinstance(att, dict):
            continue
        url = att.get("data_url") or att.get("thumb_url")
        file_type = att.get("file_type")
        if url and file_type:
            out.append({"type": str(file_type), "url": str(url), "extension": att.get("extension")})
    return out


def conversation_labels(payload: dict[str, Any]) -> list[str]:
    """Labels que Chatwoot ya manda en el payload del webhook (array de strings)."""
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    labels = conversation.get("labels") or payload.get("labels") or []
    return [str(label) for label in labels] if isinstance(labels, list) else []


def chatwoot_contact_id(payload: dict[str, Any]) -> str | None:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    value = sender.get("id") or contact.get("id")
    return str(value) if value is not None else None


def should_handoff_to_agent(content: str) -> bool:
    text = content.lower()
    # ponytail: el prompt usa esta frase para derivaciones reales; si agregan nuevas plantillas,
    # sumar frases acá antes de inventar estado/tool-calls.
    return "te derivo con un vendedor" in text and "si quer" not in text


def is_handoff_acceptance(content: str, history: list[AgentMessage]) -> bool:
    last_assistant = next((m.content for m in reversed(history) if m.role == "assistant"), "")
    return _offered_handoff(last_assistant) and _accepts_handoff(content)


def _offered_handoff(content: str) -> bool:
    text = _plain(content)
    return "vendedor" in text and ("queres que te derive" in text or "queres que te deriven" in text)


def _accepts_handoff(content: str) -> bool:
    text = _plain(content)
    if not text:
        return False
    if "?" in text and "deriv" not in text:
        return False
    if text in {"si", "dale", "ok", "okay", "bueno", "perfecto", "de una"}:
        return True
    return any(
        phrase in text
        for phrase in (
            "si me interesa",
            "si quiero",
            "me interesa",
            "quiero coordinar",
            "quiero que me deriven",
            "derivame",
            "pasame con",
            "te dije que si",
        )
    )


def _plain(content: str) -> str:
    text = unicodedata.normalize("NFKD", content.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.replace("¿", "").replace("¡", "").split())


def is_no_stock_handoff(content: str) -> bool:
    """El caso 'no hay literalmente': la respuesta deriva a un humano Y manda al cliente a las
    sucursales de Monteagudo/Avellaneda. Se detecta por esas sucursales dentro de un handoff.
    Las sucursales solo se nombran en esta plantilla y en la respuesta institucional —que no deriva—
    así que 'handoff + sucursal' identifica sin falsos positivos."""
    if not should_handoff_to_agent(content):
        return False
    text = content.lower()
    return "monteagudo" in text or "avellaneda" in text


def detect_handoff_flags(content: str) -> list[str]:
    """Flags comerciales deducibles de la respuesta cuando hay handoff (ahí el clasificador no
    corre). Se detectan por la plantilla que disparó, igual que should_handoff_to_agent. Solo las
    flags cuyas plantillas derivan a humano: mayorista, negociacion y el sin_stock 'no hay
    literalmente'. Mantener los nombres en sync con Flag en classifier.py."""
    text = content.lower()
    flags: list[str] = []
    if is_no_stock_handoff(content):
        flags.append("sin_stock")
    if "compras por cantidad" in text or "presupuestos personalizados" in text:
        flags.append("mayorista")
    if "mejor condición disponible" in text or "mejor condicion disponible" in text:
        flags.append("negociacion")
    return flags


if __name__ == "__main__":
    assert should_handoff_to_agent("Te derivo con un vendedor de FerrePro.")
    assert not should_handoff_to_agent("Si querés, te derivo con un vendedor para revisar alternativas.")
    assert is_handoff_acceptance(
        "Si me interesa",
        [AgentMessage(role="assistant", content="¿Querés que te derive con un vendedor para coordinar la compra?")],
    )
    assert not is_handoff_acceptance(
        "Sí, cuánto sale el envío?",
        [AgentMessage(role="assistant", content="¿Querés que te derive con un vendedor para coordinar la compra?")],
    )
    assert is_no_stock_handoff(
        "No tengo taladro disponible. Podés consultarlo en Bernardo Monteagudo 340 o Av. Avellaneda 512. "
        "Te derivo con un vendedor de FerrePro para que te ayude."
    )
    assert not is_no_stock_handoff("Te derivo con un vendedor de FerrePro para que pueda ayudarte.")
    assert not is_no_stock_handoff("📍 Bernardo Monteagudo 340\nLunes a viernes de 9 a 17 hs.")
    assert detect_handoff_flags(
        "No tengo eso disponible. Podés consultarlo en Bernardo Monteagudo 340 o Av. Avellaneda 512. "
        "Te derivo con un vendedor de FerrePro para que te ayude."
    ) == ["sin_stock"]
    assert detect_handoff_flags(
        "Para compras por cantidad o presupuestos personalizados, te derivo con un vendedor de FerrePro para que lo vea con vos."
    ) == ["mayorista"]
    assert detect_handoff_flags(
        "Te derivo con un vendedor de FerrePro para que pueda revisar la mejor condición disponible."
    ) == ["negociacion"]
    assert detect_handoff_flags("Te derivo con un vendedor de FerrePro para que pueda ayudarte.") == []
    print("self-check puro: OK")
