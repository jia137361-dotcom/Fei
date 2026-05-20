"""Nanobot task dispatcher.

Scans the ``scheduled_task`` table for work that is due, applies account-level
rate limits via ``AccountPolicy``, acquires advisory DB locks (per account),
and enqueues the corresponding Celery task on the correct queue.

Called by the Celery Beat ``dispatch_publish_jobs`` task or the equivalent
scheduler entry-point.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account_policy import AccountPolicy
from app.models.scheduled_task import ScheduledTask

logger = logging.getLogger(__name__)

_TASK_QUEUE_MAP = {
    "publish": "publish",
    "warmup": "publish",
    "warmup_and_publish": "publish",
    "generate_image": "media",
    "generate_video": "media",
    "auto_reply": "engagement",
    "refresh_trends": "trend",
    "cleanup": "trend",
    "reclaim_stale": "trend",
}

# Maps scheduled_task.task_type → registered Celery task name.
_TASK_NAME_MAP = {
    "publish": "app.jobs.publish_job",
    "warmup": "app.jobs.warmup",
    "warmup_and_publish": "app.jobs.warmup_and_publish",
    "generate_image": "app.jobs.generate_image_asset",
    "generate_video": "app.jobs.generate_marketing_video",
    "auto_reply": "app.jobs.auto_reply",
    "refresh_trends": "app.jobs.refresh_current_event_trends",
    "cleanup": "app.jobs.cleanup_assets",
    "reclaim_stale": "app.jobs.reclaim_stale_tasks",
}


def dispatch_ready_tasks(
    db: Session,
    *,
    limit: int = 20,
    dry_run: bool = False,
) -> dict:
    """Scan ``scheduled_task`` for ready work and enqueue Celery tasks.

    Returns a summary dict: ``{"dispatched": N, "skipped_cooldown": N, ...}``
    """
    from app.celery_app import celery_app

    now = datetime.now(UTC)
    batch_id = uuid4().hex[:16]
    dispatched = 0
    skipped: dict[str, int] = {"cooldown": 0, "out_of_window": 0, "locked": 0, "account_busy": 0}

    rows = list(
        db.scalars(
            select(ScheduledTask)
            .with_for_update(skip_locked=True)
            .where(
                ScheduledTask.status.in_(["pending", "ready", "scheduled"]),
                ScheduledTask.scheduled_at <= now,
                ScheduledTask.attempt_count < ScheduledTask.max_attempts,
                (ScheduledTask.next_retry_at == None)
                | (ScheduledTask.next_retry_at <= now),
                (ScheduledTask.lock_until == None)
                | (ScheduledTask.lock_until <= now),
            )
            .order_by(ScheduledTask.priority.desc(), ScheduledTask.scheduled_at.asc())
            .limit(limit)
        ).all()
    )

    # Phase 1 — atomic claim under FOR UPDATE SKIP LOCKED
    claimed: list[tuple[ScheduledTask, dict[str, object], str, str]] = []

    for task in rows:
        if task.account_id:
            policy = _get_policy(db, task.account_id)
            if not _can_run_now(task, policy, now, db):
                skipped["cooldown"] += 1
                continue
            if not _within_time_window(policy, now):
                skipped["out_of_window"] += 1
                continue

            existing_running = db.scalar(
                select(func.count(ScheduledTask.id)).where(
                    ScheduledTask.account_id == task.account_id,
                    ScheduledTask.status == "running",
                )
            )
            if existing_running:
                skipped["account_busy"] += 1
                continue

        task.status = "ready"
        task.locked_by = f"dispatch_{batch_id}"
        task.lock_until = now + timedelta(minutes=30)

        queue = _TASK_QUEUE_MAP.get(task.task_type, "publish")
        payload = dict(task.payload_json)
        payload["scheduled_task_id"] = task.task_id
        # Auto-inject account_id from DB row if the task payload is missing it
        if task.account_id and "account_id" not in payload:
            payload["account_id"] = task.account_id
        if task.task_type in ("publish", "warmup_and_publish"):
            payload.setdefault("dry_run", dry_run)
            payload.setdefault("content_batch_id", batch_id)
        elif task.task_type in ("auto_reply", "warmup"):
            payload.setdefault("dry_run", dry_run)

        celery_name = _TASK_NAME_MAP.get(task.task_type)
        if celery_name is None:
            logger.warning("No Celery task mapped for task_type=%s", task.task_type)
            continue

        claimed.append((task, payload, celery_name, queue))

    db.commit()  # release FOR UPDATE locks — claims are persisted

    # Phase 2 — enqueue Celery tasks outside the lock window
    for task, payload, celery_name, queue in claimed:
        try:
            async_result = celery_app.send_task(
                celery_name,
                kwargs=payload,
                queue=queue,
            )
        except Exception as exc:
            task.status = "pending"
            task.locked_by = None
            task.lock_until = None
            task.error_message = f"Dispatch send_task failed: {exc}"
            logger.error(
                "Failed to send Celery task for task_id=%s type=%s: %s",
                task.task_id,
                task.task_type,
                exc,
            )
            continue
        task.celery_task_id = getattr(async_result, "id", None)
        task.status = "running"
        task.started_at = now
        task.attempt_count += 1
        task.heartbeat_at = now
        dispatched += 1
        logger.info(
            "Dispatched task_id=%s type=%s queue=%s celery=%s",
            task.task_id,
            task.task_type,
            queue,
            task.celery_task_id,
        )
    db.commit()

    return {
        "dispatched": dispatched,
        "skipped": skipped,
        "batch_id": batch_id,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _get_policy(db: Session, account_id: str) -> AccountPolicy | None:
    return db.scalar(
        select(AccountPolicy).where(AccountPolicy.account_id == account_id)
    )


def _can_run_now(task: ScheduledTask, policy: AccountPolicy | None, now: datetime, db: Session) -> bool:
    if policy is None:
        return True
    if policy.cooldown_until and now < policy.cooldown_until.astimezone(UTC):
        logger.debug(
            "Account %s in cooldown until %s", task.account_id, policy.cooldown_until
        )
        return False

    # daily post limit check
    if task.task_type in ("publish", "warmup_and_publish"):
        if task.account_id is None:
            return False
        today_posts = _count_posts_today(db, task.account_id, now)
        if today_posts >= policy.daily_max_posts:
            logger.debug(
                "Account %s reached daily max posts (%d/%d)",
                task.account_id,
                today_posts,
                policy.daily_max_posts,
            )
            return False

        # min post interval check
        if policy.min_post_interval_min > 0:
            last = _last_publish_time(db, task.account_id)
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < policy.min_post_interval_min * 60:
                    logger.debug(
                        "Account %s min interval not met (elapsed=%ds, min=%ds)",
                        task.account_id,
                        int(elapsed),
                        policy.min_post_interval_min * 60,
                    )
                    return False

    return True


def _within_time_window(policy: AccountPolicy | None, now: datetime) -> bool:
    if policy is None:
        return True
    if not policy.allowed_timezone_start or not policy.allowed_timezone_end:
        return True

    settings = get_settings()
    local_now = now.astimezone(ZoneInfo(settings.scheduler_timezone))
    current_time = local_now.strftime("%H:%M")
    start = policy.allowed_timezone_start
    end = policy.allowed_timezone_end

    if start <= end:
        return start <= current_time <= end
    else:
        # overnight window, e.g. 22:00-09:00
        return current_time >= start or current_time <= end


def _count_posts_today(db: Session, account_id: str, now: datetime) -> int:
    """Count posts published by this account today from scheduled_task."""
    tz = ZoneInfo(get_settings().scheduler_timezone)
    local_now = now.astimezone(tz)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today.astimezone(UTC)
    return db.scalar(
        select(func.count(ScheduledTask.id)).where(
            ScheduledTask.account_id == account_id,
            ScheduledTask.task_type.in_(["publish", "warmup_and_publish"]),
            ScheduledTask.status.in_(["completed", "published"]),
            ScheduledTask.finished_at >= today_utc,
        )
    ) or 0


def _last_publish_time(db: Session, account_id: str) -> datetime | None:
    """Return the most recent publish finish time for *account_id* from
    scheduled_task.  Returns None if the account has never published."""
    return db.scalar(
        select(ScheduledTask.finished_at)
        .where(
            ScheduledTask.account_id == account_id,
            ScheduledTask.task_type.in_(["publish", "warmup_and_publish"]),
            ScheduledTask.status.in_(["completed", "published"]),
            ScheduledTask.finished_at != None,  # noqa: E711
        )
        .order_by(ScheduledTask.finished_at.desc())
        .limit(1)
    )
