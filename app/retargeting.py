from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from .agent import _history_product_links
from .chatwoot import should_handoff_to_agent
from .classifier import FLAG_LABELS
from .config import settings
from .models import AgentMessage

logger = logging.getLogger(__name__)

# Follow-up one-shot a leads que se colgaron. La selección es en tres capas:
#   1. SQL (RPC chat_retargeting_candidates): el bot habló último, el cliente calló, la ventana
#      de 24h de WhatsApp sigue abierta y la conversación no fue evaluada nunca.
#   2. Reglas duras de acá: horario comercial, etiquetas que excluyen, derivación a humano.
#   3. Un LLM que decide si vale la pena y, si vale, escribe el mensaje con el contexto real.
# Las capas 1 y 2 son gratis y descartan la mayoría; la 3 solo corre sobre lo que sobrevive.

TZ = ZoneInfo("America/Argentina/Tucuman")

# Ventana de envío en hora de Tucumán: de lunes a lunes (todos los días, feriados incluidos),
# de 8 a 22.
SEND_START = time(8, 0)
SEND_END = time(22, 0)

# Etiquetas que vetan el follow-up. reclamo/fuera_rubro no son compradores; mayorista ya está
# con un vendedor; bot_apagado significa que un humano tomó la conversación.
ETIQUETAS_QUE_VETAN: dict[str, str] = {
    "reclamo": "es un reclamo",
    "fuera_rubro": "pidió algo fuera del rubro",
    "mayorista": "es mayorista, lo sigue un vendedor",
}


# Largo del follow-up: 300 es el objetivo que pide el prompt, 600 es el corte técnico. Son dos
# números distintos a propósito: pasarse de 300 es cosmético y no justifica descartar el lead;
# 600 ya es un modelo descontrolado. No va en el schema porque OpenAI structured outputs (strict)
# no soporta maxLength, así que se valida apenas llega la salida estructurada.
MENSAJE_OBJETIVO_CHARS = 300
MENSAJE_MAX_CHARS = 600


class RedactorError(RuntimeError):
    """Falla técnica del redactor (timeout, rate limit, API caída). NO es una decisión comercial:
    el candidato no se descarta, se reintenta en la próxima corrida."""


class Followup(BaseModel):
    """Decisión + mensaje. Un solo modelo: el que juzga es el que escribe, así no manda algo
    genérico cuando no tiene contexto real que retomar."""

    vale_la_pena: bool
    motivo: str = Field(description="Por qué sí o por qué no, en pocas palabras (queda en logs)")
    mensaje: str | None = None


REDACTOR_PROMPT = """\
Sos Matías, asistente de ventas de FerrePro, ferretería de San Miguel de Tucumán. Escribís por
WhatsApp en español rioplatense, con voseo.

Un cliente dejó de responder a mitad de la charla. Tu tarea es doble:
1. Decidir si vale la pena escribirle UNA sola vez para retomar.
2. Si vale, escribir ese mensaje.

## Cuándo NO vale la pena (vale_la_pena=false)

- El cliente se despidió, agradeció y cerró, o dijo que no le interesa.
- Ya compró, ya tiene el link para comprar, o quedó en pasar por la sucursal.
- La última respuesta del bot derivó a un vendedor: lo sigue un humano, no te metas.
- Es un reclamo, un cambio o una devolución.
- Pidió algo fuera del rubro de ferretería.
- No hay nada concreto que retomar: saludo suelto, charla sin producto, o una pregunta que ya
  quedó respondida y no necesitaba respuesta del cliente.
- El producto que buscaba está sin stock y no hay otra cosa que ofrecerle.

Ante la duda, NO escribas. Un mensaje de más molesta más de lo que un mensaje de menos pierde.

## Cuándo SÍ

El cliente venía preguntando por un producto concreto y quedó algo en el aire: una opción sin
elegir, una pregunta del bot sin contestar, un precio que no respondió.

## Cómo tiene que ser el mensaje

- 1 o 2 líneas. Máximo 300 caracteres.
- Nombrá el producto concreto que quedó pendiente, con las mismas palabras que ya usó la charla.
- Una sola pregunta, al final.
- No repitas el listado de productos ni vuelvas a pegar links.
- Entrá directo y cordial. Si la charla ya venía saludada, no saludes de nuevo.
- No inventes precios, stock, plazos, descuentos ni promociones: solo lo que ya está en la charla.
- Nunca te disculpes por insistir, ni digas "te escribo de nuevo", ni menciones que pasó tiempo.
- Nada de emojis de relleno; como máximo uno.

Ajustes según el contexto:
- Si el cliente estaba regateando el precio, podés recordarle el 10% de descuento pagando en
  efectivo en sucursal. Es el único beneficio que existe.
- Si pidió envío, ofrecé que un vendedor se lo coordine.
- Si preguntó por disponibilidad en una sucursal puntual, ofrecé que un vendedor lo confirme.
"""


def build_redactor() -> Agent[None, Followup]:
    if not settings.openai_api_key:
        raise RuntimeError("Falta OPENAI_API_KEY para redactar el follow-up.")
    model = OpenAIChatModel(settings.retargeting_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    model_settings = None
    effort = settings.retargeting_reasoning_effort
    if effort and (settings.retargeting_model.startswith("gpt-5") or settings.retargeting_model.startswith("o")):
        model_settings = OpenAIChatModelSettings(openai_reasoning_effort=effort)
    return Agent(model=model, output_type=Followup, system_prompt=REDACTOR_PROMPT, model_settings=model_settings)


def compose_followup(
    history: list[AgentMessage],
    *,
    nombre: str | None = None,
    stage: str | None = None,
    flags: list[str] | None = None,
) -> Followup:
    """Juez + redactor en una sola llamada.

    Un `vale_la_pena=False` es una decisión comercial y quema el lead (one-shot), así que una
    falla técnica NO puede disfrazarse de eso: se propaga como RedactorError para reintentar."""
    if not history:
        return Followup(vale_la_pena=False, motivo="sin historial")
    lines = ["Conversación (el cliente dejó de responder después del último mensaje):"]
    lines += [f"{'cliente' if m.role == 'user' else 'Matías'}: {m.content}" for m in history]
    lines.append("")
    lines.append(f"Nombre del cliente: {nombre or 'no lo sabemos'}")
    lines.append(f"Etapa del embudo: {stage or 'sin clasificar'}")
    lines.append(f"Flags comerciales: {', '.join(flags) if flags else 'ninguna'}")
    try:
        result = build_redactor().run_sync("\n".join(lines)).output
    except Exception as exc:
        logger.warning("followup_compose_failed", extra={"error": str(exc)})
        raise RedactorError(str(exc)) from exc
    mensaje = (result.mensaje or "").strip()
    if not result.vale_la_pena:
        return Followup(vale_la_pena=False, motivo=result.motivo)
    # De acá para abajo el modelo dijo que sí, así que el contrato tiene que cumplirse. Un "sí"
    # sin texto o con un chorizo es una falla del modelo, NO una decisión comercial: si lo
    # tratáramos como descarte quemaríamos el lead (one-shot) por un error técnico.
    if not mensaje:
        raise RedactorError("dijo vale_la_pena=True pero no escribió mensaje")
    if len(mensaje) > MENSAJE_MAX_CHARS:
        raise RedactorError(f"mensaje de {len(mensaje)} chars, se descontroló (máx {MENSAJE_MAX_CHARS})")
    if len(mensaje) > MENSAJE_OBJETIVO_CHARS:
        logger.info("followup_largo", extra={"chars": len(mensaje)})
    return Followup(vale_la_pena=True, motivo=result.motivo, mensaje=mensaje)


# ── Horario de envío (Tucumán) ──────────────────────────────────────────────
def en_horario_de_envio(now: datetime | None = None) -> bool:
    """True si AHORA se puede escribir: cualquier día, de 8 a 22 hora de Tucumán."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    return SEND_START <= now.time() < SEND_END


def proxima_oportunidad(now: datetime) -> datetime:
    """Próxima corrida del beat en la que se podría enviar. Asume que `now` está en horario
    (el llamador ya lo chequeó): la siguiente es dentro de un sweep, o mañana a las 8."""
    siguiente = now + timedelta(minutes=settings.retargeting_sweep_minutes)
    if en_horario_de_envio(siguiente):
        return siguiente
    return (siguiente + timedelta(days=1)).replace(
        hour=SEND_START.hour, minute=SEND_START.minute, second=0, microsecond=0
    )


def momento_de_enviar(cierre_de_ventana: datetime, now: datetime | None = None) -> bool:
    """True si esta es la ÚLTIMA corrida útil antes de que cierre la ventana de 24h de Meta.

    El recontacto se manda lo más tarde posible a propósito: dar tiempo real a que el cliente
    conteste solo, y recién ahí insistir. En la práctica eso cae 22-23h después del último
    mensaje del cliente, salvo que la ventana venza de madrugada — ahí se manda la noche
    anterior, que es la última chance dentro del horario."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if not en_horario_de_envio(now):
        return False
    # Margen: entre que decidimos y Meta entrega hay cola, reintentos y latencia. No apuramos
    # el envío al filo del cierre.
    limite = cierre_de_ventana.astimezone(TZ) - timedelta(hours=settings.retargeting_safety_margin_hours)
    if now >= limite:
        return True  # es ahora o nunca
    return proxima_oportunidad(now) > limite


def es_perdida_definitiva(cierre_de_ventana: datetime, now: datetime | None = None) -> bool:
    """True si NO va a haber ninguna corrida más antes de que cierre la ventana: el lead que no
    se contacte ahora se pierde para siempre.

    Ojo con la diferencia contra momento_de_enviar(): ese usa el límite con colchón, así que un
    candidato puede estar "listo para enviar" y todavía tener una hora de ventana real por delante.
    Para contar leads perdidos y alertar hay que mirar el cierre de verdad, o se avisa de más.

    El >= no es un detalle: la RPC de candidatos filtra con `last_user.at > now() - 24h`, así que
    una corrida que caiga JUSTO en el cierre ya no lo ve. Con > diríamos que todavía se salva."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    return proxima_oportunidad(now) >= cierre_de_ventana.astimezone(TZ)


# ── Reglas duras (capa 2) ───────────────────────────────────────────────────
def veto_por_etiquetas(labels: list[str]) -> str | None:
    """Motivo por el que las etiquetas vetan el follow-up, o None si está habilitado."""
    presentes = set(labels)
    if settings.bot_apagado_label in presentes:
        return "un humano tomó la conversación (bot_apagado)"
    if settings.no_retargeting_label in presentes:
        return f"tiene la etiqueta {settings.no_retargeting_label}"
    for label, motivo in ETIQUETAS_QUE_VETAN.items():
        if label in presentes:
            return motivo
    return None


def veto_por_conversacion(history: list[AgentMessage], stage: str | None) -> str | None:
    """Vetos que se leen de la charla misma, sin gastar tokens."""
    ultimo_bot = next((m.content for m in reversed(history) if m.role == "assistant"), "")
    if should_handoff_to_agent(ultimo_bot):
        return "el último mensaje del bot fue una derivación a un vendedor"
    # "curioso" es el cajón de sastre del clasificador: solo lo perseguimos si el bot llegó a
    # mostrarle productos (hay links en la charla). Si no, no hay nada concreto que retomar.
    if stage == "curioso" and not _mostro_productos(history):
        return "curioso sin productos mostrados"
    return None


def _mostro_productos(history: list[AgentMessage]) -> bool:
    return bool(_history_product_links([m for m in history if m.role == "assistant"]))


def state_labels(state: dict[str, Any] | None) -> list[str]:
    """Etiquetas espejadas en el state por el clasificador. Sirven de pre-filtro barato; la
    verdad para el envío la da la API de Chatwoot (un humano pudo etiquetar después)."""
    labels = (state or {}).get("labels")
    return [str(label) for label in labels] if isinstance(labels, list) else []


def state_stage(state: dict[str, Any] | None) -> str | None:
    stage = (state or {}).get("stage")
    return str(stage) if stage else None


def state_flags(state: dict[str, Any] | None) -> list[str]:
    flags = (state or {}).get("flags")
    if not isinstance(flags, list):
        return []
    return [str(f) for f in flags if str(f) in FLAG_LABELS]


def contact_name(payload: dict[str, Any] | None) -> str | None:
    """Nombre de pila del contacto, del payload de Chatwoot. Los teléfonos como nombre
    (contactos sin nombre en WhatsApp) no sirven para personalizar."""
    payload = payload or {}
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    name = str(sender.get("name") or contact.get("name") or "").strip()
    if not name or name.replace("+", "").replace(" ", "").isdigit():
        return None
    return name.split()[0]


if __name__ == "__main__":
    # Horario: puro, sin red. De lunes a lunes, de 8 a 22, feriados incluidos.
    assert en_horario_de_envio(datetime(2026, 8, 3, 8, 0, tzinfo=TZ)), "las 8 en punto sí"
    assert en_horario_de_envio(datetime(2026, 8, 3, 14, 30, tzinfo=TZ)), "la siesta también"
    assert en_horario_de_envio(datetime(2026, 8, 3, 21, 59, tzinfo=TZ))
    assert not en_horario_de_envio(datetime(2026, 8, 3, 22, 0, tzinfo=TZ)), "22:00 ya es tarde"
    assert not en_horario_de_envio(datetime(2026, 8, 3, 7, 59, tzinfo=TZ))
    assert en_horario_de_envio(datetime(2026, 8, 8, 18, 0, tzinfo=TZ)), "sábado sí"
    assert en_horario_de_envio(datetime(2026, 8, 9, 11, 0, tzinfo=TZ)), "domingo sí"
    assert en_horario_de_envio(datetime(2026, 12, 25, 11, 0, tzinfo=TZ)), "feriado también"

    # Recontacto largo: se manda en la última corrida útil antes de que cierre la ventana.
    # Caso típico: el cliente escribió el lunes 10:00 → la ventana cierra el martes 10:00 y el
    # límite (con 1h de colchón) es martes 09:00.
    cierre = datetime(2026, 8, 4, 10, 0, tzinfo=TZ)
    assert not momento_de_enviar(cierre, datetime(2026, 8, 3, 10, 30, tzinfo=TZ)), "recién atendido, no"
    assert not momento_de_enviar(cierre, datetime(2026, 8, 3, 21, 40, tzinfo=TZ)), "mañana hay otra chance"
    assert not momento_de_enviar(cierre, datetime(2026, 8, 4, 8, 0, tzinfo=TZ)), "todavía quedan corridas"
    assert not momento_de_enviar(cierre, datetime(2026, 8, 4, 8, 40, tzinfo=TZ)), "queda el tick de las 9"
    assert momento_de_enviar(cierre, datetime(2026, 8, 4, 9, 0, tzinfo=TZ)), "el límite en punto: ahora"
    assert momento_de_enviar(cierre, datetime(2026, 8, 4, 9, 20, tzinfo=TZ)), "pasado el límite: ahora o nunca"
    assert not momento_de_enviar(cierre, datetime(2026, 8, 4, 7, 0, tzinfo=TZ)), "fuera de horario"

    # Ventana que vence de madrugada (cliente escribió a las 6): la última chance es la noche
    # anterior, no hay corrida a las 5 de la mañana.
    cierre_madrugada = datetime(2026, 8, 4, 5, 0, tzinfo=TZ)
    assert not momento_de_enviar(cierre_madrugada, datetime(2026, 8, 3, 15, 0, tzinfo=TZ))
    assert momento_de_enviar(cierre_madrugada, datetime(2026, 8, 3, 21, 40, tzinfo=TZ)), "última del día"

    # Vetos.
    assert veto_por_etiquetas(["interesado", "bot_apagado"])
    assert veto_por_etiquetas(["interesado", "reclamo"])
    assert veto_por_etiquetas(["interesado", "envio"]) is None
    deriva = [AgentMessage(role="assistant", content="Te derivo con un vendedor de FerrePro.")]
    assert veto_por_conversacion(deriva, "compra")
    charla = [
        AgentMessage(role="user", content="hola, tenés amoladoras?"),
        AgentMessage(role="assistant", content="Sí: https://www.ferreproindustrial.com/productos/amoladora-x"),
    ]
    assert veto_por_conversacion(charla, "curioso") is None
    assert veto_por_conversacion([AgentMessage(role="user", content="hola")], "curioso")
    assert contact_name({"sender": {"name": "Juan Pérez"}}) == "Juan"
    assert contact_name({"sender": {"name": "+54 381 555 1234"}}) is None
    print("retargeting puro: OK")

    if not settings.openai_api_key:
        print("self-check vivo: SALTEADO (falta OPENAI_API_KEY)")
        raise SystemExit

    # Vivo: casos donde debe escribir y casos donde no.
    casos: list[tuple[bool, str, list[AgentMessage]]] = [
        (
            True,
            "quedó eligiendo entre opciones",
            [
                AgentMessage(role="user", content="hola, busco una amoladora de 4 1/2"),
                AgentMessage(role="assistant", content="Tengo la Bosch GWS 850 a $89.000 y la Einhell TE-AG 125 a $62.000. ¿Cuál te interesa?"),
            ],
        ),
        (
            False,
            "se despidió",
            [
                AgentMessage(role="user", content="cuánto sale el taladro Bosch?"),
                AgentMessage(role="assistant", content="El Bosch GSB 550 sale $75.000."),
                AgentMessage(role="user", content="ah ok, gracias, después veo. saludos"),
                AgentMessage(role="assistant", content="Dale, cualquier cosa avisame."),
            ],
        ),
        (
            False,
            "saludo suelto sin producto",
            [
                AgentMessage(role="user", content="hola"),
                AgentMessage(role="assistant", content="¡Hola! ¿Qué producto estás buscando?"),
            ],
        ),
    ]
    ok = 0
    for esperado, etiqueta, hist in casos:
        got = compose_followup(hist, nombre="Juan", stage="interesado")
        acerto = got.vale_la_pena == esperado
        ok += acerto
        print(f"  {'✓' if acerto else '✗'} {etiqueta}: vale={got.vale_la_pena} motivo={got.motivo}")
        if got.mensaje:
            print(f"      → {got.mensaje}")
    print(f"redactor: {ok}/{len(casos)}")
