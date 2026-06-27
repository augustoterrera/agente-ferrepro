from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

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
    },
)
