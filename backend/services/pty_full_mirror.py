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

import logging
from typing import Any

from claude_pty.adapters.ccm import CCMBackend

logger = logging.getLogger(__name__)


class FullMirrorCCMBackend(CCMBackend):
    """CCMBackend，但 chat turn 结束后 autonomous 事件全量走 _process_event。

    ``super().on_exit`` 会把 ``session.on_autonomous_event`` 降级为
    ``_subagent_only_callback``；本类在其后原位换回全量转发。只在识别到
    降级回调（按函数名）时才替换——轮换 relaunch 已重新绑定
    ``base._on_autonomous`` 的 session、以及非 chat 会话都不受干扰。
    """

    async def on_exit(self, key: Any, exit_code: int | None, **context) -> None:
        # claude_pty starts its consumer before InstanceManager persists the
        # initial running metadata. A short turn must not write idle first and
        # then be overwritten by that late running commit.
        await self._im.wait_for_pty_launch_metadata(key)
        await super().on_exit(key, exit_code, **context)
        try:
            session = context.get("session") or self._sessions.get(key)
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

        async def _full_autonomous_mirror(event, **ctx):
            try:
                await im._process_event(
                    key, task_id, event.to_dict(), loop_iteration
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
