from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "buybring",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.voice_note_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_concurrency=2,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.voice_note_tasks.process_voice_note": {"queue": "voice_notes"},
    },
)
