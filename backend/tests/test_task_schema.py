"""Tests for TaskResponse datetime serialization — UTC timestamps must include timezone info."""
from datetime import datetime, timezone

import pytest

from backend.schemas.task import TaskResponse


def _make_task_response(**overrides) -> TaskResponse:
    defaults = dict(
        id=1, title="t", description="d", status="pending", priority=0,
        project_id=None, target_repo=None, target_branch="main",
        result_branch=None, merge_status="pending", instance_id=None,
        retry_count=0, max_retries=2, mode="auto", todo_file_path=None,
        loop_progress=None, max_iterations=50, must_complete=False,
        goal_condition=None, goal_evaluator_model=None, goal_max_turns=30,
        goal_turns_used=0, goal_last_reason=None, plan_content=None,
        plan_approved=None, session_id=None, provider="claude", model=None,
        effort_level=None, thinking_budget=None, enable_workflows=False,
        enabled_skills=None, starred=False, archived=False, has_unread=False,
        error_message=None, tags=None, metadata_=None, active_sub_agents=0,
        context_window_usage=None,
        created_at=datetime(2026, 6, 10, 14, 30, 0),
        started_at=None, completed_at=None,
    )
    defaults.update(overrides)
    return TaskResponse(**defaults)


class TestTaskResponseDatetimeSerialization:
    def test_naive_created_at_serialized_with_utc_suffix(self):
        resp = _make_task_response(created_at=datetime(2026, 6, 10, 14, 30, 0))
        data = resp.model_dump(mode="json")
        assert data["created_at"].endswith("+00:00")

    def test_aware_created_at_preserved(self):
        aware_dt = datetime(2026, 6, 10, 14, 30, 0, tzinfo=timezone.utc)
        resp = _make_task_response(created_at=aware_dt)
        data = resp.model_dump(mode="json")
        assert data["created_at"].endswith("+00:00")

    def test_started_at_none_serialized_as_none(self):
        resp = _make_task_response(started_at=None)
        data = resp.model_dump(mode="json")
        assert data["started_at"] is None

    def test_started_at_naive_gets_utc_suffix(self):
        resp = _make_task_response(started_at=datetime(2026, 6, 10, 15, 0, 0))
        data = resp.model_dump(mode="json")
        assert data["started_at"].endswith("+00:00")

    def test_completed_at_naive_gets_utc_suffix(self):
        resp = _make_task_response(completed_at=datetime(2026, 6, 10, 16, 0, 0))
        data = resp.model_dump(mode="json")
        assert data["completed_at"].endswith("+00:00")

    def test_all_three_timestamps_have_utc(self):
        resp = _make_task_response(
            created_at=datetime(2026, 1, 1),
            started_at=datetime(2026, 1, 2),
            completed_at=datetime(2026, 1, 3),
        )
        data = resp.model_dump(mode="json")
        for field in ("created_at", "started_at", "completed_at"):
            assert "+00:00" in data[field], f"{field} missing UTC offset"
