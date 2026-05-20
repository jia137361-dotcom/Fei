#!/usr/bin/env python
"""MCP server exposing Pinterest POD Agent operations to nanobot.

Usage (nanobot config.json mcpServers entry):
    {
      "pinterest-pod": {
        "command": ".venv\\Scripts\\python.exe",
        "args": ["scripts\\nanobot_mcp_server.py"],
        "toolTimeout": 60,
        "enabledTools": ["*"]
      }
    }
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# -- ensure project root is on sys.path ---------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger("nanobot_mcp_server")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [mcp] %(message)s")

mcp = FastMCP("pinterest-pod-agent", log_level="INFO")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _db():
    from app.database import get_sessionmaker

    return get_sessionmaker()()


# ---------------------------------------------------------------------------
# 1. create_publish_task
# ---------------------------------------------------------------------------


@mcp.tool()
def create_publish_task(
    account_id: str,
    job_id: str,
    warmup_duration_minutes: int = 5,
    scheduled_at: str | None = None,
    priority: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a warmup_and_publish scheduled_task for a specific account + job.

    The task will be picked up by the dispatcher at its scheduled time.
    Set dry_run=False for production publishing.
    """
    from uuid import uuid4

    from app.models.publish_job import PublishJob
    from app.models.scheduled_task import ScheduledTask
    from app.models.social_account import SocialAccount

    db = _db()
    try:
        from sqlalchemy import select

        # Validate account exists and has AdsPower profile
        account = db.scalar(
            select(SocialAccount).where(SocialAccount.account_id == account_id)
        )
        if account is None:
            return {"error": f"Account not found: {account_id}"}
        if not account.adspower_profile_id:
            return {"error": f"No AdsPower profile bound for account: {account_id}"}

        # Validate publish_job exists and has image
        job = db.scalar(
            select(PublishJob).where(PublishJob.job_id == job_id)
        )
        if job is None:
            return {"error": f"Publish job not found: {job_id}"}
        if job.status in ("cancelled", "published", "failed"):
            return {"error": f"Publish job already finalized: {job_id} (status={job.status})"}

        now = _now()
        task = ScheduledTask(
            task_id=f"st_{uuid4().hex[:16]}",
            task_type="warmup_and_publish",
            account_id=account_id,
            status="pending",
            priority=priority,
            scheduled_at=datetime.fromisoformat(scheduled_at) if scheduled_at else now,
            payload_json={
                "account_id": account_id,
                "job_id": job_id,
                "warmup_duration_minutes": warmup_duration_minutes,
                "dry_run": dry_run,
            },
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "account_id": task.account_id,
            "status": task.status,
            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
            "dry_run": dry_run,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. dispatch_ready_tasks
# ---------------------------------------------------------------------------


@mcp.tool()
def dispatch_ready_tasks(
    limit: int = 20,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Manually trigger the task dispatcher NOW (bypasses Beat schedule).

    Directly calls dispatch_ready_tasks() — synchronous, no Celery relay.
    Set dry_run=False for production.  Returns the dispatch summary.
    """
    from app.jobs.dispatcher import dispatch_ready_tasks as _dispatch

    db = _db()
    try:
        result = _dispatch(db, limit=limit, dry_run=dry_run)
        db.commit()
        return result
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. get_task_status
# ---------------------------------------------------------------------------


@mcp.tool()
def get_task_status(task_id: str) -> dict[str, Any]:
    """Get detailed info for a single task including payload, result, errors."""
    from app.models.scheduled_task import ScheduledTask

    db = _db()
    try:
        from sqlalchemy import select

        t = db.scalar(
            select(ScheduledTask).where(ScheduledTask.task_id == task_id)
        )
        if t is None:
            return {"error": f"Task not found: {task_id}"}
        result = t.result_json or {}
        return {
            "task_id": t.task_id,
            "task_type": t.task_type,
            "account_id": t.account_id,
            "campaign_id": t.campaign_id,
            "status": t.status,
            "priority": t.priority,
            "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            "attempt_count": t.attempt_count,
            "max_attempts": t.max_attempts,
            "celery_task_id": t.celery_task_id,
            "error_message": t.error_message,
            "error_type": t.error_type,
            "payload": t.payload_json,
            "result": t.result_json,
            "pin_url": result.get("pin_url"),
            "publish_evidence": result.get("publish_evidence"),
            "debug_artifact_dir": result.get("debug_artifact_dir"),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. get_account_runtime_status
# ---------------------------------------------------------------------------


@mcp.tool()
def get_account_runtime_status() -> dict[str, Any]:
    """Today's overview: per-account completed/running/pending/failed counts."""
    from app.models.scheduled_task import ScheduledTask

    db = _db()
    try:
        from sqlalchemy import func, select

        today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = list(
            db.execute(
                select(
                    ScheduledTask.account_id,
                    ScheduledTask.status,
                    func.count(ScheduledTask.id),
                )
                .where(
                    (ScheduledTask.scheduled_at >= today)
                    | (ScheduledTask.status.in_(["running"]))
                )
                .group_by(ScheduledTask.account_id, ScheduledTask.status)
            ).all()
        )

        by_account: dict[str, dict[str, int]] = {}
        for row in rows:
            aid = row.account_id or "(no account)"
            if aid not in by_account:
                by_account[aid] = {}
            by_account[aid][row.status] = row.count

        return {
            "date": today.date().isoformat(),
            "accounts": {
                aid: {
                    "completed": counts.get("completed", 0),
                    "running": counts.get("running", 0),
                    "pending": counts.get("pending", 0),
                    "failed": counts.get("failed", 0),
                    "total": (
                        counts.get("completed", 0)
                        + counts.get("running", 0)
                        + counts.get("pending", 0)
                        + counts.get("failed", 0)
                    ),
                }
                for aid, counts in by_account.items()
            },
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. store_trend_signals
# ---------------------------------------------------------------------------


@mcp.tool()
def store_trend_signals(
    scope: str,
    bucket: str,
    signals: list[dict[str, Any]],
) -> dict[str, Any]:
    """Store trend keywords collected by nanobot into the global_strategy table.

    *scope*: typically an account_id or niche name
    *bucket*: "current_event_trends" or "product_trends"
    *signals*: list of {keyword, weight, source, metadata?}

    These keywords are then consumed by EvoMap prompt generation.
    """
    from app.evomap.strategy_matrix import get_strategy, upsert_strategy

    db = _db()
    try:
        strategy = get_strategy(db, scope)
        normalized = [
            {
                "keyword": " ".join(s.get("keyword", "").lower().split()),
                "source": s.get("source", "nanobot"),
                "weight": s.get("weight", 1.0),
                "metadata": s.get("metadata", {}),
            }
            for s in signals
            if s.get("keyword", "").strip()
        ]
        if not normalized:
            return {"stored": 0, "message": "No valid keyword signals provided"}

        strategy[bucket] = normalized
        strategy["trend_keywords"] = sorted(
            {item["keyword"] for item in normalized}
        )

        history = strategy.get("trend_history", [])
        history.append(
            {
                "source": bucket,
                "count": len(normalized),
                "recorded_at": _now().isoformat(),
            }
        )
        strategy["trend_history"] = history[-50:]
        short_bucket = {"current_event_trends": "cet", "product_trends": "pt"}.get(
            bucket, bucket[:8]
        )
        strategy["version"] = f"nb_{short_bucket}_{_now().strftime('%Y%m%d%H%M')}"

        upsert_strategy(db, scope, strategy, version=strategy["version"])
        db.commit()

        return {
            "stored": len(normalized),
            "bucket": bucket,
            "scope": scope,
            "keywords": strategy["trend_keywords"],
            "version": strategy["version"],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5a. content templates
# ---------------------------------------------------------------------------


@mcp.tool()
def upsert_content_template(
    scope: str,
    template_type: str,
    template_text: str,
    is_active: bool = True,
) -> dict[str, Any]:
    """Create or update an EvoMap content template.

    template_type must be "title_description" or "image_prompt".
    scope can be a niche, product_type, "niche:product_type", or "global".
    """
    if template_type not in {"title_description", "image_prompt"}:
        return {"error": "template_type must be title_description or image_prompt"}

    from sqlalchemy import select

    from app.models.content_template import ContentTemplate

    db = _db()
    try:
        template = db.scalar(
            select(ContentTemplate).where(
                ContentTemplate.scope == scope,
                ContentTemplate.template_type == template_type,
            )
        )
        if template is None:
            template = ContentTemplate(
                scope=scope,
                template_type=template_type,
                template_text=template_text,
                is_active=is_active,
            )
            db.add(template)
        else:
            template.template_text = template_text
            template.is_active = is_active
        db.commit()
        db.refresh(template)
        return {
            "id": template.id,
            "scope": template.scope,
            "template_type": template.template_type,
            "is_active": template.is_active,
        }
    finally:
        db.close()


@mcp.tool()
def list_content_templates(
    scope: str | None = None,
    template_type: str | None = None,
) -> dict[str, Any]:
    """List EvoMap content templates."""
    from sqlalchemy import select

    from app.models.content_template import ContentTemplate

    db = _db()
    try:
        stmt = select(ContentTemplate).order_by(ContentTemplate.scope, ContentTemplate.template_type)
        if scope:
            stmt = stmt.where(ContentTemplate.scope == scope)
        if template_type:
            stmt = stmt.where(ContentTemplate.template_type == template_type)
        rows = list(db.scalars(stmt).all())
        return {
            "templates": [
                {
                    "id": row.id,
                    "scope": row.scope,
                    "template_type": row.template_type,
                    "template_text": row.template_text,
                    "is_active": row.is_active,
                }
                for row in rows
            ]
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. publish_pin_direct (forced dry_run=True)
# ---------------------------------------------------------------------------


@mcp.tool()
def publish_pin_direct(
    account_id: str,
    job_id: str,
    dry_run: bool = True,
    warmup_duration_minutes: int = 0,
    bypass_scheduler: bool = False,
) -> dict[str, Any]:
    """Publish a Pin immediately via warmup_and_publish.

    Defaults to dry_run=True for safety. Set dry_run=False for production.
    By default, creates a trackable scheduled_task so get_task_status works.
    Pass bypass_scheduler=True for the old direct-dispatch behavior.
    """
    from uuid import uuid4

    from app.models.publish_job import PublishJob
    from app.models.scheduled_task import ScheduledTask
    from app.models.social_account import SocialAccount

    db = _db()
    try:
        from sqlalchemy import select

        account = db.scalar(
            select(SocialAccount).where(SocialAccount.account_id == account_id)
        )
        if account is None:
            return {"error": f"Account not found: {account_id}"}
        if not account.adspower_profile_id:
            return {"error": f"No AdsPower profile bound for account: {account_id}"}

        job = db.scalar(
            select(PublishJob).where(PublishJob.job_id == job_id)
        )
        if job is None:
            return {"error": f"Publish job not found: {job_id}"}
        if job.status in ("cancelled", "published", "failed"):
            return {"error": f"Publish job already finalized: {job_id} (status={job.status})"}

        if bypass_scheduler:
            db.close()
            from app.jobs.tasks import warmup_and_publish_task

            result = warmup_and_publish_task.delay(
                account_id=account_id,
                job_id=job_id,
                warmup_duration_minutes=warmup_duration_minutes,
                dry_run=dry_run,
            )
            return {
                "celery_task_id": result.id,
                "scheduled_task_id": None,
                "job_id": job_id,
                "account_id": account_id,
                "dry_run": dry_run,
                "bypass_scheduler": True,
                "note": "Task dispatched without scheduled_task tracking. Use Celery task_id to monitor.",
            }

        # Normal path: create a scheduled_task record for full tracking
        now = _now()
        task = ScheduledTask(
            task_id=f"st_{uuid4().hex[:16]}",
            task_type="warmup_and_publish",
            account_id=account_id,
            status="pending",
            priority=8,  # direct publish gets higher priority
            scheduled_at=now,
            payload_json={
                "account_id": account_id,
                "job_id": job_id,
                "warmup_duration_minutes": warmup_duration_minutes,
                "dry_run": dry_run,
            },
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        saved_task_id = task.task_id

        # Dispatch immediately
        from app.jobs.dispatcher import dispatch_ready_tasks

        dispatch_result = dispatch_ready_tasks(db, limit=1, dry_run=dry_run)
        db.commit()

        return {
            "scheduled_task_id": saved_task_id,
            "job_id": job_id,
            "account_id": account_id,
            "dry_run": dry_run,
            "dispatched": dispatch_result.get("dispatched", 0),
            "note": "Use get_task_status with scheduled_task_id to track progress",
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 7. auto_schedule_daily
# ---------------------------------------------------------------------------


@mcp.tool()
def auto_schedule_daily(
    account_ids: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Generate today's task schedule: warmup_and_publish for each account.

    Only creates warmup_and_publish tasks when ready publish_jobs exist.
    Does NOT fall back to standalone warmup — time slots without ready jobs
    are simply skipped.  Pass force=True to override idempotency check.
    """
    from uuid import uuid4

    from app.models.account_policy import AccountPolicy
    from app.models.scheduled_task import ScheduledTask
    from app.models.social_account import SocialAccount

    db = _db()
    try:
        from sqlalchemy import func, select

        stmt = select(SocialAccount)
        if account_ids:
            stmt = stmt.where(SocialAccount.account_id.in_(account_ids))
        accounts = list(db.scalars(stmt).all())

        if not accounts:
            return {"scheduled": 0, "message": "No accounts found", "accounts": []}

        now = _now()
        base_hour = now.hour + 1

        summary: list[dict[str, Any]] = []
        scheduled_count = 0

        # idempotency check (skipped when force=True)
        if not force:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            existing_task_count = db.scalar(
                select(func.count(ScheduledTask.id)).where(
                    ScheduledTask.scheduled_at >= today_start,
                    ScheduledTask.scheduled_at < today_start + timedelta(days=1),
                    ScheduledTask.status.in_(["pending", "ready", "scheduled"]),
                )
            ) or 0
            if existing_task_count > 0:
                return {
                    "scheduled": 0,
                    "message": f"Tasks already scheduled for today ({existing_task_count} pending). Use force=True to override.",
                    "accounts": [],
                }

        for idx, account in enumerate(accounts):
            policy = db.scalar(
                select(AccountPolicy).where(
                    AccountPolicy.account_id == account.account_id
                )
            )
            daily_posts = policy.daily_max_posts if policy else 2
            warmup_sessions = policy.warmup_sessions_per_day if policy else 2
            warmup_duration = policy.warmup_duration_min if policy else 5

            acc_summary: dict[str, Any] = {
                "account_id": account.account_id,
                "tasks": [],
            }

            offset_h = idx * 2

            from app.models.publish_job import PublishJob

            ready_jobs = list(
                db.scalars(
                    select(PublishJob)
                    .where(
                        PublishJob.account_id == account.account_id,
                        PublishJob.status.in_(["pending", "ready"]),
                    )
                    .order_by(PublishJob.created_at.asc())
                ).all()
            )

            for s in range(min(warmup_sessions, daily_posts)):
                scheduled_hour = (base_hour + offset_h + s * 4) % 24
                scheduled_at = now.replace(
                    hour=scheduled_hour, minute=30, second=0, microsecond=0
                )
                if scheduled_at <= now:
                    continue

                if not ready_jobs:
                    # No ready publish_job — skip this time slot
                    continue

                publish_job = ready_jobs.pop(0)
                task_id = f"st_{uuid4().hex[:16]}"
                payload = {
                    "account_id": account.account_id,
                    "warmup_duration_minutes": warmup_duration,
                    "job_id": publish_job.job_id,
                }
                task = ScheduledTask(
                    task_id=task_id,
                    task_type="warmup_and_publish",
                    account_id=account.account_id,
                    status="pending",
                    priority=5,
                    scheduled_at=scheduled_at,
                    payload_json=payload,
                )
                db.add(task)
                scheduled_count += 1
                acc_summary["tasks"].append(
                    {
                        "task_id": task_id,
                        "type": "warmup_and_publish",
                        "scheduled_at": scheduled_at.isoformat(),
                        "warmup_min": warmup_duration,
                        "job_id": publish_job.job_id,
                    }
                )

            # auto-reply: one pass per day in the evening
            if policy and policy.auto_reply_enabled:
                reply_hour = (base_hour + offset_h + 8) % 24
                reply_at = now.replace(
                    hour=reply_hour, minute=0, second=0, microsecond=0
                )
                if reply_at > now:
                    task_id = f"st_{uuid4().hex[:16]}"
                    task = ScheduledTask(
                        task_id=task_id,
                        task_type="auto_reply",
                        account_id=account.account_id,
                        status="pending",
                        priority=3,
                        scheduled_at=reply_at,
                        payload_json={
                            "account_id": account.account_id,
                            "dry_run": dry_run,
                            "limit": 20,
                        },
                    )
                    db.add(task)
                    scheduled_count += 1
                    acc_summary["tasks"].append(
                        {
                            "task_id": task_id,
                            "type": "auto_reply",
                            "scheduled_at": reply_at.isoformat(),
                        }
                    )

            db.commit()
            summary.append(acc_summary)

        return {
            "scheduled": scheduled_count,
            "dry_run": dry_run,
            "accounts": summary,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 8. operational status and trend helpers
# ---------------------------------------------------------------------------


@mcp.tool()
def check_health() -> dict[str, Any]:
    """Check PostgreSQL, Redis, and AdsPower local API reachability."""
    health: dict[str, Any] = {"status": "ok", "checks": {}}

    try:
        from sqlalchemy import text

        db = _db()
        try:
            db.execute(text("SELECT 1"))
            health["checks"]["database"] = {"ok": True}
        finally:
            db.close()
    except Exception as exc:
        health["status"] = "degraded"
        health["checks"]["database"] = {"ok": False, "error": str(exc)}

    try:
        import redis

        from app.config import get_settings

        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            health["checks"]["redis"] = {"ok": bool(r.ping())}
        finally:
            r.close()
    except Exception as exc:
        health["status"] = "degraded"
        health["checks"]["redis"] = {"ok": False, "error": str(exc)}

    try:
        from app.tools.adspower_api import AdsPowerClient

        client = AdsPowerClient(timeout_seconds=5.0)
        profiles = client.list_profiles()
        health["checks"]["adspower"] = {
            "ok": True,
            "profile_count": len(profiles),
        }
    except Exception as exc:
        health["status"] = "degraded"
        msg = str(exc)
        if "401" in msg or "403" in msg:
            detail = "AdsPower API key rejected"
        elif "Connection" in msg or "connect" in msg.lower():
            detail = "AdsPower not running or unreachable"
        else:
            detail = msg[:200]
        health["checks"]["adspower"] = {"ok": False, "error": detail}

    return health


@mcp.tool()
def check_account_proxies(account_ids: list[str] | None = None) -> dict[str, Any]:
    """Check account to AdsPower profile bindings and local profile status."""
    from sqlalchemy import select

    from app.models.social_account import SocialAccount
    from app.tools.adspower_api import AdsPowerClient

    db = _db()
    try:
        stmt = select(SocialAccount)
        if account_ids:
            stmt = stmt.where(SocialAccount.account_id.in_(account_ids))
        accounts = list(db.scalars(stmt).all())
    finally:
        db.close()

    client = AdsPowerClient(timeout_seconds=5.0)
    results: list[dict[str, Any]] = []
    for account in accounts:
        item: dict[str, Any] = {
            "account_id": account.account_id,
            "proxy_region": account.proxy_region,
            "adspower_profile_id": account.adspower_profile_id,
            "ok": False,
        }
        if not account.adspower_profile_id:
            item["error"] = "No AdsPower profile bound"
            results.append(item)
            continue
        try:
            status = client.get_profile_status(account.adspower_profile_id)
            item.update(
                {
                    "ok": True,
                    "profile_status": status.status,
                    "debug_port": status.debug_port,
                    "has_browser_endpoint": bool(status.ws_puppeteer),
                }
            )
        except Exception as exc:
            item["error"] = str(exc)
        results.append(item)

    return {"checked": len(results), "accounts": results}


@mcp.tool()
def list_tasks(
    account_id: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List scheduled tasks by account/status/type."""
    from sqlalchemy import select

    from app.models.scheduled_task import ScheduledTask

    db = _db()
    try:
        stmt = select(ScheduledTask).order_by(ScheduledTask.scheduled_at.desc()).limit(limit)
        if account_id:
            stmt = stmt.where(ScheduledTask.account_id == account_id)
        if status:
            stmt = stmt.where(ScheduledTask.status == status)
        if task_type:
            stmt = stmt.where(ScheduledTask.task_type == task_type)
        rows = list(db.scalars(stmt).all())
        return {
            "items": [
                {
                    "task_id": t.task_id,
                    "task_type": t.task_type,
                    "account_id": t.account_id,
                    "status": t.status,
                    "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                    "error_message": t.error_message,
                }
                for t in rows
            ],
            "count": len(rows),
        }
    finally:
        db.close()


@mcp.tool()
def get_task_detail(task_id: str) -> dict[str, Any]:
    """Alias for get_task_status with a clearer nanobot-facing name."""
    return get_task_status(task_id)


@mcp.tool()
def get_recent_errors(hours: int = 4, limit: int = 20) -> dict[str, Any]:
    """Return recent failed scheduled tasks."""
    from sqlalchemy import select

    from app.models.scheduled_task import ScheduledTask

    cutoff = _now() - timedelta(hours=hours)
    db = _db()
    try:
        rows = list(
            db.scalars(
                select(ScheduledTask)
                .where(
                    ScheduledTask.status == "failed",
                    ScheduledTask.finished_at >= cutoff,
                )
                .order_by(ScheduledTask.finished_at.desc())
                .limit(limit)
            ).all()
        )
        return {
            "hours": hours,
            "items": [
                {
                    "task_id": t.task_id,
                    "task_type": t.task_type,
                    "account_id": t.account_id,
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                    "error_type": t.error_type,
                    "error_message": t.error_message,
                }
                for t in rows
            ],
        }
    finally:
        db.close()


@mcp.tool()
def get_status_dashboard() -> dict[str, Any]:
    """Today's per-account task dashboard."""
    return get_account_runtime_status()


@mcp.tool()
def get_trend_snapshot(scope: str) -> dict[str, Any]:
    """Read the stored trend strategy snapshot for a scope."""
    from app.evomap.strategy_matrix import get_strategy

    db = _db()
    try:
        return {"scope": scope, "strategy": get_strategy(db, scope)}
    finally:
        db.close()


@mcp.tool()
def refresh_pinterest_trends(
    scope: str,
    trend_type: str = "product",
    query: str | None = None,
    niche: str | None = None,
    product_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Queue a Pinterest trend refresh task."""
    from app.jobs.tasks import refresh_current_event_trends_task, refresh_product_trends_task

    if trend_type == "current_events":
        result = refresh_current_event_trends_task.delay(scope, query, limit)
        task_name = "app.jobs.refresh_current_event_trends"
    else:
        result = refresh_product_trends_task.delay(scope, niche, product_type, limit)
        task_name = "app.jobs.refresh_product_trends"
    return {
        "celery_task_id": getattr(result, "id", None),
        "task_name": task_name,
        "scope": scope,
        "trend_type": trend_type,
    }


@mcp.tool()
def generate_image(
    prompt: str,
    image_size: str = '{"width": 800, "height": 1200}',
) -> dict[str, Any]:
    """Queue a Fal.ai image generation task."""
    from app.jobs.tasks import generate_image_asset_task

    result = generate_image_asset_task.delay(prompt, image_size)
    return {
        "celery_task_id": getattr(result, "id", None),
        "task_name": "app.jobs.generate_image_asset",
        "prompt": prompt,
        "image_size": image_size,
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
