"""Tests for dispatcher auto top-up of idle worker instances."""
import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import select

from backend.models.instance import Instance
from backend.services.dispatcher import GlobalDispatcher


@pytest.fixture
def dispatcher(db_factory):
    d = GlobalDispatcher.__new__(GlobalDispatcher)
    d.db_factory = db_factory
    d.broadcaster = MagicMock()
    d.instance_manager = MagicMock()
    d._running_tasks = {}
    return d


async def _seed(db_factory, statuses: list[str], name_prefix: str = "worker-"):
    async with db_factory() as db:
        for i, st in enumerate(statuses):
            db.add(Instance(name=f"{name_prefix}{i + 1}", status=st))
        await db.commit()


async def _all_instances(db_factory):
    async with db_factory() as db:
        result = await db.execute(select(Instance).order_by(Instance.id))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_tops_up_idle_to_min(dispatcher, db_factory):
    """4 idle + 2 busy → add 6 so idle count reaches 10."""
    await _seed(db_factory, ["idle"] * 4 + ["running"] * 2)
    with patch("backend.services.dispatcher.settings") as s:
        s.max_concurrent_instances = 0  # 0 = 不设并发硬上限
        s.min_idle_instances = 10
        await dispatcher._ensure_min_idle_instances()

    instances = await _all_instances(db_factory)
    assert len(instances) == 12
    assert sum(1 for i in instances if i.status == "idle") == 10


@pytest.mark.asyncio
async def test_no_op_when_enough_idle(dispatcher, db_factory):
    await _seed(db_factory, ["idle"] * 10)
    with patch("backend.services.dispatcher.settings") as s:
        s.max_concurrent_instances = 0  # 0 = 不设并发硬上限
        s.min_idle_instances = 10
        await dispatcher._ensure_min_idle_instances()
    assert len(await _all_instances(db_factory)) == 10


@pytest.mark.asyncio
async def test_disabled_when_zero(dispatcher, db_factory):
    await _seed(db_factory, ["idle"])
    with patch("backend.services.dispatcher.settings") as s:
        s.max_concurrent_instances = 0  # 0 = 不设并发硬上限
        s.min_idle_instances = 0
        await dispatcher._ensure_min_idle_instances()
    assert len(await _all_instances(db_factory)) == 1


@pytest.mark.asyncio
async def test_names_continue_from_highest_suffix(dispatcher, db_factory):
    """Names must continue past the highest worker-N, even after deletions."""
    async with db_factory() as db:
        db.add(Instance(name="worker-7", status="running"))
        db.add(Instance(name="custom-name", status="error"))
        await db.commit()

    with patch("backend.services.dispatcher.settings") as s:
        s.max_concurrent_instances = 0  # 0 = 不设并发硬上限
        s.min_idle_instances = 2
        await dispatcher._ensure_min_idle_instances()

    instances = await _all_instances(db_factory)
    new_names = {i.name for i in instances if i.status == "idle"}
    assert new_names == {"worker-8", "worker-9"}


@pytest.mark.asyncio
async def test_idempotent_across_polls(dispatcher, db_factory):
    """Repeated polls (dispatch loop) must not keep adding instances."""
    with patch("backend.services.dispatcher.settings") as s:
        s.max_concurrent_instances = 0  # 0 = 不设并发硬上限
        s.min_idle_instances = 10
        await dispatcher._ensure_min_idle_instances()
        await dispatcher._ensure_min_idle_instances()
        await dispatcher._ensure_min_idle_instances()
    assert len(await _all_instances(db_factory)) == 10


@pytest.mark.asyncio
async def test_terminal_instances_do_not_consume_cap(dispatcher, db_factory):
    """Error/stopped history must not prevent replenishing idle capacity."""
    await _seed(db_factory, ["error"] * 8 + ["stopped"])

    with patch("backend.services.dispatcher.settings") as s:
        s.max_concurrent_instances = 8
        s.min_idle_instances = 2
        await dispatcher._ensure_min_idle_instances()

    instances = await _all_instances(db_factory)
    assert sum(1 for i in instances if i.status == "idle") == 2
    assert sum(1 for i in instances if i.status in ("idle", "running")) == 2


@pytest.mark.asyncio
async def test_terminal_instances_do_not_bypass_live_cap(dispatcher, db_factory):
    """Top-up may replace terminal capacity but cannot exceed the live cap."""
    await _seed(db_factory, ["running"] * 7 + ["error"] * 9)

    with patch("backend.services.dispatcher.settings") as s:
        s.max_concurrent_instances = 8
        s.min_idle_instances = 2
        await dispatcher._ensure_min_idle_instances()

    instances = await _all_instances(db_factory)
    assert sum(1 for i in instances if i.status == "idle") == 1
    assert sum(1 for i in instances if i.status in ("idle", "running")) == 8
