"""PTY autonomous-turn 全量镜像：让 idle-time 自主 turn 的产出进入聊天。

任务 27 实录（2026-07-13）：后台监视器（Bash run_in_background）正点回调、
session 自主醒来并写出完整报告——但 adapter 在 chat turn 结束时把
``session.on_autonomous_event`` 降级成 ``_subagent_only_callback``（只喂
子 agent 面板），assistant 的 text/thinking/tool_use 全部被丢弃：报告只
存在于 session JSONL，聊天里永久不可见（idle watcher 消费过的记录 reader
offset 已越过，下一条消息的 orphan 回填也捞不回来）。

历史包袱：降级的前身是 on_exit 直接 ``on_autonomous_event = None``，防的
是"重放旧 prompt"——idle watcher 产出的 user-role 事件曾被原样镜像成重复
的用户消息。该风险现由 ``InstanceManager._process_event`` 的 autonomous
user-role 消毒承担（<task-notification> 压成一行 system_event，其余 user
记录直接丢弃），因此这里可以安全恢复全量转发；子 agent 面板的 upsert 在
``_process_event`` 内部完成，行为不变。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_pty.adapters.ccm import CCMBackend

logger = logging.getLogger(__name__)


class FullMirrorCCMBackend(CCMBackend):
    """CCMBackend with exact PTY-turn finalization and full idle mirroring.

    The dependency adapter finalizes by reusable instance/task ids, which is
    insufficient once a hot PTY Session/PID hosts several turns. This subclass
    owns terminal bookkeeping with CCM's durable generation fences and keeps
    autonomous output on the full ``_process_event`` path.
    """

    async def on_event(self, key: Any, event_dict: dict, **context) -> None:
        """Forward a foreground event with its immutable PTY turn identity."""

        await self._im.wait_for_pty_launch_metadata(key)
        consumer = asyncio.current_task()
        record = getattr(
            consumer, "_ccm_output_consumer_record", None
        )
        try:
            await self._im._process_event(
                key,
                context.get("task_id"),
                event_dict,
                context.get("loop_iteration"),
                consumer_record=record,
            )
        except Exception:
            logger.exception(
                "PTY on_event failed for instance %s task %s",
                key,
                context.get("task_id"),
            )

    async def on_exit(self, key: Any, exit_code: int | None, **context) -> None:
        # claude_pty starts its consumer before InstanceManager persists the
        # initial running metadata. A short turn must not write idle first and
        # then be overwritten by that late running commit.
        await self._im.wait_for_pty_launch_metadata(key)
        consumer = asyncio.current_task()
        record = getattr(
            consumer, "_ccm_output_consumer_record", None
        )
        # A docker-exec client can report exit while a detached command still
        # runs in the project container.  Prove the exact tokenized generation
        # gone before the adapter publishes an idle/reusable Instance.
        await self._im.finalize_pty_container_exec(
            key,
            expected_process=getattr(record, "process", None),
        )
        session = context.get("session")
        task_id = context.get("task_id")
        chat_initiated = bool(context.get("chat_initiated", False))
        ec = exit_code if exit_code is not None else 0

        # The upstream CCM adapter finalizes with only instance_id/task_id.
        # That is unsafe for PTY hot reuse: many turns share one Session/PID,
        # and a late old callback can clear a newer same-slot owner.  Keep pool
        # rotation, but only while this callback still owns the exact immutable
        # consumer record; all terminal DB state is committed by the manager's
        # Task+Instance generation CAS below.
        owns_record = bool(
            record is not None
            and getattr(record, "task", None) is consumer
            and self._im._consumer_records.get(key) is record
            and self._im._tasks.get(key) is consumer
            and self._im.processes.get(key) is getattr(record, "process", None)
        )
        if chat_initiated and task_id and owns_record and ec not in (0, -2, 130):
            try:
                rotated = await self._im._try_chat_pool_rotation(
                    key, task_id, ec, ""
                )
                if rotated:
                    old_proxy = record.process
                    new_proxy = self._proxies.get(key)
                    if new_proxy is not None and new_proxy is not old_proxy:
                        new_proxy.chain(old_proxy)
                    else:
                        old_proxy.complete(ec)
                    return
            except Exception:
                logger.exception(
                    "Pool rotation check failed for instance %s", key
                )

        final_status = None
        if chat_initiated and task_id and owns_record:
            final_status = await self._im.finalize_pty_chat_generation(
                key,
                task_id,
                ec,
                record,
            )

        if final_status == "completed" and ec == 0:
            await self._maybe_retry_empty_reply(key, task_id)

        # Exact identity cleanup replaces CCMBackend.on_exit.  Calling the
        # upstream method after a replacement is registered would let it read
        # ``session._ccm_proxy`` from the replacement and complete/pop the
        # wrong turn.  A stale callback is allowed to complete only its own
        # proxy; every instance-keyed map uses an identity guard.
        process = getattr(record, "process", None)
        if process is not None:
            if self._proxies.get(key) is process:
                self._proxies.pop(key, None)
            process.complete(ec)
        if self._consumers.get(key) is consumer:
            self._consumers.pop(key, None)
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
        if process is not None and self._im.processes.get(key) is process:
            self._im.processes.pop(key, None)
        if self._im._tasks.get(key) is consumer:
            self._im._tasks.pop(key, None)

        # Keep native sub-agent transcript progress flowing after the foreground
        # turn.  Unlike the upstream adapter we never downgrade the callback;
        # the full mirror already sanitizes autonomous user-role echoes.
        if chat_initiated and session is not None:
            tracker = getattr(
                getattr(session, "_reader", None), "_tracker", None
            )
            if tracker is not None and tracker.has_pending:
                asyncio.create_task(
                    self._poll_subagent_transcripts(tracker, task_id)
                )

        try:
            session = session or self._sessions.get(key)
            self._restore_full_autonomous_mirror(
                session,
                key,
                context.get("task_id"),
                context.get("loop_iteration"),
            )
        except Exception:
            logger.exception(
                "Failed to restore full autonomous mirror for key=%s", key
            )

    async def _maybe_retry_empty_reply(
        self,
        instance_id: int,
        task_id: int,
    ) -> None:
        """Preserve the adapter's one-shot empty-response recovery."""

        params = self._im._launch_params.get(instance_id)
        if not params or params.get("_retried"):
            return
        try:
            assistant_texts = await self._get_recent_assistant_texts(task_id)
            combined = " ".join(assistant_texts).strip().lower().rstrip(".")
            if assistant_texts and combined not in {
                "no response requested",
                "no response needed",
            }:
                return
            params["_retried"] = True
            logger.warning(
                "Task %d got empty/non-response (%r), re-enqueueing",
                task_id,
                combined[:80],
            )
            from backend.main import dispatcher
            from backend.services.dispatcher import PRIORITY_USER

            await dispatcher.enqueue_message(
                task_id=task_id,
                prompt=params["prompt"],
                priority=PRIORITY_USER,
                source="retry",
            )
        except Exception:
            logger.exception(
                "Empty-reply retry check failed for task %s", task_id
            )

    def _restore_full_autonomous_mirror(
        self,
        session: Any,
        key: Any,
        task_id: int | None,
        loop_iteration: int | None,
    ) -> None:
        if session is None:
            return
        current = getattr(session, "on_autonomous_event", None)
        if getattr(current, "__name__", "") != "_subagent_only_callback":
            return

        im = self._im
        expected_session_id = getattr(session, "session_id", None)

        async def _full_autonomous_mirror(event, **ctx):
            try:
                event_data = event.to_dict()
                event_data["autonomous"] = True
                await im._process_event(
                    key,
                    task_id,
                    event_data,
                    loop_iteration,
                    detached_autonomous=True,
                    expected_session_id=expected_session_id,
                )
            except Exception:
                logger.exception(
                    "Autonomous mirror failed for task %s (instance %s)",
                    task_id, key,
                )

        session.on_autonomous_event = _full_autonomous_mirror
        logger.info(
            "Autonomous full mirror armed for task %s (instance %s)",
            task_id, key,
        )
