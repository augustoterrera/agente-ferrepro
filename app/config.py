from __future__ import annotations

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
    # Etiqueta que, puesta por un humano en Chatwoot, silencia al bot (toma el control la persona).
    apagar_bot_label: str = "apagar_bot"

    # Búsqueda
    search_default_limit: int = 8
    # Híbrido (léxico+trigram+semántico). Sin openai_api_key cae a léxico solo (el RPC lo soporta).
    semantic_search_enabled: bool = True

    # Chatwoot (transporte)
    chatwoot_url: str | None = None
    chatwoot_account_id: int | None = None
    chatwoot_access_token: str | None = None
    chatwoot_webhook_secret: str | None = None
    chatwoot_webhook_timestamp_tolerance_seconds: int = 300
    # En prod poner True: sin webhook secret, el arranque aborta en vez de aceptar POSTs sin firmar.
    require_webhook_secret: bool = False
    chatwoot_history_limit: int = 16
    chatwoot_agent_limit: int = 5
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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
