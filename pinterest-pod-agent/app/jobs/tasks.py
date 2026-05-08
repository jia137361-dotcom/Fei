from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.celery_app import celery_app, run_async
from app.config import get_settings
from app.database import get_sessionmaker
from app.evomap.prompt_evolve import PromptContext
from app.models.publish_job import PublishJob
from app.safety.errors import FatalError, classify_exception
from app.workflows.auto_reply_flow import run_auto_reply_flow
from app.workflows.image_generation_flow import generate_image_asset
from app.workflows.pin_publish_flow import AccountPublishWorkflowInput, run_pin_publish_with_adspower
from app.workflows.trend_tracking_flow import refresh_current_event_trends, refresh_product_trends
from app.workflows.video_generation_flow import VideoGenerationInput, generate_marketing_video
from app.workflows.warmup_flow import run_warmup_session

logger = logging.getLogger(__name__)

DEFAULT_RETRY_BACKOFF = 60        # seconds, multiplied by 2^attempt


def _update_heartbeat(
    scheduled_task_id: str | None,
    account_id: str | None = None,
    profile_id: str | None = None,
) -> None:
    """Update heartbeat_at on the scheduled_task and renew Redis lock TTLs."""
    if not scheduled_task_id:
        return
    from app.models.scheduled_task import ScheduledTask

    with get_sessionmaker()() as db:
        st = db.scalar(
            select(ScheduledTask).where(ScheduledTask.task_id == scheduled_task_id)
        )
        if st and st.status == "running":
            st.heartbeat_at = datetime.now(UTC)
            db.commit()

    if account_id:
        from app.safety.locks import renew_locks_once_sync

        try:
            renew_locks_once_sync(account_id, profile_id)
        except Exception:
            logger.debug(
                "Failed to renew Redis locks account=%s — non-fatal", account_id,
                exc_info=True,
            )


def _st_writeback(
    scheduled_task_id: str | None,
    status: str,
    *,
    result_json: dict[str, Any] | None = None,
    error_message: str | None = None,
    error_type: str | None = None,
) -> None:
    """Write completion/failure back to the scheduled_task row that spawned this
    Celery task.  No-op when *scheduled_task_id* is None (e.g. directly-invoked
    tasks that did not originate from the dispatcher).
    """
    if not scheduled_task_id:
        return
    from app.models.scheduled_task import ScheduledTask

    with get_sessionmaker()() as db:
        st = db.scalar(
            select(ScheduledTask).where(ScheduledTask.task_id == scheduled_task_id)
        )
        if st is None:
            return
        st.status = status
        st.finished_at = datetime.now(UTC)
        st.locked_by = None
        st.lock_until = None
        st.celery_task_id = None
        if result_json is not None:
            st.result_json = result_json
        if error_message is not None:
            st.error_message = error_message
        if error_type is not None:
            st.error_type = error_type
        db.commit()


def _check_final_retry_and_writeback(
    scheduled_task_id: str | None,
    *,
    error_message: str | None = None,
    error_type: str | None = None,
) -> None:
    """Write 'failed' when Celery has exhausted all retry attempts.

    On intermediate attempts, update next_retry_at + error_message so
    reclaim knows the task is still being retried by Celery rather than
    stranded in 'running'.
    """
    if not scheduled_task_id:
        return
    from celery import current_task

    from app.models.scheduled_task import ScheduledTask

    task_req = current_task.request if current_task else None
    now = datetime.now(UTC)

    with get_sessionmaker()() as db:
        st = db.scalar(
            select(ScheduledTask).where(ScheduledTask.task_id == scheduled_task_id)
        )
        if st is None:
            return

        if task_req is not None:
            retries = task_req.retries
            max_retries = getattr(task_req, "max_retries", 0)
            if retries >= max_retries:
                st.status = "failed"
                st.finished_at = now
                st.locked_by = None
                st.lock_until = None
                st.celery_task_id = None
                if error_type == "retryable":
                    error_type = "final_failed"
            else:
                # Intermediate retry — keep status=running but signal Celery
                # is still retrying via next_retry_at so reclaim won't kill it.
                retry_delay = getattr(task_req, "default_retry_delay", 60)
                backoff = min(2 ** retries * retry_delay, 600)
                st.next_retry_at = now + timedelta(seconds=backoff)
        else:
            st.status = "failed"
            st.finished_at = now
            st.locked_by = None
            st.lock_until = None
            st.celery_task_id = None

        if error_message is not None:
            st.error_message = error_message
        if error_type is not None:
            st.error_type = error_type
        db.commit()


def _mark_publish_job_failed(job_id: str, error_message: str) -> None:
    with get_sessionmaker()() as db:
        job = db.scalar(select(PublishJob).where(PublishJob.job_id == job_id))
        if job is None:
            return
        job.status = "failed"
        job.error_message = error_message
        job.finished_at = datetime.now(UTC)
        db.commit()


# ---------------------------------------------------------------------------
# helpers: regenerate content + image fresh each publish
# ---------------------------------------------------------------------------


async def _regenerate_content(
    job: PublishJob,
    account_id: str,
    db: Any,
) -> None:
    """Regenerate title + description via EvoMap, with dedup against history.

    Overwrites job.title, job.description, job.*_hash in-place and commits.
    Falls back to existing content when LLM is unavailable.
    """
    from app.config import get_settings

    SENTINEL = "pending_auto_generation"
    has_existing = (
        job.title
        and job.description
        and job.title != SENTINEL
        and job.description != SENTINEL
    )

    settings = get_settings()
    if not settings.volc_api_key:
        if has_existing:
            logger.info(
                "VOLC_API_KEY not configured — keeping existing content for job=%s",
                job.job_id,
            )
            return
        raise FatalError(
            f"No VOLC_API_KEY configured and job {job.job_id} has no existing content"
        )

    from app.evomap.content_dedup import ContentDeduper
    from app.evomap.prompt_evolve import PromptEvolver
    from app.models.pin_performance import PinPerformance

    context = PromptContext(
        product_type=job.product_type,
        niche=job.niche,
        audience=job.audience,
        season=job.season,
        offer=job.offer,
        destination_url=job.destination_url,
    )

    evolver = PromptEvolver(db=db)

    dedup = ContentDeduper()
    cutoff = datetime.now(UTC) - timedelta(days=30)

    # try up to 2 rounds (initial + 1 retry with different angle hint)
    for attempt in range(2):
        try:
            content = await evolver.agenerate_single_content(context)
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    "LLM content generation failed for job=%s, falling back to existing: %s",
                    job.job_id, exc,
                )
                if has_existing:
                    return
                raise FatalError(
                    f"LLM content generation failed and job {job.job_id} has no existing content"
                ) from exc
            continue

        title = content.get("title", "")
        description = content.get("description", "")
        if not title or not description:
            if attempt == 1:
                logger.warning(
                    "LLM returned empty content for job=%s, falling back to existing",
                    job.job_id,
                )
                if has_existing:
                    return
                raise FatalError(
                    f"LLM returned empty content and job {job.job_id} has no existing content"
                )
            continue

        # dedup against 30-day PinPerformance history
        history_rows = list(
            db.scalars(
                select(PinPerformance)
                .where(
                    PinPerformance.account_id == account_id,
                    PinPerformance.niche == context.niche,
                    PinPerformance.product_type == context.product_type,
                    PinPerformance.published_at >= cutoff,
                )
                .order_by(PinPerformance.published_at.desc())
                .limit(60)
            ).all()
        )
        history = [
            {"title": r.title, "description": r.description} for r in history_rows
        ]

        hist_rejected, hist_reason = dedup.check_against_history(
            title=title, description=description, history=history
        )
        if hist_rejected:
            logger.info(
                "Content dedup failed job=%s attempt=%d reason=%s — retrying",
                job.job_id, attempt, hist_reason,
            )
            continue

        # accepted — write back
        board = _clean_generated_board(content.get("board"), context)
        topics = _clean_generated_topics(content.get("tagged_topics"), context)

        job.title = title[:100]
        job.description = description
        job.title_hash = dedup.stable_hash(title)
        job.description_hash = dedup.stable_hash(description)
        job.content_hash = dedup.stable_hash(f"{title}|{description}")
        job.board_name = board
        job.tagged_topics = json.dumps(topics, ensure_ascii=False)
        job.variant_angle = content.get("angle") or job.variant_angle
        db.commit()
        logger.info(
            "Content regenerated job=%s title=%.60s... board=%s hash=%s",
            job.job_id, title, board, job.content_hash,
        )
        return

    logger.warning(
        "Content dedup failed after all retries for job=%s, keeping existing",
        job.job_id,
    )
    if not has_existing:
        raise FatalError(
            f"Content dedup exhausted and job {job.job_id} has no existing content"
        )


def _clean_generated_board(value: Any, context: PromptContext) -> str:
    board = str(value or "").strip()
    if not board:
        board = context.niche or context.product_type or "Pinterest Finds"
    board = " ".join(board.replace("_", " ").split())
    words = board.split()
    if len(words) > 4:
        board = " ".join(words[:4])
    return board.title()[:160]


def _clean_generated_topics(value: Any, context: PromptContext) -> list[str]:
    if isinstance(value, list):
        raw_topics = [str(item).strip() for item in value if str(item).strip()]
    else:
        raw_topics = []
    if not raw_topics:
        raw_topics = [
            context.niche,
            context.product_type,
            context.season,
            "Gift Ideas",
            "Pinterest Finds",
        ]
    cleaned: list[str] = []
    for topic in raw_topics:
        topic = " ".join(str(topic).replace("_", " ").split()).title()
        if topic and topic not in cleaned:
            cleaned.append(topic[:80])
        if len(cleaned) >= 5:
            break
    return cleaned


async def _generate_fresh_image(
    job: PublishJob,
    db: Any,
) -> None:
    """Generate a fresh image via EvoMap → Flux 2 Pro → ESRGAN; falls back to existing.

    When LLM or image service is unavailable, keeps the job's existing image.
    Only raises when the job has no image at all.
    """
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    has_fal = bool(settings.fal_key)
    has_llm = bool(settings.volc_api_key)

    if not has_fal:
        if job.image_path and Path(job.image_path).exists():
            logger.info(
                "FAL_KEY not configured — keeping existing image for job=%s",
                job.job_id,
            )
            return
        raise FatalError(
            f"FAL_KEY not configured and job {job.job_id} has no existing image"
        )

    from app.evomap.prompt_evolve import PromptEvolver
    from app.workflows.image_generation_flow import DEFAULT_PIN_IMAGE_SIZE, generate_image_asset

    context = PromptContext(
        product_type=job.product_type,
        niche=job.niche,
        audience=job.audience,
        season=job.season,
        offer=job.offer,
        destination_url=job.destination_url,
    )

    visual_prompt = None
    if has_llm:
        evolver = PromptEvolver(db=db)
        try:
            visual_prompt = await evolver.agenerate_visual_brief(context)
        except Exception as exc:
            logger.warning(
                "Visual prompt generation failed for job=%s: %s", job.job_id, exc,
            )

    if not visual_prompt or len(visual_prompt.strip()) < 10:
        if job.image_path and Path(job.image_path).exists():
            logger.info(
                "No visual prompt generated — keeping existing image for job=%s",
                job.job_id,
            )
            return
        raise FatalError(
            f"No visual prompt for job {job.job_id} and no existing image"
        )

    logger.info(
        "Auto-generating image for job=%s prompt=%.80s...",
        job.job_id,
        visual_prompt,
    )

    try:
        asset = await generate_image_asset(
            prompt=visual_prompt,
            image_size=DEFAULT_PIN_IMAGE_SIZE,
        )
    except Exception as exc:
        if job.image_path and Path(job.image_path).exists():
            logger.warning(
                "Image generation failed for job=%s, keeping existing: %s",
                job.job_id, exc,
            )
            return
        raise FatalError(
            f"Image generation failed for job {job.job_id} and no existing image: {exc}"
        ) from exc

    job.image_path = asset.local_path
    job.error_message = None
    db.commit()

    logger.info(
        "Image regenerated job=%s path=%s bytes=%d",
        job.job_id,
        asset.local_path,
        asset.bytes_written,
    )


# ---------------------------------------------------------------------------
# helper: lock + publish with heartbeat stages
# ---------------------------------------------------------------------------


def _acquire_locks_and_publish(
    job_id: str,
    account_id: str,
    profile_id: str,
    batch_id: str,
    job: PublishJob,
    scheduled_task_id: str | None = None,
) -> dict[str, Any]:
    """Acquire both account and profile Redis locks, then run the publish flow."""
    from app.safety.locks import account_lock, profile_lock

    async def _run() -> dict[str, Any]:
        stages: list[dict[str, Any]] = []
        # heartbeat before entering browser automation so reclaim doesn't
        # reset a live task that's just waiting on Pinterest network
        _update_heartbeat(scheduled_task_id, account_id, profile_id)

        async with account_lock(account_id) as acct_held:
            if not acct_held:
                raise RetryableTaskError(f"Account lock held by another worker: {account_id}")
            async with profile_lock(profile_id) as prof_held:
                if not prof_held:
                    raise RetryableTaskError(f"Profile lock held by another worker: {profile_id}")

                with get_sessionmaker()() as lock_db:
                    stages.append(_stage("open_profile", "running"))
                    try:
                        result = await run_pin_publish_with_adspower(
                            db=lock_db,
                            workflow_input=AccountPublishWorkflowInput(
                                account_id=job.account_id,
                                campaign_id=job.campaign_id,
                                board_name=job.board_name,
                                image_path=Path(job.image_path),
                                title=job.title,
                                description=job.description,
                                destination_url=job.destination_url,
                                prompt_context=PromptContext(
                                    product_type=job.product_type,
                                    niche=job.niche,
                                    audience=job.audience,
                                    season=job.season,
                                    offer=job.offer,
                                    destination_url=job.destination_url,
                                ),
                            ),
                            content_batch_id=batch_id,
                            variant_angle=job.variant_angle,
                            content_hash=job.content_hash,
                            title_hash=job.title_hash,
                            description_hash=job.description_hash,
                        )
                        stages.append(
                            _stage("publish", "completed", extra={"pin_url": result.pin_url})
                        )
                        return {
                            "job_id": job_id,
                            "status": "published" if result.success else "failed",
                            "pin_url": result.pin_url,
                            "publish_evidence": result.publish_evidence,
                            "pin_performance_id": result.pin_performance_id,
                            "debug_artifact_dir": result.debug_artifact_dir,
                            "stages": stages,
                        }
                    except Exception:
                        stages.append(_stage("publish", "failed"))
                        raise

    return run_async(_run())


def _stage(name: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"stage": name, "status": status, "ts": datetime.now(UTC).isoformat(), **extra}


class RetryableTaskError(Exception):
    """Transient error that Celery should retry."""


def _handle_task_exception(
    exc: Exception,
    st_id: str | None,
) -> None:
    """Route exception to the correct writeback: fatal → failed, retryable → retry."""
    if isinstance(exc, RetryableTaskError):
        _check_final_retry_and_writeback(st_id, error_message=str(exc), error_type="retryable")
        raise
    if isinstance(exc, FatalError):
        _st_writeback(st_id, "failed", error_message=str(exc), error_type="fatal")
        raise
    error_type = classify_exception(exc)
    if error_type == "fatal":
        _st_writeback(st_id, "failed", error_message=str(exc), error_type="fatal")
        raise FatalError(str(exc)) from exc
    _check_final_retry_and_writeback(st_id, error_message=str(exc), error_type=error_type)
    raise RetryableTaskError(str(exc)) from exc


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.jobs.generate_image_asset",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=60,
)
def generate_image_asset_task(
    prompt: str, image_size: str = "{\"width\": 800, \"height\": 1200}", **kwargs: Any
) -> dict[str, Any]:
    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    try:
        asset = run_async(generate_image_asset(prompt=prompt, image_size=image_size))
        result = asdict(asset)
        _st_writeback(st_id, "completed", result_json=result)
        return result
    except Exception as exc:
        _handle_task_exception(exc, st_id)


@celery_app.task(
    name="app.jobs.generate_image_for_publish_job",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=60,
)
def generate_image_for_publish_job_task(
    job_id: str,
    prompt: str,
    image_size: str = '{"width": 800, "height": 1200}',
    **kwargs: Any,
) -> dict[str, Any]:
    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    try:
        asset = run_async(generate_image_asset(prompt=prompt, image_size=image_size))
        with get_sessionmaker()() as db:
            job = _get_publish_job(db, job_id)
            job.image_path = asset.local_path
            job.status = "pending"
            job.error_message = None
            db.commit()
        result = asdict(asset) | {"job_id": job_id}
        _st_writeback(st_id, "completed", result_json=result)
        return result
    except Exception as exc:
        _handle_task_exception(exc, st_id)


@celery_app.task(
    name="app.jobs.publish_job",
    autoretry_for=(RetryableTaskError,),
    max_retries=1,
    default_retry_delay=120,
)
def publish_job_task(
    job_id: str, dry_run: bool = False, content_batch_id: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    with get_sessionmaker()() as db:
        job = _get_publish_job(db, job_id)
        if job.status == "cancelled":
            _st_writeback(
                st_id, "failed",
                error_message=f"Publish job is cancelled: {job_id}",
                error_type="fatal",
            )
            raise FatalError(f"Publish job is cancelled: {job_id}")

        # auto-regenerate content + image before publish (falls back to existing)
        try:
            run_async(_regenerate_content(job, job.account_id, db))
            run_async(_generate_fresh_image(job, db))
        except FatalError:
            raise
        except Exception as exc:
            logger.warning(
                "Content/image regeneration warning for job=%s: %s — proceeding",
                job.job_id, exc,
            )

        batch_id = content_batch_id or job.content_batch_id or f"batch_{job_id[:12]}"
        job.content_batch_id = batch_id
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.finished_at = None
        job.error_message = None
        db.commit()

        if dry_run:
            job.status = "dry_run_done"
            db.commit()
            result = {"job_id": job_id, "status": job.status, "dry_run": True}
            _st_writeback(st_id, "completed", result_json=result)
            return result

        # resolve AdsPower profile id
        from app.models.social_account import SocialAccount

        account = db.scalar(
            select(SocialAccount).where(SocialAccount.account_id == job.account_id)
        )
        if account is None or not account.adspower_profile_id:
            job.status = "failed"
            job.error_message = f"No AdsPower profile bound for account {job.account_id}"
            job.finished_at = datetime.now(UTC)
            db.commit()
            _st_writeback(
                st_id, "failed",
                error_message=job.error_message,
                error_type="fatal",
            )
            raise FatalError(f"No AdsPower profile bound for account {job.account_id}")

        profile_id = account.adspower_profile_id

    # acquire locks and publish (outside the DB session to avoid holding it)
    try:
        result = _acquire_locks_and_publish(
            job_id=job_id,
            account_id=job.account_id,
            profile_id=profile_id,
            batch_id=batch_id,
            job=job,
            scheduled_task_id=st_id,
        )
        with get_sessionmaker()() as db:
            job = _get_publish_job(db, job_id)
            job.status = result["status"]
            job.pin_performance_id = result.get("pin_performance_id")
            job.error_message = None if result["status"] == "published" else job.error_message
            job.finished_at = datetime.now(UTC)
            db.commit()
        _st_writeback(st_id, job.status, result_json=result)
        return result
    except Exception as exc:
        error_type = classify_exception(exc)
        with get_sessionmaker()() as db:
            job = _get_publish_job(db, job_id)
            job.status = "failed"
            job.error_message = f"[{error_type}] {exc}"
            job.finished_at = datetime.now(UTC)
            db.commit()
        if error_type == "fatal":
            _st_writeback(
                st_id, "failed",
                error_message=str(exc),
                error_type="fatal",
            )
            raise FatalError(str(exc)) from exc
        # mark scheduled_task as failed; Celery may retry once (max_retries=1)
        _st_writeback(
            st_id, "failed",
            error_message=str(exc),
            error_type=error_type,
        )
        raise RetryableTaskError(str(exc)) from exc


@celery_app.task(name="app.jobs.dispatch_publish_jobs")
def dispatch_publish_jobs_task(limit: int = 20, dry_run: bool = False) -> dict[str, Any]:
    """Scan scheduled_task (+ legacy publish_job) and enqueue Celery tasks.

    dry_run is on by default to prevent accidental real-platform publishing
    during local development; pass False in production.
    """
    from app.jobs.dispatcher import dispatch_ready_tasks

    with get_sessionmaker()() as db:
        result = dispatch_ready_tasks(db, limit=limit, dry_run=dry_run)
    logger.info(
        "Dispatched %d tasks batch=%s dry_run=%s skipped=%s",
        result["dispatched"],
        result["batch_id"],
        dry_run,
        result["skipped"],
    )
    return result


@celery_app.task(
    name="app.jobs.refresh_current_event_trends",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=120,
)
def refresh_current_event_trends_task(
    scope: str, query: str | None = None, limit: int = 20, **kwargs: Any
) -> dict[str, Any]:
    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    try:
        with get_sessionmaker()() as db:
            result = run_async(refresh_current_event_trends(db, scope=scope, query=query, limit=limit))
        _st_writeback(st_id, "completed", result_json=result)
        return result
    except Exception as exc:
        _handle_task_exception(exc, st_id)


@celery_app.task(
    name="app.jobs.refresh_product_trends",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=120,
)
def refresh_product_trends_task(
    scope: str,
    niche: str | None = None,
    product_type: str | None = None,
    limit: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    try:
        with get_sessionmaker()() as db:
            result = run_async(
                refresh_product_trends(
                    db,
                    scope=scope,
                    niche=niche,
                    product_type=product_type,
                    limit=limit,
                )
            )
        _st_writeback(st_id, "completed", result_json=result)
        return result
    except Exception as exc:
        _handle_task_exception(exc, st_id)


@celery_app.task(
    name="app.jobs.generate_marketing_video",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=120,
)
def generate_marketing_video_task(
    prompt: str,
    image_url: str | None = None,
    duration_seconds: int = 5,
    aspect_ratio: str = "9:16",
    **kwargs: Any,
) -> dict[str, Any]:
    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    try:
        video = run_async(
            generate_marketing_video(
                VideoGenerationInput(
                    prompt=prompt,
                    image_url=image_url,
                    duration_seconds=duration_seconds,
                    aspect_ratio=aspect_ratio,
                )
            )
        )
        result = asdict(video)
        _st_writeback(st_id, "completed", result_json=result)
        return result
    except Exception as exc:
        _handle_task_exception(exc, st_id)


@celery_app.task(
    name="app.jobs.auto_reply",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=120,
)
def auto_reply_task(
    account_id: str,
    dry_run: bool = True,
    limit: int = 20,
    brand_voice: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    from app.models.social_account import SocialAccount
    from app.safety.locks import account_lock as _acct_lock
    from app.safety.locks import profile_lock

    async def _locked_reply() -> dict[str, Any]:
        _update_heartbeat(st_id, account_id)

        with get_sessionmaker()() as db:
            account = db.scalar(
                select(SocialAccount).where(SocialAccount.account_id == account_id)
            )
            if account is None or not account.adspower_profile_id:
                raise FatalError(f"No AdsPower profile bound: {account_id}")
            profile_id = account.adspower_profile_id

        from app.automation.browser_factory import open_adspower_profile
        from app.safety.proxy_check import verify_us_ip

        async with _acct_lock(account_id) as acct_held:
            if not acct_held:
                raise RetryableTaskError(f"Account lock held: {account_id}")
            async with profile_lock(profile_id) as prof_held:
                if not prof_held:
                    raise RetryableTaskError(f"Profile lock held: {profile_id}")

                _update_heartbeat(st_id, account_id, profile_id)
                session = await open_adspower_profile(profile_id)
                try:
                    await verify_us_ip(session.page)
                    result = await run_auto_reply_flow(
                        account_id=account_id,
                        page=session.page,
                        dry_run=dry_run,
                        limit=limit,
                        brand_voice=brand_voice,
                    )
                finally:
                    await session.close()

        return {
            "account_id": result.account_id,
            "dry_run": result.dry_run,
            "suggestions": [asdict(item) for item in result.suggestions],
            "posted": [asdict(item) for item in result.posted],
        }

    try:
        result = run_async(_locked_reply())
        _st_writeback(st_id, "completed", result_json=result)
        return result
    except Exception as exc:
        _handle_task_exception(exc, st_id)


@celery_app.task(name="app.jobs.cleanup_assets")
def cleanup_assets_task(retention_days: int = 7, **kwargs: Any) -> dict[str, Any]:
    """Delete generated image/video files older than *retention_days*."""
    import time

    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    settings = get_settings()
    upload_dir = Path(settings.upload_dir)

    # Safety: only operate inside well-known upload directories to prevent
    # misconfigured UPLOAD_DIR from deleting unrelated files.
    allowed_keywords = ("upload", "var", "generated", "assets")
    if not any(kw in str(upload_dir).lower() for kw in allowed_keywords):
        logger.error(
            "Cleanup aborted — upload_dir=%s does not look like an upload directory",
            upload_dir,
        )
        msg = {"deleted": 0, "message": "Upload directory does not pass safety check"}
        _st_writeback(st_id, "completed", result_json=msg)
        return msg

    if not upload_dir.exists():
        msg = {"deleted": 0, "message": "Upload directory does not exist"}
        _st_writeback(st_id, "completed", result_json=msg)
        return msg

    cutoff = time.time() - retention_days * 86400
    deleted_count = 0
    deleted_bytes = 0

    for entry in upload_dir.iterdir():
        if entry.is_file() and entry.name.startswith("generated_"):
            try:
                stat = entry.stat()
                if stat.st_mtime < cutoff:
                    size = stat.st_size
                    entry.unlink()
                    deleted_count += 1
                    deleted_bytes += size
            except OSError:
                pass

    logger.info(
        "Cleaned up %d assets (%d bytes) from %s",
        deleted_count,
        deleted_bytes,
        upload_dir,
    )
    result = {
        "deleted": deleted_count,
        "deleted_bytes": deleted_bytes,
        "upload_dir": str(upload_dir),
        "retention_days": retention_days,
    }
    _st_writeback(st_id, "completed", result_json=result)
    return result


@celery_app.task(
    name="app.jobs.warmup",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=120,
)
def warmup_task(
    account_id: str = "",
    duration_minutes: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a single warmup browsing session via AdsPower for *account_id*.

    Acquires account + profile Redis locks to prevent concurrent browser
    sessions on the same account / AdsPower profile.
    """
    from app.models.social_account import SocialAccount
    from app.safety.locks import account_lock, profile_lock

    st_id: str | None = kwargs.pop("scheduled_task_id", None)

    if not account_id:
        _st_writeback(st_id, "failed", error_message="Missing account_id", error_type="fatal")
        raise FatalError("warmup_task requires account_id")

    async def _run() -> dict[str, Any]:
        _update_heartbeat(st_id, account_id)

        # resolve AdsPower profile
        with get_sessionmaker()() as db:
            account = db.scalar(
                select(SocialAccount).where(SocialAccount.account_id == account_id)
            )
            if account is None or not account.adspower_profile_id:
                raise FatalError(
                    f"No AdsPower profile bound for account {account_id}"
                )
            profile_id = account.adspower_profile_id

        from app.automation.browser_factory import open_adspower_profile

        async with account_lock(account_id) as acct_held:
            if not acct_held:
                raise RetryableTaskError(
                    f"Account lock held by another worker: {account_id}"
                )
            async with profile_lock(profile_id) as prof_held:
                if not prof_held:
                    raise RetryableTaskError(
                        f"Profile lock held by another worker: {profile_id}"
                    )

                _update_heartbeat(st_id, account_id, profile_id)
                session = await open_adspower_profile(profile_id)
                try:
                    result = await run_warmup_session(
                        session.page,
                        account_id=account_id,
                        duration_minutes=duration_minutes,
                    )
                finally:
                    await session.close()

        return {
            "account_id": result.account_id,
            "duration_seconds": result.duration_seconds,
            "actions": result.actions,
            "searches": result.searches,
            "interactions": result.interactions,
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat(),
        }

    try:
        result = run_async(_run())
        _st_writeback(st_id, "completed", result_json=result)
        return result
    except Exception as exc:
        _handle_task_exception(exc, st_id)


@celery_app.task(
    name="app.jobs.warmup_and_publish",
    autoretry_for=(RetryableTaskError,),
    max_retries=2,
    default_retry_delay=300,
)
def warmup_and_publish_task(
    account_id: str,
    job_id: str,
    warmup_duration_minutes: int | None = None,
    content_batch_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run warmup then publish in a single AdsPower browser session.

    Acquires account + profile Redis locks, opens AdsPower once, runs
    warmup browsing, then immediately publishes the Pin on the same page.
    """
    from app.models.social_account import SocialAccount
    from app.safety.locks import account_lock, profile_lock

    st_id: str | None = kwargs.pop("scheduled_task_id", None)
    dry_run: bool = kwargs.pop("dry_run", False)

    async def _run() -> dict[str, Any]:
        if dry_run:
            _update_heartbeat(st_id, account_id)
            result = {
                "account_id": account_id,
                "job_id": job_id,
                "status": "dry_run_skipped",
                "note": "dry_run=True — browser session skipped",
            }
            _st_writeback(st_id, "completed", result_json=result)
            return result

        _update_heartbeat(st_id, account_id)

        with get_sessionmaker()() as db:
            account = db.scalar(
                select(SocialAccount).where(SocialAccount.account_id == account_id)
            )
            if account is None or not account.adspower_profile_id:
                raise FatalError(
                    f"No AdsPower profile bound for account {account_id}"
                )
            profile_id = account.adspower_profile_id

            job = db.scalar(select(PublishJob).where(PublishJob.job_id == job_id))
            if job is None:
                raise FatalError(f"Publish job not found: {job_id}")
            if job.status == "cancelled":
                raise FatalError(f"Publish job cancelled: {job_id}")
            # Regenerate content + image fresh every publish (falls back to existing)
            try:
                await _regenerate_content(job, account_id, db)
                await _generate_fresh_image(job, db)
            except FatalError:
                raise
            except Exception as exc:
                logger.warning(
                    "Content/image regeneration warning for job=%s: %s — proceeding",
                    job_id, exc,
                )

            job.status = "running"
            job.started_at = datetime.now(UTC)
            job.finished_at = None
            job.error_message = None
            db.commit()

        from app.workflows.warmup_publish_flow import run_warmup_then_publish

        async with account_lock(account_id) as acct_held:
            if not acct_held:
                raise RetryableTaskError(
                    f"Account lock held by another worker: {account_id}"
                )
            async with profile_lock(profile_id) as prof_held:
                if not prof_held:
                    raise RetryableTaskError(
                        f"Profile lock held by another worker: {profile_id}"
                    )

                _update_heartbeat(st_id, account_id, profile_id)
                result = await run_warmup_then_publish(
                    account_id=account_id,
                    job_id=job_id,
                    warmup_duration_minutes=warmup_duration_minutes,
                    content_batch_id=content_batch_id,
                )

        publish_status = "published" if (
            result.publish and result.publish.success
        ) else "failed"
        with get_sessionmaker()() as db:
            job = db.scalar(select(PublishJob).where(PublishJob.job_id == job_id))
            if job:
                job.status = publish_status
                job.pin_performance_id = result.pin_performance_id
                job.finished_at = datetime.now(UTC)
                job.error_message = None if publish_status == "published" else "Publish workflow returned failed"
                db.commit()

        publish_evidence = result.publish.publish_evidence if result.publish else None
        return {
            "account_id": account_id,
            "job_id": job_id,
            "status": publish_status,
            "pin_url": result.publish.pin_url if result.publish else None,
            "publish_evidence": publish_evidence,
            "pin_performance_id": result.pin_performance_id,
            "warmup_seconds": result.warmup.duration_seconds if result.warmup else 0,
        }

    try:
        result = run_async(_run())
        final_status = "completed" if result.get("status") in {"published", "dry_run_skipped"} else "failed"
        _st_writeback(
            st_id,
            final_status,
            result_json=result,
            error_message=None if final_status == "completed" else result.get("status", "Publish workflow failed"),
            error_type=None if final_status == "completed" else "publish_failed",
        )
        return result
    except RetryableTaskError as exc:
        _mark_publish_job_failed(job_id, str(exc))
        _check_final_retry_and_writeback(st_id, error_message=str(exc), error_type="retryable")
        raise
    except FatalError as exc:
        _mark_publish_job_failed(job_id, str(exc))
        _st_writeback(
            st_id, "failed", error_message=str(exc), error_type="fatal"
        )
        raise
    except Exception as exc:
        error_type = classify_exception(exc)
        if error_type == "fatal":
            _mark_publish_job_failed(job_id, str(exc))
            _st_writeback(
                st_id, "failed",
                error_message=str(exc),
                error_type="fatal",
            )
            raise FatalError(str(exc)) from exc
        _mark_publish_job_failed(job_id, str(exc))
        _check_final_retry_and_writeback(
            st_id,
            error_message=str(exc),
            error_type=error_type,
        )
        raise RetryableTaskError(str(exc)) from exc


@celery_app.task(name="app.jobs.reclaim_stale_tasks")
def reclaim_stale_tasks_task(
    stale_minutes: int = 45, **kwargs: Any
) -> dict[str, Any]:
    """Reset tasks stuck in 'running' (no heartbeat) or 'ready' (expired lock).

    Browser automation tasks that crash or get killed may leave scheduled_task
    rows in 'running' forever.  The dispatcher may also crash between claiming
    a task (setting status='ready' + lock_until) and enqueuing it, leaving it
    stuck in 'ready' with an expired lock.
    """
    from app.models.scheduled_task import ScheduledTask

    with get_sessionmaker()() as db:
        now = datetime.now(UTC)
        heartbeat_cutoff = now - timedelta(minutes=stale_minutes)
        lock_cutoff = now - timedelta(minutes=max(stale_minutes, 30))

        # 1) "running" tasks whose heartbeat has timed out
        stale_running = list(
            db.scalars(
                select(ScheduledTask)
                .where(
                    ScheduledTask.status == "running",
                    ScheduledTask.heartbeat_at.isnot(None),
                    ScheduledTask.heartbeat_at < heartbeat_cutoff,
                )
            ).all()
        )

        # 2) "running" tasks that never set a heartbeat (e.g. TypeError before
        #    the function body executed — Celery failed, DB was never updated)
        never_heartbeated = list(
            db.scalars(
                select(ScheduledTask)
                .where(
                    ScheduledTask.status == "running",
                    ScheduledTask.heartbeat_at == None,  # noqa: E711
                    ScheduledTask.started_at.isnot(None),
                    ScheduledTask.started_at < heartbeat_cutoff,
                )
            ).all()
        )

        # 3) "ready" tasks whose lock has expired (dispatcher crashed mid-claim)
        stuck_ready = list(
            db.scalars(
                select(ScheduledTask)
                .where(
                    ScheduledTask.status == "ready",
                    ScheduledTask.lock_until.isnot(None),
                    ScheduledTask.lock_until < lock_cutoff,
                )
            ).all()
        )

        reclaimed = 0
        for st in stale_running + never_heartbeated + stuck_ready:
            old_status = st.status
            st.status = "pending"
            st.locked_by = None
            st.lock_until = None
            st.celery_task_id = None
            st.attempt_count = 0
            st.next_retry_at = now + timedelta(minutes=5)
            st.error_message = (
                f"Stale task reclaimed after {stale_minutes}min (was {old_status})"
            )
            reclaimed += 1
            logger.warning(
                "Reclaimed stuck task_id=%s type=%s account=%s (was status=%s)",
                st.task_id,
                st.task_type,
                st.account_id,
                old_status,
            )

            # Also unstick publish_job if this task owned one
            job_id = (st.payload_json or {}).get("job_id")
            if job_id and st.task_type in ("publish", "warmup_and_publish"):
                from app.models.publish_job import PublishJob

                job = db.scalar(
                    select(PublishJob).where(PublishJob.job_id == job_id)
                )
                if job and job.status == "running":
                    job.status = "pending"
                    job.error_message = (
                        f"Reset after owning task {st.task_id} was reclaimed"
                    )
                    logger.warning(
                        "Reset stuck publish_job %s (was running) after reclaim",
                        job_id,
                    )

        db.commit()

    return {"reclaimed": reclaimed, "stale_minutes": stale_minutes}


def _get_publish_job(db: Any, job_id: str) -> PublishJob:
    job = db.scalar(select(PublishJob).where(PublishJob.job_id == job_id))
    if job is None:
        raise FatalError(f"Publish job not found: {job_id}")
    return job
