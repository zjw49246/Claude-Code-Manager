"""ask_user — 拦截内置 AskUserQuestion 工具，转成前端可选卡片再把答案喂回模型。

机制：CCM 在每次 launch 时把一个 PreToolUse hook 合并进 {config_dir}/settings.json
（见 ask_user_settings.py），hook 命中 AskUserQuestion 后调用 backend/hooks/ask_user_hook.py。
该脚本带着 questions 阻塞式 POST /api/ask-user/wait：
  1. 后端按 session_id 找到 Task，登记一个 pending future，广播 ask_user_question 卡片；
  2. 前端渲染卡片，用户选择后 POST /api/tasks/{id}/ask-user/{request_id} → resolve future；
  3. /wait 返回答案 + 一段 reason 文案；hook 以 PreToolUse deny + permissionDecisionReason
     的形式把 reason 回传给模型——deny 的 reason 会作为 tool_result（is_error=true）喂回，
     模型据此当作"用户的回答"继续（已实测，见 PROGRESS.md）。

这里只管理 request_id ↔ asyncio.Future 的生命周期；广播/落库/阻塞等待在 api/ask_user.py。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingAsk:
    request_id: str
    task_id: int
    session_id: str
    questions: list[dict]
    future: asyncio.Future
    tool_use_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class AskUserRegistry:
    """进程内单例：登记 / 解除 待回答的 AskUserQuestion 请求。"""

    def __init__(self) -> None:
        self._pending: dict[str, PendingAsk] = {}

    def create(
        self,
        task_id: int,
        session_id: str,
        questions: list[dict],
        tool_use_id: str | None = None,
    ) -> PendingAsk:
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        pending = PendingAsk(
            request_id=request_id,
            task_id=task_id,
            session_id=session_id,
            questions=questions,
            future=future,
            tool_use_id=tool_use_id,
        )
        self._pending[request_id] = pending
        return pending

    def get(self, request_id: str) -> PendingAsk | None:
        return self._pending.get(request_id)

    def resolve(self, request_id: str, answers: Any) -> bool:
        """前端回包：把答案塞进 future。未知/已完成返回 False。"""
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(answers)
        return True

    def discard(self, request_id: str) -> None:
        self._pending.pop(request_id, None)

    def list_for_task(self, task_id: int) -> list[PendingAsk]:
        """仍在等待回答的请求（前端重连时回填卡片用）。"""
        return [
            p for p in self._pending.values()
            if p.task_id == task_id and not p.future.done()
        ]


# 进程内单例
ask_user_registry = AskUserRegistry()


def format_answer_reason(questions: list[dict], answers: list[dict]) -> str:
    """把用户的选择拼成喂回模型的 deny reason 文案。

    answers: 与 questions 对齐的列表，每项 {header?, labels: [str], text?: str}。
    """
    lines = ["The user answered your question(s) through the UI:\n"]
    for idx, q in enumerate(questions):
        ans = answers[idx] if idx < len(answers) else {}
        labels = ans.get("labels") or []
        text = (ans.get("text") or "").strip()
        q_text = q.get("question") or q.get("header") or f"Question {idx + 1}"
        # 把选中的 label 对回它的 description，便于模型理解语义
        opt_desc = {o.get("label"): o.get("description", "") for o in q.get("options", [])}
        chosen: list[str] = []
        for lb in labels:
            d = opt_desc.get(lb)
            chosen.append(f"{lb} ({d})" if d else lb)
        if text:
            chosen.append(f'(custom answer) "{text}"')
        rendered = "; ".join(chosen) if chosen else "(no selection)"
        lines.append(f"Q{idx + 1}: {q_text}\n→ {rendered}")
    lines.append(
        "\nTreat the above as the user's reply and continue accordingly. "
        "Do NOT call AskUserQuestion again for the same question."
    )
    return "\n".join(lines)
