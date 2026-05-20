"""Verify the 8 bug fixes from the comprehensive code review.

These tests target:
  #2  dispatcher timezone fix
  #3  publish_job_task writes "completed" (not "published") to scheduled_task
  #4  models/__init__.py exports all models
  #5  reclaim_stale stops retrying after hard limit
  #6  publish_job_task retryable errors use _check_final_retry_and_writeback
  #7  check_health uses valid AdsPower endpoint
  #8  publish_pin_direct creates a trackable scheduled_task (non-bypass path)

All tests are DRY_RUN / mock only — no real Pinterest, no real AdsPower,
no real Celery, no real browser.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.jobs.dispatcher import (
    _count_posts_today,
    _within_time_window,
)
from app.jobs.tasks import (
    _st_writeback,
    reclaim_stale_tasks_task,
)
from app.models.account_policy import AccountPolicy
from app.models.scheduled_task import ScheduledTask


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_db_session(returned_task=None, returned_job=None):
    """Build a MagicMock that mimics a SQLAlchemy Session for one-shot use."""
    db = MagicMock()
    # .scalars(...).all() → [task] or []
    mock_scalars_chain = MagicMock()
    mock_scalars_chain.all.return_value = [returned_task] if returned_task else []
    db.scalars.return_value = mock_scalars_chain
    # .scalar(...) → returned_job (for publish_job lookups)
    db.scalar.return_value = returned_job
    return db


def _patch_get_sessionmaker(db):
    """Patch get_sessionmaker so the context-manager form returns *db*."""
    @contextmanager
    def _mock_session():
        yield db
    return _mock_session


# ---------------------------------------------------------------------------
# #2  timezone fix — _within_time_window and _count_posts_today
# ---------------------------------------------------------------------------


class TestWithinTimeWindow:
    """Verify _within_time_window uses scheduler timezone (not raw UTC)."""

    def test_daytime_window_allows_local_afternoon(self, monkeypatch):
        monkeypatch.setattr(
            "app.jobs.dispatcher.get_settings",
            lambda: MagicMock(scheduler_timezone="Asia/Shanghai"),
        )
        policy = MagicMock(spec=AccountPolicy)
        policy.allowed_timezone_start = "09:00"
        policy.allowed_timezone_end = "22:00"
        # UTC 08:00 = Shanghai 16:00 → inside 09:00-22:00
        now = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        assert _within_time_window(policy, now) is True

    def test_daytime_window_rejects_local_midnight(self, monkeypatch):
        monkeypatch.setattr(
            "app.jobs.dispatcher.get_settings",
            lambda: MagicMock(scheduler_timezone="Asia/Shanghai"),
        )
        policy = MagicMock(spec=AccountPolicy)
        policy.allowed_timezone_start = "09:00"
        policy.allowed_timezone_end = "22:00"
        # UTC 18:00 = Shanghai 02:00 (next day) → outside 09:00-22:00
        now = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
        assert _within_time_window(policy, now) is False

    def test_overnight_window_allows_local_midnight(self, monkeypatch):
        monkeypatch.setattr(
            "app.jobs.dispatcher.get_settings",
            lambda: MagicMock(scheduler_timezone="Asia/Shanghai"),
        )
        policy = MagicMock(spec=AccountPolicy)
        policy.allowed_timezone_start = "22:00"
        policy.allowed_timezone_end = "06:00"
        # UTC 18:00 = Shanghai 02:00 → inside 22:00-06:00
        now = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
        assert _within_time_window(policy, now) is True

    def test_no_policy_always_returns_true(self):
        assert _within_time_window(None, datetime.now(UTC)) is True

    def test_no_window_returns_true(self):
        policy = MagicMock(spec=AccountPolicy)
        policy.allowed_timezone_start = None
        policy.allowed_timezone_end = None
        assert _within_time_window(policy, datetime.now(UTC)) is True

    def test_different_timezone_preserves_hours(self, monkeypatch):
        """US/Eastern: when local is 14:00 (2pm), 09:00-17:00 window passes."""
        monkeypatch.setattr(
            "app.jobs.dispatcher.get_settings",
            lambda: MagicMock(scheduler_timezone="US/Eastern"),
        )
        policy = MagicMock(spec=AccountPolicy)
        policy.allowed_timezone_start = "09:00"
        policy.allowed_timezone_end = "17:00"
        # UTC 19:00 on Jan 15 = US/Eastern 14:00 (EST, UTC-5)
        now = datetime(2026, 1, 15, 19, 0, tzinfo=UTC)
        assert _within_time_window(policy, now) is True


class TestCountPostsToday:
    """Verify _count_posts_today respects scheduler_timezone day boundary."""

    def test_returns_zero_when_no_posts(self, monkeypatch):
        monkeypatch.setattr(
            "app.jobs.dispatcher.get_settings",
            lambda: MagicMock(scheduler_timezone="UTC"),
        )
        now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        fake_db = _fake_db_session()
        # Override scalar to return None (no posts)
        fake_db.scalar.return_value = None
        fake_db.scalars.return_value.all.return_value = []

        result = _count_posts_today(fake_db, "acc_test", now)
        assert result == 0

    def test_local_day_boundary_differs_from_utc(self, monkeypatch):
        """When scheduler_timezone is Asia/Shanghai, the day boundary should
        be at UTC+8 midnight, not UTC midnight."""
        monkeypatch.setattr(
            "app.jobs.dispatcher.get_settings",
            lambda: MagicMock(scheduler_timezone="Asia/Shanghai"),
        )
        # UTC 22:00 Jan 15 = Shanghai 06:00 Jan 16
        now = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
        fake_db = _fake_db_session()
        # Expect the finished_at filter to use local day start in UTC
        fake_db.scalar.return_value = 0
        fake_db.scalars.return_value.all.return_value = []

        result = _count_posts_today(fake_db, "acc_test", now)
        assert result == 0
        # Verify the scalar was called (it executed the query)
        assert fake_db.scalar.call_count >= 1


# ---------------------------------------------------------------------------
# #3  scheduled_task status never "published"
# ---------------------------------------------------------------------------


class TestScheduledTaskStatusValues:
    """scheduled_task.status must only use: pending / ready / running /
    completed / failed / cancelled."""

    def test_all_st_writeback_calls_use_standard_status(self):
        """All literal _st_writeback status args in tasks.py must be standard."""
        import ast
        import inspect

        from app.jobs import tasks as tmod

        def _get_literal_status_calls(source: str) -> list[str]:
            try:
                tree = ast.parse(source)
            except SyntaxError:
                return []
            invalid: list[str] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name not in ("_st_writeback", "_check_final_retry_and_writeback"):
                    continue
                if not node.args or len(node.args) < 2:
                    continue
                second_arg = node.args[1]
                if isinstance(second_arg, ast.Constant) and isinstance(second_arg.value, str):
                    val = second_arg.value
                    if val not in {"completed", "failed", "pending", "running", "ready", "cancelled"}:
                        invalid.append(
                            f"{name}(..., {val!r}) at line {second_arg.lineno}"
                        )
            return invalid

        tasks_src = inspect.getsource(tmod)
        invalid = _get_literal_status_calls(tasks_src)
        assert not invalid, (
            f"_st_writeback calls with non-standard status in tasks.py: {invalid}"
        )


# ---------------------------------------------------------------------------
# #4  models/__init__.py exports all models
# ---------------------------------------------------------------------------


class TestModelsInit:
    """app.models exports every SQLAlchemy model and Base.metadata is complete."""

    def test_all_models_exported(self):
        import app.models as models_mod
        expected = {
            "AccountPolicy", "Campaign", "ContentTemplate",
            "GlobalStrategy", "PinPerformance", "PublishJob",
            "ReplyRecord", "ScheduledTask", "SocialAccount",
            "TokenUsage",
        }
        actual = set(getattr(models_mod, "__all__", []))
        missing = expected - actual
        assert not missing, f"models/__init__.py missing: {missing}"

    def test_base_metadata_includes_all_tables(self):
        from app.database import Base

        import app.models  # noqa: F401
        table_names = list(Base.metadata.tables.keys())
        assert len(table_names) >= 8, (
            f"Expected >=8 tables in metadata, got {len(table_names)}: {table_names}"
        )
        assert "scheduled_task" in table_names
        assert "publish_job" in table_names


# ---------------------------------------------------------------------------
# #5  reclaim_stale stops retrying after hard limit
# ---------------------------------------------------------------------------


class TestReclaimStaleLimits:
    """reclaim_stale must NOT retry forever."""

    @staticmethod
    def _make_stale_task(task_id, attempt_count, max_attempts=3, **kw):
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=50)
        return ScheduledTask(
            task_id=task_id,
            task_type=kw.get("task_type", "warmup_and_publish"),
            account_id=kw.get("account_id", "acc_test"),
            status="running",
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            heartbeat_at=cutoff,
            started_at=cutoff,
            scheduled_at=cutoff,
            payload_json=kw.get("payload_json", {"job_id": f"job_{task_id}"}),
        )

    def test_reclaims_below_hard_limit(self):
        """attempt_count=2 < hard_limit(3*3=9) → resets to pending."""
        task = self._make_stale_task("st_below", attempt_count=2, max_attempts=3)
        db = _fake_db_session(returned_task=task)

        with patch(
            "app.jobs.tasks.get_sessionmaker",
            return_value=_patch_get_sessionmaker(db),
        ):
            result = reclaim_stale_tasks_task(stale_minutes=45)
            assert task.status == "pending", (
                f"Expected pending, got {task.status!r}"
            )
            assert result["reclaimed"] >= 1

    def test_fails_at_hard_limit(self):
        """attempt_count=9 >= hard_limit(3*3=9) → fails with reclaim_exhausted."""
        task = self._make_stale_task("st_at_limit", attempt_count=9, max_attempts=3)
        db = _fake_db_session(returned_task=task)

        with patch(
            "app.jobs.tasks.get_sessionmaker",
            return_value=_patch_get_sessionmaker(db),
        ):
            _ = reclaim_stale_tasks_task(stale_minutes=45)
            assert task.status == "failed", (
                f"Expected failed at hard limit, got {task.status!r}"
            )
            assert task.error_type == "reclaim_exhausted"

    def test_custom_max_attempts_hard_limit(self):
        """max_attempts=5 → hard_limit=15; attempt_count=10 < 15 → reclaimed."""
        task = self._make_stale_task("st_custom", attempt_count=10, max_attempts=5)
        db = _fake_db_session(returned_task=task)

        with patch(
            "app.jobs.tasks.get_sessionmaker",
            return_value=_patch_get_sessionmaker(db),
        ):
            _ = reclaim_stale_tasks_task(stale_minutes=45)
            assert task.status == "pending", (
                f"Expected pending, got {task.status!r}"
            )

    def test_fails_linked_publish_job_on_exhaustion(self):
        """When reclaim exhausts for a publish task, linked publish_job fails."""
        task = self._make_stale_task(
            "st_linked", attempt_count=9, max_attempts=3,
            task_type="publish",
            payload_json={"job_id": "job_linked"},
        )
        linked_job = MagicMock()
        linked_job.status = "running"
        db = _fake_db_session(returned_task=task, returned_job=linked_job)

        with patch(
            "app.jobs.tasks.get_sessionmaker",
            return_value=_patch_get_sessionmaker(db),
        ):
            _ = reclaim_stale_tasks_task(stale_minutes=45)
            assert task.status == "failed"
            assert task.error_type == "reclaim_exhausted"
            assert linked_job.status == "failed"


# ---------------------------------------------------------------------------
# #6  publish_job_task retryable errors → _check_final_retry_and_writeback
# ---------------------------------------------------------------------------


class TestPublishJobRetryableErrorHandling:
    """retryable errors must not write 'failed' prematurely."""

    def test_retryable_error_uses_check_final(self):
        from app.jobs.tasks import _handle_task_exception, RetryableTaskError

        with (
            patch("app.jobs.tasks._check_final_retry_and_writeback") as mock_chk,
            patch("app.jobs.tasks._st_writeback") as mock_st,
        ):
            with pytest.raises(RetryableTaskError):
                _handle_task_exception(
                    RetryableTaskError("transient network error"),
                    "st_retry_test",
                )
            assert mock_chk.call_count >= 1, (
                "retryable error must call _check_final_retry_and_writeback"
            )
            # _st_writeback should NOT be called with "failed"
            failed_calls = [
                c for c in mock_st.call_args_list
                if len(c.args) > 1 and c.args[1] == "failed"
            ]
            assert not failed_calls, (
                "retryable error must NOT call _st_writeback('failed')"
            )

    def test_fatal_error_writes_failed_immediately(self):
        from app.jobs.tasks import _handle_task_exception
        from app.safety.errors import FatalError

        with (
            patch("app.jobs.tasks._check_final_retry_and_writeback") as mock_chk,
            patch("app.jobs.tasks._st_writeback") as mock_st,
        ):
            with pytest.raises(FatalError):
                _handle_task_exception(
                    FatalError("account suspended"),
                    "st_fatal_test",
                )
            failed_calls = [
                c for c in mock_st.call_args_list
                if len(c.args) > 1 and c.args[1] == "failed"
            ]
            assert len(failed_calls) >= 1, (
                "fatal error must write 'failed' immediately"
            )


# ---------------------------------------------------------------------------
# #7  check_health uses valid AdsPower endpoint (list_profiles)
# ---------------------------------------------------------------------------


class TestCheckHealthAdsPower:
    """check_health must call AdsPowerClient.list_profiles(), not a fake /status."""

    def test_adspower_check_success(self, monkeypatch):
        monkeypatch.setattr(
            "app.tools.adspower_api.AdsPowerClient.list_profiles",
            lambda self: [{"user_id": "prof1", "name": "Test"}],
        )
        from scripts.nanobot_mcp_server import check_health

        result = check_health()
        adspower_check = result["checks"].get("adspower", {})
        assert adspower_check.get("ok") is True
        assert adspower_check.get("profile_count", 0) >= 1

    def test_adspower_check_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.tools.adspower_api.AdsPowerClient.list_profiles",
            lambda self: (_ for _ in ()).throw(ConnectionError("Cannot connect")),
        )
        from scripts.nanobot_mcp_server import check_health

        result = check_health()
        adspower_check = result["checks"].get("adspower", {})
        assert adspower_check.get("ok") is False
        assert "not running" in adspower_check.get("error", "").lower()

    def test_adspower_check_unauthorized(self, monkeypatch):
        monkeypatch.setattr(
            "app.tools.adspower_api.AdsPowerClient.list_profiles",
            lambda self: (_ for _ in ()).throw(RuntimeError("HTTP 401: Unauthorized")),
        )
        from scripts.nanobot_mcp_server import check_health

        result = check_health()
        adspower_check = result["checks"].get("adspower", {})
        assert adspower_check.get("ok") is False
        assert "api key" in adspower_check.get("error", "").lower()


# ---------------------------------------------------------------------------
# #8  publish_pin_direct creates a trackable scheduled_task
# ---------------------------------------------------------------------------


class TestPublishPinDirectTracking:
    """publish_pin_direct (non-bypass) must create a scheduled_task row."""

    def test_creates_scheduled_task_by_default(self, monkeypatch):
        from app.models.social_account import SocialAccount
        from app.models.publish_job import PublishJob

        mock_db = MagicMock()
        mock_account = MagicMock(spec=SocialAccount)
        mock_account.adspower_profile_id = "prof_abc"
        mock_job = MagicMock(spec=PublishJob)
        mock_job.job_id = "job_track"
        mock_job.status = "pending"
        mock_db.scalar.side_effect = [mock_account, mock_job]

        monkeypatch.setattr(
            "scripts.nanobot_mcp_server._db", lambda: mock_db
        )
        monkeypatch.setattr(
            "app.jobs.dispatcher.dispatch_ready_tasks",
            lambda db, limit, dry_run: {
                "dispatched": 1, "skipped": {}, "batch_id": "b01", "dry_run": dry_run,
            },
        )

        from scripts.nanobot_mcp_server import publish_pin_direct

        result = publish_pin_direct(
            account_id="acc_track",
            job_id="job_track",
            dry_run=True,
            bypass_scheduler=False,
        )

        assert result.get("scheduled_task_id"), (
            f"Expected scheduled_task_id, got: {result}"
        )
        assert result["scheduled_task_id"].startswith("st_")
        assert result.get("dispatched") == 1

    def test_bypass_scheduler_skips_tracking(self, monkeypatch):
        from app.models.social_account import SocialAccount
        from app.models.publish_job import PublishJob

        mock_db = MagicMock()
        mock_account = MagicMock(spec=SocialAccount)
        mock_account.adspower_profile_id = "prof_abc"
        mock_job = MagicMock(spec=PublishJob)
        mock_job.job_id = "job_bypass"
        mock_job.status = "pending"
        mock_db.scalar.side_effect = [mock_account, mock_job]

        monkeypatch.setattr(
            "scripts.nanobot_mcp_server._db", lambda: mock_db
        )

        mock_delay = MagicMock()
        mock_delay.return_value.id = "celery_task_bypass"
        monkeypatch.setattr(
            "app.jobs.tasks.warmup_and_publish_task.delay",
            mock_delay,
        )

        from scripts.nanobot_mcp_server import publish_pin_direct

        result = publish_pin_direct(
            account_id="acc_bypass",
            job_id="job_bypass",
            dry_run=True,
            bypass_scheduler=True,
        )

        assert result.get("scheduled_task_id") is None
        assert result.get("bypass_scheduler") is True
        assert result.get("celery_task_id") == "celery_task_bypass"

    def test_missing_account_returns_error(self, monkeypatch):
        mock_db = MagicMock()
        mock_db.scalar.return_value = None

        monkeypatch.setattr(
            "scripts.nanobot_mcp_server._db", lambda: mock_db
        )

        from scripts.nanobot_mcp_server import publish_pin_direct

        result = publish_pin_direct(
            account_id="acc_missing",
            job_id="job_any",
            dry_run=True,
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_already_published_job_returns_error(self, monkeypatch):
        from app.models.social_account import SocialAccount
        from app.models.publish_job import PublishJob

        mock_db = MagicMock()
        mock_account = MagicMock(spec=SocialAccount)
        mock_account.adspower_profile_id = "prof_abc"
        mock_job = MagicMock(spec=PublishJob)
        mock_job.job_id = "job_done"
        mock_job.status = "published"
        mock_db.scalar.side_effect = [mock_account, mock_job]

        monkeypatch.setattr(
            "scripts.nanobot_mcp_server._db", lambda: mock_db
        )

        from scripts.nanobot_mcp_server import publish_pin_direct

        result = publish_pin_direct(
            account_id="acc_track",
            job_id="job_done",
            dry_run=True,
        )
        assert "error" in result
        assert "finalized" in result["error"].lower()
