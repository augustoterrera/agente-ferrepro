from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure

from . import notifier
from .config import settings

celery_app = Celery(
    "ferrepro",
    broker=settings.redis_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.chatwoot_tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    timezone=settings.celery_timezone,
    task_default_queue="default",
    task_routes={
        "app.tasks.chatwoot_tasks.process_chatwoot_conversation": {"queue": "chatwoot_messages"},
        "app.tasks.chatwoot_tasks.send_chatwoot_outbound_message": {"queue": "chatwoot_outbound"},
        "app.tasks.chatwoot_tasks.classify_and_label_conversation": {"queue": "chatwoot_outbound"},
        "app.tasks.chatwoot_tasks.retry_stale_processing_jobs": {"queue": "chatwoot_messages"},
        "app.tasks.chatwoot_tasks.requeue_stuck_conversation_jobs": {"queue": "chatwoot_messages"},
        "app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages": {"queue": "chatwoot_outbound"},
        "app.tasks.chatwoot_tasks.cleanup_expired_locks": {"queue": "chatwoot_messages"},
        "app.tasks.chatwoot_tasks.sweep_retargeting": {"queue": "chatwoot_outbound"},
    },
    beat_schedule={
        # Red de seguridad de la cola: si Redis pierde una task, el job persistido en la DB
        # se re-despacha desde acá. Robustez sin acoplar el flujo principal.
        "retry-stale-processing-jobs": {
            "task": "app.tasks.chatwoot_tasks.retry_stale_processing_jobs",
            "schedule": crontab(minute="*/5"),
        },
        "dispatch-pending-outbox-messages": {
            "task": "app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages",
            "schedule": crontab(minute="*/1"),
        },
        "requeue-stuck-conversation-jobs": {
            "task": "app.tasks.chatwoot_tasks.requeue_stuck_conversation_jobs",
            "schedule": crontab(minute="*/5"),
        },
        "cleanup-expired-locks": {
            "task": "app.tasks.chatwoot_tasks.cleanup_expired_locks",
            "schedule": crontab(minute="*/15"),
        },
        # Retargeting: el beat corre en hora de Tucumán (timezone de arriba). Este crontab solo
        # evita ticks al vacío de madrugada; el corte fino (8 a 22, todos los días) lo decide
        # en_horario_de_envio() dentro de la task.
        "sweep-retargeting": {
            "task": "app.tasks.chatwoot_tasks.sweep_retargeting",
            "schedule": crontab(minute=f"*/{settings.retargeting_sweep_minutes}", hour="8-21"),
        },
    },
)


# Sweepers idempotentes del beat: si fallan tras los retries (ej. Supabase intermitente más
# que la ventana de reintento), el próximo tick se recupera solo —los jobs/outbox viven en
# Supabase, no se pierde nada—. No alertamos para no spamear con blips transitorios; las tasks
# user-facing (process/send) sí alertan porque ahí sí queda un cliente sin respuesta.
_ALERTAS_SILENCIADAS = {
    "app.tasks.chatwoot_tasks.retry_stale_processing_jobs",
    "app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages",
    "app.tasks.chatwoot_tasks.requeue_stuck_conversation_jobs",
    "app.tasks.chatwoot_tasks.cleanup_expired_locks",
}


@task_failure.connect
def _alerta_telegram(sender=None, task_id=None, exception=None, args=None, kwargs=None, einfo=None, **extra):
    """Avisa por Telegram cuando una task agota reintentos y falla definitivamente. task_failure
    se dispara solo en el fallo FINAL (no por cada retry), así que no genera spam."""
    name = getattr(sender, "name", "?")
    if name in _ALERTAS_SILENCIADAS:
        return  # sweeper idempotente: se recupera solo en el próximo tick
    notifier.notify_error(
        f"task {name} falló",
        detalle=(einfo.traceback if einfo else str(exception)),
        contexto={"task_id": task_id, "args": args},
    )
