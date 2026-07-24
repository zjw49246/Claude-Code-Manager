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
from datetime import datetime, timedelta

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

    async def test_detached_autonomous_event_cannot_touch_reused_instance(
        self,
        db_factory,
    ):
        """An idle PTY callback remains task-scoped after its slot is reused."""

        heartbeat = datetime.utcnow() - timedelta(minutes=5)
        async with db_factory() as db:
            inst = Instance(
                name="reused-autonomous-slot",
                status="running",
                pid=8831,
                last_heartbeat=heartbeat,
            )
            old_task = Task(
                title="old",
                description="old",
                status="completed",
                session_id="session-old",
            )
            new_task = Task(
                title="new",
                description="new",
                status="executing",
                session_id="session-new",
            )
            db.add_all([inst, old_task, new_task])
            await db.flush()
            inst.current_task_id = new_task.id
            new_task.instance_id = inst.id
            await db.commit()
            inst_id = inst.id
            old_task_id = old_task.id

        im, broadcaster = _make_im(db_factory)
        await im._process_event(
            inst_id,
            old_task_id,
            {
                "event_type": "message",
                "role": "assistant",
                "content": "late autonomous report",
                "autonomous": True,
                "context_usage": {
                    "input_tokens": 30,
                    "total_input_tokens": 30,
                },
            },
            detached_autonomous=True,
            expected_session_id="session-old",
        )

        async with db_factory() as db:
            current_instance = await db.get(Instance, inst_id)
            current_old_task = await db.get(Task, old_task_id)
            assert current_instance.last_heartbeat == heartbeat
            assert current_instance.current_task_id == new_task.id
            assert current_old_task.has_unread is True
            assert current_old_task.context_window_usage is None

        channels = [c.args[0] for c in broadcaster.broadcast.await_args_list]
        assert f"task:{old_task_id}" in channels
        assert f"instance:{inst_id}" not in channels


class TestFullMirrorBackend:
    """on_exit 后把降级的 subagent-only 回调换回全量转发。"""

    def _bare_backend(self, im=None):
        from backend.services.pty_full_mirror import FullMirrorCCMBackend
        backend = object.__new__(FullMirrorCCMBackend)  # 跳过 BridgeHub 启动
        backend._im = im or MagicMock()
        backend._sessions = {}
        backend._consumers = {}
        backend._proxies = {}
        return backend

    async def test_foreground_event_forwards_immutable_consumer_record(self):
        im = MagicMock()
        im._process_event = AsyncMock()
        im.wait_for_pty_launch_metadata = AsyncMock()
        backend = self._bare_backend(im)
        consumer = asyncio.current_task()
        record = MagicMock()
        previous = getattr(
            consumer, "_ccm_output_consumer_record", None
        )
        setattr(consumer, "_ccm_output_consumer_record", record)
        try:
            event = {
                "event_type": "message",
                "role": "assistant",
                "content": "foreground",
            }
            await backend.on_event(
                7,
                event,
                task_id=27,
                loop_iteration=3,
            )
        finally:
            if previous is None:
                delattr(consumer, "_ccm_output_consumer_record")
            else:
                setattr(
                    consumer,
                    "_ccm_output_consumer_record",
                    previous,
                )

        im._process_event.assert_awaited_once_with(
            7,
            27,
            event,
            3,
            consumer_record=record,
        )
        im.wait_for_pty_launch_metadata.assert_awaited_once_with(7)

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
        im.finalize_pty_container_exec = AsyncMock()
        backend = self._bare_backend(im)
        session = MagicMock()
        session._reader._tracker.has_pending = False

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
            im.finalize_pty_container_exec.assert_awaited_once_with(
                7, expected_process=None
            )
            # FullMirror owns exact terminal bookkeeping locally; delegating
            # would reintroduce the dependency's id-only stale writes.
            base_on_exit.assert_not_awaited()

    async def test_exact_pty_generation_finalizes_task_and_instance(
        self, db_factory
    ):
        im, broadcaster = _make_im(db_factory)
        backend = self._bare_backend(im)
        instance_id, task_id = await _make_inst_task(db_factory)
        started_at = datetime.utcnow()

        async with db_factory() as db:
            task = await db.get(Task, task_id)
            task.status = "executing"
            task.retry_count = 4
            task.instance_id = instance_id
            inst = await db.get(Instance, instance_id)
            inst.status = "running"
            inst.pid = 321
            inst.current_task_id = task_id
            inst.started_at = started_at
            await db.commit()

        class Proxy:
            pid = 321
            returncode = None

            def complete(self, code=0):
                self.returncode = code

        proxy = Proxy()
        session = MagicMock()
        session._reader._tracker.has_pending = False
        backend._sessions[instance_id] = session
        backend._proxies[instance_id] = proxy
        im._launch_params[instance_id] = {"_retried": True}

        async def exit_turn():
            consumer = asyncio.current_task()
            backend._consumers[instance_id] = consumer
            im.processes[instance_id] = proxy
            im._track_output_consumer(
                instance_id,
                proxy,
                consumer,
                chat_initiated=True,
                provider="claude",
                task_id=task_id,
                task_retry_count=4,
                instance_started_at=started_at,
            )
            await backend.on_exit(
                instance_id,
                0,
                session=session,
                task_id=task_id,
                chat_initiated=True,
            )

        await exit_turn()

        async with db_factory() as db:
            task = await db.get(Task, task_id)
            inst = await db.get(Instance, instance_id)
            assert task.status == "completed"
            assert task.retry_count == 4
            assert inst.status == "idle"
            assert inst.pid is None
            assert inst.current_task_id is None
        assert proxy.returncode == 0
        status_events = [
            call.args[1]
            for call in broadcaster.broadcast.await_args_list
            if call.args[0] == "tasks"
        ]
        assert any(
            event.get("new_status") == "completed"
            for event in status_events
        )

    async def test_failed_pty_generation_records_terminal_timestamp(
        self, db_factory
    ):
        im, _ = _make_im(db_factory)
        instance_id, task_id = await _make_inst_task(db_factory)
        started_at = datetime.utcnow()

        class Proxy:
            pid = 777
            returncode = 9

        proxy = Proxy()
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            task.status = "executing"
            task.retry_count = 2
            task.instance_id = instance_id
            inst = await db.get(Instance, instance_id)
            inst.status = "running"
            inst.pid = proxy.pid
            inst.current_task_id = task_id
            inst.started_at = started_at
            await db.commit()

        consumer = asyncio.current_task()
        im.processes[instance_id] = proxy
        im._track_output_consumer(
            instance_id,
            proxy,
            consumer,
            chat_initiated=True,
            provider="claude",
            task_id=task_id,
            task_retry_count=2,
            instance_started_at=started_at,
        )
        status = await im.finalize_pty_chat_generation(
            instance_id,
            task_id,
            9,
            im._consumer_records[instance_id],
        )
        assert status == "failed"
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            inst = await db.get(Instance, instance_id)
            assert task.status == "failed"
            assert task.completed_at is not None
            assert "code 9" in task.error_message
            assert inst.status == "error"

    @pytest.mark.parametrize("changed_field", ["retry", "started_at"])
    async def test_old_pty_exit_cannot_finalize_new_same_task_generation(
        self, db_factory, changed_field
    ):
        im, _ = _make_im(db_factory)
        backend = self._bare_backend(im)
        instance_id, task_id = await _make_inst_task(db_factory)
        old_started_at = datetime.utcnow()
        durable_started_at = (
            old_started_at + timedelta(seconds=1)
            if changed_field == "started_at"
            else old_started_at
        )
        durable_retry = 8 if changed_field == "retry" else 7

        async with db_factory() as db:
            task = await db.get(Task, task_id)
            task.status = "executing"
            task.retry_count = durable_retry
            task.instance_id = instance_id
            inst = await db.get(Instance, instance_id)
            inst.status = "running"
            inst.pid = 654
            inst.current_task_id = task_id
            inst.started_at = durable_started_at
            await db.commit()

        class Proxy:
            pid = 654
            returncode = None

            def complete(self, code=0):
                self.returncode = code

        proxy = Proxy()
        session = MagicMock()
        session._reader._tracker.has_pending = False
        backend._sessions[instance_id] = session
        backend._proxies[instance_id] = proxy

        async def exit_old_turn():
            consumer = asyncio.current_task()
            backend._consumers[instance_id] = consumer
            im.processes[instance_id] = proxy
            im._track_output_consumer(
                instance_id,
                proxy,
                consumer,
                chat_initiated=True,
                provider="claude",
                task_id=task_id,
                task_retry_count=7,
                instance_started_at=old_started_at,
            )
            await backend.on_exit(
                instance_id,
                0,
                session=session,
                task_id=task_id,
                chat_initiated=True,
            )

        await exit_old_turn()

        async with db_factory() as db:
            task = await db.get(Task, task_id)
            inst = await db.get(Instance, instance_id)
            assert task.status == "executing"
            assert task.retry_count == durable_retry
            assert inst.status == "running"
            assert inst.pid == 654
            assert inst.current_task_id == task_id
            assert inst.started_at == durable_started_at

    async def test_stale_pty_callback_keeps_replacement_maps(self, db_factory):
        im, _ = _make_im(db_factory)
        backend = self._bare_backend(im)
        instance_id, task_id = await _make_inst_task(db_factory)

        class Proxy:
            def __init__(self, pid):
                self.pid = pid
                self.returncode = None

            def complete(self, code=0):
                self.returncode = code

        old_proxy = Proxy(111)
        new_proxy = Proxy(222)
        old_session = MagicMock()
        old_session._reader._tracker.has_pending = False
        new_session = MagicMock()
        backend._sessions[instance_id] = new_session
        backend._proxies[instance_id] = new_proxy

        replacement_ready = asyncio.Event()
        release_old = asyncio.Event()

        async def old_exit():
            consumer = asyncio.current_task()
            from backend.services.instance_manager import _OutputConsumerRecord

            old_record = _OutputConsumerRecord(
                old_proxy,
                consumer,
                True,
                "claude",
                task_id,
                0,
                datetime.utcnow(),
            )
            setattr(
                consumer, "_ccm_output_consumer_record", old_record
            )
            replacement_ready.set()
            await release_old.wait()
            await backend.on_exit(
                instance_id,
                0,
                session=old_session,
                task_id=task_id,
                chat_initiated=True,
            )

        old_task = asyncio.create_task(old_exit())
        await replacement_ready.wait()
        new_consumer = asyncio.create_task(asyncio.sleep(60))
        try:
            from backend.services.instance_manager import _OutputConsumerRecord

            new_record = _OutputConsumerRecord(
                new_proxy,
                new_consumer,
                True,
                "claude",
                task_id,
                0,
                datetime.utcnow(),
            )
            backend._consumers[instance_id] = new_consumer
            im._tasks[instance_id] = new_consumer
            im._consumer_records[instance_id] = new_record
            im.processes[instance_id] = new_proxy
            release_old.set()
            await old_task
            assert backend._proxies[instance_id] is new_proxy
            assert backend._sessions[instance_id] is new_session
            assert backend._consumers[instance_id] is new_consumer
            assert im.processes[instance_id] is new_proxy
            assert im._tasks[instance_id] is new_consumer
            assert im._consumer_records[instance_id] is new_record
            assert old_proxy.returncode == 0
            assert new_proxy.returncode is None
        finally:
            new_consumer.cancel()
            await asyncio.gather(new_consumer, return_exceptions=True)

    async def test_mirror_forwards_to_process_event(self):
        im = MagicMock()
        im._process_event = AsyncMock()
        backend = self._bare_backend(im)
        session = MagicMock()

        async def _subagent_only_callback(event, **ctx):
            pass

        session.on_autonomous_event = _subagent_only_callback
        session.session_id = "session-27"
        backend._restore_full_autonomous_mirror(session, 7, 27, 3)

        event = MagicMock()
        event.to_dict.return_value = {
            "event_type": "message", "role": "assistant",
            "content": "hi", "autonomous": True,
        }
        await session.on_autonomous_event(event)
        im._process_event.assert_awaited_once_with(
            7,
            27,
            event.to_dict.return_value,
            3,
            detached_autonomous=True,
            expected_session_id="session-27",
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
