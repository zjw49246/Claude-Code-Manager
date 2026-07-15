"""Native sub-agent integration — PTY 观测到的模型原生子 agent 接入通用子 agent 体系。

覆盖：
- SubAgentSession 通用模型（agent_type/source/meta 字段、旧名兼容别名）
- InstanceManager._upsert_native_sub_agent 的 spawn/progress/done 生命周期
- /sub-agents/summary 按 agent_type 分组
"""
import json

import pytest
from sqlalchemy import select

from backend.models.task import Task
from backend.models.sub_agent import SubAgentSession, SubAgentReport
from backend.models.monitor_session import MonitorSession, MonitorCheck


# ------------------------------------------------------------------ model


@pytest.mark.asyncio
async def test_generic_model_defaults(db_session):
    sa = SubAgentSession(task_id=1, description="原生子agent")
    db_session.add(sa)
    await db_session.commit()
    await db_session.refresh(sa)
    assert sa.agent_type == "monitor"  # 默认类别保持 monitor 兼容
    assert sa.source == "ccm"
    assert sa.meta is None


@pytest.mark.asyncio
async def test_native_agent_record(db_session):
    sa = SubAgentSession(
        task_id=1,
        agent_type="native-agent",
        source="native",
        description="摸清架构",
        meta=json.dumps({"tool_use_id": "toolu_x", "background": False}),
    )
    db_session.add(sa)
    await db_session.commit()
    await db_session.refresh(sa)
    assert sa.agent_type == "native-agent"
    assert json.loads(sa.meta)["tool_use_id"] == "toolu_x"


@pytest.mark.asyncio
async def test_legacy_aliases_still_work(db_session):
    """MonitorSession/MonitorCheck 别名 + monitor_session_id synonym 兼容旧调用点。"""
    ms = MonitorSession(task_id=2, description="legacy")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)
    assert isinstance(ms, SubAgentSession)

    check = MonitorCheck(monitor_session_id=ms.id, check_number=1, status="success")
    db_session.add(check)
    await db_session.commit()
    await db_session.refresh(check)
    assert check.session_id == ms.id
    assert check.monitor_session_id == ms.id

    loaded = (
        await db_session.execute(
            select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms.id)
        )
    ).scalars().first()
    assert loaded.id == check.id


# ------------------------------------------------------- upsert lifecycle


class _FakeBroadcaster:
    def __init__(self):
        self.events = []

    async def broadcast(self, channel, data):
        self.events.append((channel, data))


def _make_im(db_factory):
    """A minimal InstanceManager carrying just what _upsert_native_sub_agent needs."""
    from backend.services.instance_manager import InstanceManager

    im = InstanceManager.__new__(InstanceManager)
    im.db_factory = db_factory
    im.broadcaster = _FakeBroadcaster()
    return im


@pytest.fixture
def im(db_factory):
    return _make_im(db_factory)


@pytest.mark.asyncio
async def test_spawn_progress_done_lifecycle(im, db_session):
    info = {
        "tool_use_id": "toolu_abc",
        "kind": "native-monitor",
        "description": "watch smoke log",
    }
    await im._upsert_native_sub_agent(7, "subagent_spawn", info)

    row = (
        await db_session.execute(
            select(SubAgentSession).where(SubAgentSession.task_id == 7)
        )
    ).scalars().first()
    assert row is not None
    assert row.agent_type == "native-monitor"
    assert row.source == "native"
    assert row.status == "running"

    # replay safety: duplicate spawn does not create a second row
    await im._upsert_native_sub_agent(7, "subagent_spawn", info)
    rows = (
        await db_session.execute(
            select(SubAgentSession).where(SubAgentSession.task_id == 7)
        )
    ).scalars().all()
    assert len(rows) == 1

    await im._upsert_native_sub_agent(
        7, "subagent_progress", {**info, "summary": "step: deploy"}
    )
    await db_session.refresh(row)
    assert row.checks_done == 1
    assert "deploy" in row.last_summary

    await im._upsert_native_sub_agent(
        7, "subagent_done", {**info, "timed_out": True}
    )
    await db_session.refresh(row)
    assert row.status == "completed"
    assert row.completed_at is not None
    assert "[timed out]" in row.last_summary

    event_types = [d["event_type"] for _, d in im.broadcaster.events]
    assert event_types == [
        "sub_agent_session_created",
        "sub_agent_report",
        "system_event",  # subagent_progress 同时写入聊天 system_event
        "sub_agent_session_status",
    ]


@pytest.mark.asyncio
async def test_progress_for_unknown_agent_is_noop(im, db_session):
    await im._upsert_native_sub_agent(
        9, "subagent_progress", {"tool_use_id": "toolu_zzz", "summary": "x"}
    )
    rows = (
        await db_session.execute(
            select(SubAgentSession).where(SubAgentSession.task_id == 9)
        )
    ).scalars().all()
    assert rows == []
    assert im.broadcaster.events == []


@pytest.mark.asyncio
async def test_missing_tool_use_id_ignored(im, db_session):
    await im._upsert_native_sub_agent(9, "subagent_spawn", {"kind": "native-agent"})
    rows = (
        await db_session.execute(select(SubAgentSession))
    ).scalars().all()
    assert rows == []


# ------------------------------------------------------------- summary API


@pytest.mark.asyncio
async def test_summary_groups_by_agent_type(client, db_session):
    task = Task(title="t", description="d")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add_all([
        SubAgentSession(task_id=task.id, description="m1"),  # monitor/ccm running
        SubAgentSession(
            task_id=task.id, description="m2", status="completed"
        ),
        SubAgentSession(
            task_id=task.id, agent_type="native-agent", source="native",
            description="n1", status="running",
        ),
        SubAgentSession(
            task_id=task.id, agent_type="native-monitor", source="native",
            description="n2", status="completed",
        ),
    ])
    await db_session.commit()

    resp = await client.get(f"/api/tasks/{task.id}/sub-agents/summary")
    assert resp.status_code == 200
    by_type = resp.json()["by_type"]
    assert by_type["monitor"] == {"running": 1, "completed": 1}
    assert by_type["native-agent"] == {"running": 1, "completed": 0}
    assert by_type["native-monitor"] == {"running": 0, "completed": 1}
