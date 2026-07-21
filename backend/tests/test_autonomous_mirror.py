"""PTY autonomous-turn 全量镜像测试。

背景（2026-07-13 task 27 实录）：后台监视器正点回调、session 自主醒来写出
完整报告，但 adapter 在 chat turn 结束时把 on_autonomous_event 降级成
_subagent_only_callback，报告只存在于 JSONL、聊天永久不可见。

修复两半：
- FullMirrorCCMBackend.on_exit 在 super() 降级后原位换回全量转发；
- _process_event 对 autonomous user-role 事件消毒（<task-notification> 压成
  一行 system_event，其余丢弃），承担历史上"重放旧 prompt"的防线。
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from backend.services.instance_manager import InstanceManager
from backend.models.instance import Instance
from backend.models.task import Task
from backend.models.log_entry import LogEntry


async def _make_inst_task(db_factory):
    async with db_factory() as db:
        inst = Instance(name="t-mirror")
        task = Task(title="t", description="d")
        db.add(inst)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        return inst.id, task.id


def _make_im(db_factory):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    return InstanceManager(db_factory, broadcaster), broadcaster


async def _entries(db_factory, task_id):
    async with db_factory() as db:
        result = await db.execute(
            select(LogEntry).where(LogEntry.task_id == task_id).order_by(LogEntry.id)
        )
        return result.scalars().all()


class TestAutonomousUserSanitization:
    """_process_event：autonomous user-role 事件绝不入库为用户消息。"""

    async def test_task_notification_becomes_system_event(self, db_factory):
        inst_id, task_id = await _make_inst_task(db_factory)
        im, broadcaster = _make_im(db_factory)

        await im._process_event(inst_id, task_id, {
            "event_type": "message",
            "role": "user",
            "content": (
                "<task-notification>\n<task-id>bjv0gacf8</task-id>\n"
                "<tool-use-id>toolu_x</tool-use-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
            "autonomous": True,
        })

        entries = await _entries(db_factory, task_id)
        assert len(entries) == 1
        assert entries[0].event_type == "system_event"
        assert entries[0].role == "system"
        assert "bjv0gacf8" in entries[0].content
        assert "completed" in entries[0].content
        # 广播的也是消毒后的 system_event
        broadcast_events = [
            c.args[1] for c in broadcaster.broadcast.await_args_list
            if c.args[0] == f"task:{task_id}"
        ]
        assert any(e.get("event_type") == "system_event" for e in broadcast_events)
        assert not any(e.get("role") == "user" for e in broadcast_events)

    async def test_channel_echo_dropped(self, db_factory):
        """channel 注入回显（发送时已入库过）直接丢弃，不重复。"""
        inst_id, task_id = await _make_inst_task(db_factory)
        im, broadcaster = _make_im(db_factory)

        await im._process_event(inst_id, task_id, {
            "event_type": "message",
            "role": "user",
            "content": '<channel source="pty-bridge">\n看下进度\n</channel>',
            "autonomous": True,
        })

        assert await _entries(db_factory, task_id) == []
        broadcaster.broadcast.assert_not_awaited()

    async def test_non_autonomous_user_event_unchanged(self, db_factory):
        """非 autonomous 的 user 事件维持原行为（turn 内 orphan 回填依赖它）。"""
        inst_id, task_id = await _make_inst_task(db_factory)
        im, _ = _make_im(db_factory)

        await im._process_event(inst_id, task_id, {
            "event_type": "message",
            "role": "user",
            "content": '<channel source="pty-bridge">\n看下进度\n</channel>',
        })

        entries = await _entries(db_factory, task_id)
        assert len(entries) == 1
        assert entries[0].role == "user"

    async def test_autonomous_assistant_message_logged_and_unread(self, db_factory):
        """autonomous assistant 产出正常入库 + 亮未读 + 广播（修复的主目标）。"""
        inst_id, task_id = await _make_inst_task(db_factory)
        im, broadcaster = _make_im(db_factory)

        await im._process_event(inst_id, task_id, {
            "event_type": "message",
            "role": "assistant",
            "content": "# 第 5 轮结果：持平 20.78，没有再提高",
            "autonomous": True,
        })

        entries = await _entries(db_factory, task_id)
        assert len(entries) == 1
        assert entries[0].event_type == "message"
        assert "20.78" in entries[0].content
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            assert task.has_unread is True
        channels = [c.args[0] for c in broadcaster.broadcast.await_args_list]
        assert f"task:{task_id}" in channels


class TestFullMirrorBackend:
    """on_exit 后把降级的 subagent-only 回调换回全量转发。"""

    def _bare_backend(self, im=None):
        from backend.services.pty_full_mirror import FullMirrorCCMBackend
        backend = object.__new__(FullMirrorCCMBackend)  # 跳过 BridgeHub 启动
        backend._im = im or MagicMock()
        backend._sessions = {}
        return backend

    def test_restore_replaces_subagent_only(self):
        backend = self._bare_backend()
        session = MagicMock()

        async def _subagent_only_callback(event, **ctx):
            pass

        session.on_autonomous_event = _subagent_only_callback
        backend._restore_full_autonomous_mirror(session, 7, 27, None)
        assert session.on_autonomous_event is not _subagent_only_callback
        assert session.on_autonomous_event.__name__ == "_full_autonomous_mirror"

    async def test_on_exit_waits_for_initial_running_metadata_barrier(self):
        release_metadata = asyncio.Event()
        wait_entered = asyncio.Event()
        im = MagicMock()

        async def wait_for_metadata(instance_id):
            assert instance_id == 7
            wait_entered.set()
            await release_metadata.wait()

        im.wait_for_pty_launch_metadata = AsyncMock(
            side_effect=wait_for_metadata
        )
        backend = self._bare_backend(im)
        session = MagicMock()

        with patch(
            "backend.services.pty_full_mirror.CCMBackend.on_exit",
            new_callable=AsyncMock,
        ) as base_on_exit:
            exiting = asyncio.create_task(backend.on_exit(
                7,
                0,
                session=session,
                task_id=27,
            ))
            await wait_entered.wait()
            base_on_exit.assert_not_awaited()
            release_metadata.set()
            await exiting
            base_on_exit.assert_awaited_once()

    async def test_mirror_forwards_to_process_event(self):
        im = MagicMock()
        im._process_event = AsyncMock()
        backend = self._bare_backend(im)
        session = MagicMock()

        async def _subagent_only_callback(event, **ctx):
            pass

        session.on_autonomous_event = _subagent_only_callback
        backend._restore_full_autonomous_mirror(session, 7, 27, 3)

        event = MagicMock()
        event.to_dict.return_value = {
            "event_type": "message", "role": "assistant",
            "content": "hi", "autonomous": True,
        }
        await session.on_autonomous_event(event)
        im._process_event.assert_awaited_once_with(
            7, 27, event.to_dict.return_value, 3
        )

    async def test_mirror_swallows_process_event_errors(self):
        """镜像回调绝不向 idle watcher 抛异常。"""
        im = MagicMock()
        im._process_event = AsyncMock(side_effect=RuntimeError("db down"))
        backend = self._bare_backend(im)
        session = MagicMock()

        async def _subagent_only_callback(event, **ctx):
            pass

        session.on_autonomous_event = _subagent_only_callback
        backend._restore_full_autonomous_mirror(session, 7, 27, None)
        event = MagicMock()
        event.to_dict.return_value = {"event_type": "message"}
        await session.on_autonomous_event(event)  # 不抛

    def test_restore_skips_fresh_binding(self):
        """launch 重新绑定的 _on_autonomous（轮换 relaunch）不得被覆盖。"""
        backend = self._bare_backend()
        session = MagicMock()

        async def _on_autonomous(event):
            pass

        session.on_autonomous_event = _on_autonomous
        backend._restore_full_autonomous_mirror(session, 7, 27, None)
        assert session.on_autonomous_event is _on_autonomous

    def test_restore_skips_none_session(self):
        backend = self._bare_backend()
        backend._restore_full_autonomous_mirror(None, 7, 27, None)  # 不抛

    def test_init_wires_full_mirror_backend(self, db_factory):
        """use_pty_mode 默认开：IM 构造时就应接上 FullMirrorCCMBackend。"""
        fake_cls = MagicMock()
        with patch(
            "backend.services.pty_full_mirror.FullMirrorCCMBackend", fake_cls
        ):
            im, _ = _make_im(db_factory)
        fake_cls.assert_called_once_with(im)
        assert im._pty_enabled is True
