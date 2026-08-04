from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Acceso a Supabase: SOLO REST + service_role key (no hay Postgres directo expuesto).
    # Todo —productos y chat_memory— va por PostgREST: las funciones SQL chat_* encapsulan
    # la lógica atómica/locks adentro, así que se invocan como RPC sin perder correctitud.
    supabase_url: str | None = None
    supabase_service_key: str | None = None

    # OpenAI: agente + embeddings de la query. El catálogo ya está embebido en Supabase
    # con text-embedding-3-small (1536 dims) → mismo modelo acá.
    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    # gpt-5-mini: barato y SÍ sigue las reglas condicionales (re-buscar sin stock, no
    # reetiquetar productos), donde gpt-4.1-mini/4o-mini fallaban. Contra: es de razonamiento,
    # ~14s por respuesta. Si necesitás más rapidez, gpt-4.1 (más caro pero ~3s) o tunear
    # reasoning_effort. Cambiable por env AGENT_MODEL.
    agent_model: str = "gpt-5-mini"
    # Esfuerzo de razonamiento (solo modelos gpt-5*/o*). "low" es el equilibrio: mantiene la
    # precisión (re-buscar sin stock, no reetiquetar) a ~10s. "minimal" baja a ~6s pero pierde
    # esos casos; subir a "medium"/"high" da ~14s. Vacío = default del modelo.
    agent_reasoning_effort: str | None = "low"
    # Clasificador de etiquetas: tarea simple (4 categorías) → modelo barato y rápido, sin razonar.
    classifier_model: str = "gpt-4.1-mini"
    # Transcripción de audios (WhatsApp → texto). Modelo barato de speech-to-text.
    transcription_model: str = "gpt-4o-mini-transcribe"
    # Etiqueta que, puesta por un humano en Chatwoot, silencia al bot (toma el control la persona).
    # Nombre como estado ("el bot está apagado"): al ponerla se apaga; mientras esté, sigue apagado.
    bot_apagado_label: str = "bot_apagado"

    # Búsqueda
    search_default_limit: int = 8
    # Híbrido (léxico+trigram+semántico). Sin openai_api_key cae a léxico solo (el RPC lo soporta).
    semantic_search_enabled: bool = True

    # Chatwoot (transporte)
    chatwoot_url: str | None = None
    chatwoot_account_id: int | None = None
    chatwoot_assignee_id: int | None = None
    chatwoot_access_token: str | None = None
    chatwoot_webhook_secret: str | None = None
    chatwoot_webhook_timestamp_tolerance_seconds: int = 300
    # En prod poner True: sin webhook secret, el arranque aborta en vez de aceptar POSTs sin firmar.
    require_webhook_secret: bool = False
    chatwoot_history_limit: int = 16
    chatwoot_agent_limit: int = 5
    # Comando administrativo /reset. Ambos valores deben coincidir con el webhook entrante.
    chat_reset_phone: str | None = None
    chat_reset_conversation_id: str | None = None
    # Namespace de conversaciones en las tablas chat_*. DB propia de Ferrepro → "chatwoot"
    # no colisiona con nadie.
    channel: str = "chatwoot"

    # Celery / Redis. Reusá el Redis de odranid apuntando a OTRA db index (ej. /2, /3) para
    # no compartir colas, o levantá uno propio (compose). Todo sale de estas envs.
    redis_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_timezone: str = "America/Argentina/Tucuman"
    chatwoot_debounce_seconds: int = 5
    chatwoot_debounce_retry_seconds: int = 3
    chatwoot_lock_seconds: int = 60
    chatwoot_job_max_retries: int = 5
    chatwoot_outbox_max_retries: int = 5
    chatwoot_stale_processing_minutes: int = 15

    # ── Retargeting (seguimiento one-shot a leads que se colgaron) ──────────────
    # Apagado por default: se prende recién después de validar a quién elige en dry-run.
    retargeting_enabled: bool = False
    # Dry-run: evalúa y redacta pero NO envía (guarda el mensaje en state.retargeting_dryrun).
    # Correlo así unos días y mirá /admin/retargeting-stats antes de escribirle a clientes reales.
    retargeting_dry_run: bool = True
    # Ventana de texto libre de WhatsApp Cloud API: 24h desde el último mensaje del CLIENTE.
    # Fuera de eso Meta rechaza el mensaje (haría falta una plantilla aprobada). El recontacto
    # se manda lo más tarde posible DENTRO de esa ventana (≈22-23h después), no a las pocas
    # horas: la idea es dar tiempo real a que el cliente conteste solo.
    retargeting_window_hours: float = 24
    # Colchón contra el cierre: no mandamos al filo (hay cola, reintentos y latencia de Meta).
    retargeting_safety_margin_hours: float = 1
    # Piso: nunca escribimos a alguien recién atendido.
    retargeting_min_silence_hours: float = 2
    # Cada cuánto corre el sweep. Tiene que coincidir con el crontab del beat (celery_app.py lo
    # deriva de acá): con esto se calcula si esta corrida es la última antes del cierre.
    # `*/N` reinicia en cada hora; N debe dividir 60 para que proxima_oportunidad() coincida
    # exactamente con los ticks reales del beat.
    retargeting_sweep_minutes: Literal[1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30] = 20
    retargeting_batch_limit: int = 25
    # Techo de follow-ups por día (rolling 24h). Rienda anti-blast si algo se descontrola.
    retargeting_daily_cap: int = 40
    # Juez + redactor del follow-up. gpt-5-mini por lo mismo que el agente: gpt-4.1-mini falla
    # las reglas condicionales (acá: cuándo NO escribir). Es batch, la latencia no importa.
    retargeting_model: str = "gpt-5-mini"
    retargeting_reasoning_effort: str | None = "low"
    # Etiqueta manual en Chatwoot para excluir una conversación del retargeting.
    no_retargeting_label: str = "no_retargeting"
    # Etiqueta que se agrega cuando el cliente contesta el follow-up (visibilidad para el vendedor).
    reactivado_label: str = "reactivado"

    # Token del endpoint /admin/*. Sin token, el endpoint responde 404 (no existe).
    admin_token: str | None = None

    # Meta / WhatsApp Cloud API + Catalogo. El catalogo lo alimenta Tienda Nube; el agente solo
    # mapea producto Supabase/Tienda Nube -> variante/content_id para mandar productos nativos.
    meta_access_token: str | None = None
    meta_phone_number_id: str | None = None
    meta_catalog_id: str | None = None
    meta_graph_version: str = "v23.0"

    # Alertas por Telegram (opcional). Sin token/chat → no-op. Avisa en fallos finales de tasks
    # y excepciones no manejadas de la API.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    alert_project: str = "agente-ferrepro"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
