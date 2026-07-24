"""Shared admission guard for local Instance capacity mutations.

CCM runs one in-process dispatcher alongside the Instance API.  Both paths can
create reusable execution slots, so their count-and-insert transactions must be
serialized or concurrent requests can exceed ``max_concurrent_instances``.
"""

import asyncio

from sqlalchemy import and_, or_

from backend.models.instance import Instance


# Deliberately module-global: the API and GlobalDispatcher must use this exact
# lock around the complete live-count -> insert -> commit operation.
instance_capacity_lock = asyncio.Lock()

LIVE_INSTANCE_STATUSES = frozenset({"idle", "running"})


def reusable_idle_predicate():
    """Rows that can safely accept a new Task generation."""

    return and_(
        Instance.status == "idle",
        Instance.pid.is_(None),
        Instance.current_task_id.is_(None),
    )


def occupied_slot_predicate():
    """Rows that consume a configured worker slot."""

    return or_(
        Instance.status.in_(LIVE_INSTANCE_STATUSES),
        Instance.pid.isnot(None),
        Instance.current_task_id.isnot(None),
    )


def active_capacity_predicate():
    """Rows that consume launch concurrency, excluding reusable idle slots."""

    return or_(
        Instance.status == "running",
        Instance.pid.isnot(None),
        Instance.current_task_id.isnot(None),
    )


def instance_occupies_slot(instance: Instance) -> bool:
    """Terminal history is free only after all owner evidence is cleared."""

    return bool(
        instance.status in LIVE_INSTANCE_STATUSES
        or instance.pid is not None
        or instance.current_task_id is not None
    )


def instance_is_reusable_idle(instance: Instance) -> bool:
    return bool(
        instance.status == "idle"
        and instance.pid is None
        and instance.current_task_id is None
    )
