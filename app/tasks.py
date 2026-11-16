"""Tarefas Celery. Arranque: celery -A app.tasks worker -B --loglevel=info"""
from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab

from .agent import Deferred
from .config import get_settings
from .deps import get_agent

log = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("apexpt_agent", broker=settings.redis_url, backend=None)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    timezone="Europe/Lisbon",
    beat_schedule={
        "publicar-posts-vencidos": {"task": "app.tasks.publish_due_posts", "schedule": 60.0},
        "renovar-token": {"task": "app.tasks.refresh_token", "schedule": crontab(hour=4, minute=17, day_of_week=1)},
    },
)


def _deferred(task, exc: Deferred):
    log.info("adiado %ss: %s", exc.countdown, exc)
    raise task.retry(countdown=exc.countdown, exc=exc, max_retries=48)


@celery_app.task(name="app.tasks.process_webhook")
def process_webhook(payload: dict) -> int:
    agent = get_agent()
    jobs = agent.ingest_webhook(payload)
    for kind, data in jobs:
        if kind == "comment":
            handle_comment.delay(data)
        elif kind == "dm":
            handle_dm.delay(data)
        elif kind == "mention":
            agent.notifier.alert(f"Nova menção à ApexPT: {data}")
    return len(jobs)


@celery_app.task(name="app.tasks.handle_comment", autoretry_for=(Exception,), retry_backoff=60,
                 retry_jitter=True, max_retries=3)
def handle_comment(data: dict) -> str:
    agent = get_agent()
    draft = agent.handle_comment(**data)
    if draft.status == "approved":  # decisão automática: publica com atraso natural
        publish_draft.apply_async(args=[draft.id], countdown=agent.reply_delay())
    return draft.status


@celery_app.task(name="app.tasks.handle_dm", autoretry_for=(Exception,), retry_backoff=60,
                 retry_jitter=True, max_retries=3)
def handle_dm(data: dict) -> str:
    agent = get_agent()
    draft = agent.handle_dm(**data)
    if draft.status == "approved":
        publish_draft.apply_async(args=[draft.id], countdown=min(agent.reply_delay(), 300))
    return draft.status


@celery_app.task(name="app.tasks.publish_draft", bind=True, max_retries=48)
def publish_draft(self, draft_id: int) -> str:
    try:
        return get_agent().publish_draft(draft_id)
    except Deferred as e:
        _deferred(self, e)


@celery_app.task(name="app.tasks.publish_due_posts")
def publish_due_posts() -> list[int]:
    ids = get_agent().claim_due_posts()
    for pid in ids:
        publish_post.delay(pid)
    return ids


@celery_app.task(name="app.tasks.publish_post")
def publish_post(post_id: int) -> str:
    return get_agent().publish_post(post_id)


@celery_app.task(name="app.tasks.refresh_token", autoretry_for=(Exception,), retry_backoff=600, max_retries=5)
def refresh_token() -> str:
    if settings.dry_run:  # teste local: não há token real para renovar
        return "dry_run"
    agent = get_agent()
    try:
        exp = agent.refresh_token()
    except Exception as e:
        agent.notifier.alert(f"Falha a renovar o token do Instagram: {e}")
        raise
    return exp.isoformat() if exp else ""
