"""Celery application configuration and schedule definition."""
from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery_app = Celery(
    "vpn_private_workers",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.tasks.sync",
        "app.workers.tasks.notifications",
    ],
)

celery_app.conf.update(
    timezone="Asia/Tehran",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_time_limit=300,
    worker_prefetch_multiplier=1,
)

# Celery Beat Scheduled Tasks
celery_app.conf.beat_schedule = {
    "sync-traffic-every-5-minutes": {
        "task": "app.workers.tasks.sync.sync_all_panels_traffic",
        "schedule": 300.0, # Every 5 minutes
    },
    "check-expirations-every-10-minutes": {
        "task": "app.workers.tasks.sync.check_expired_subscriptions",
        "schedule": 600.0,
    },
    "monitor-panel-health-every-2-minutes": {
        "task": "app.workers.tasks.sync.check_panels_health",
        "schedule": 120.0,
    },
}
