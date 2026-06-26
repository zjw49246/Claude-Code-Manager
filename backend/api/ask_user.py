"""ask_user 端点 — 拦截内置 AskUserQuestion，转前端卡片再把答案喂回模型。

- POST /api/ask-user/wait        ：hook 脚本调用，阻塞直到用户回答 / 超时
- GET  /api/tasks/{id}/ask-user/pending     ：前端重连时回填活跃卡片
- POST /api/tasks/{id}/ask-user/{request_id}：前端卡片回包 → resolve

阻塞等待期间**不持有任何 DB 连接**：所有 DB 操作都用独立的短生命周期 session，
await future 时不占连接（否则一个挂起的提问会长时间占住连接池）。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update

from backend.database import async_session
from backend.models.log_entry import LogEntry
from backend.models.task import Task
from backend.services.ask_user import ask_user_registry, format_answer_reason

router = APIRouter(prefix="/api", tags=["ask-user"])


class AskUserWaitRequest(BaseModel):
    session_id: str
    questions: list[dict]
    cwd: str | None = None
    tool_use_id: str | None = None


class AskUserAnswer(BaseModel):
    # 与 questions 对齐：每项一个回答
    answers: list[dict]


@router.post("/ask-user/wait")
async def ask_user_wait(body: AskUserWaitRequest):
    """hook 脚本调用：登记提问、广播卡片，阻塞直到用户回答或超时。

    返回 {answered: true, reason} → hook 用 deny+reason 把答案喂回模型；
    返回 {answered: false, ...} → hook 放行原生 AskUserQuestion（兜底）。
    """
    from backend.config import settings
    from backend.main import broadcaster

    if not body.questions:
        return {"answered": False, "reason": "no questions"}

    # 按 session_id 找 Task（同 PTY 权限透传：取最新一条）
    async with async_session() as db:
        task = (
            await db.execute(
                select(Task)
                .where(Task.session_id == body.session_id)
                .order_by(Task.id.desc())
            )
        ).scalars().first()
        if task is None:
            # 非 CCM 管理的 session → 放行，让原生工具按默认行为处理
            return {"answered": False, "no_session": True}
        task_id = task.id
        instance_id = task.instance_id or 1

    pending = ask_user_registry.create(
        task_id=task_id,
        session_id=body.session_id,
        questions=body.questions,
        tool_use_id=body.tool_use_id,
    )
    timeout = max(10, int(getattr(settings, "ask_user_timeout", 1800)))

    summary = _questions_summary(body.questions)

    # 落库（审计用，不进 chat 历史 allowed）+ 标记 task 未读 + 广播活跃卡片
    # has_unread 让任务列表亮起未读点，即便用户当前不在该 task 页面也能察觉。
    async with async_session() as db:
        db.add(LogEntry(
            instance_id=instance_id,
            task_id=task_id,
            event_type="ask_user_question",
            role="system",
            content=summary,
            tool_name="AskUserQuestion",
            tool_input=json.dumps(body.questions, ensure_ascii=False),
            raw_json=json.dumps({"request_id": pending.request_id}, ensure_ascii=False),
        ))
        await db.execute(
            update(Task).where(Task.id == task_id).values(has_unread=True)
        )
        await db.commit()

    # 该 task 频道：渲染内联卡片（用户正在看这个 task 时）
    await broadcaster.broadcast(f"task:{task_id}", {
        "event_type": "ask_user_question",
        "request_id": pending.request_id,
        "questions": body.questions,
        "timeout_seconds": timeout,
    })
    # 全局 tasks 频道：弹出全局通知，让在别的页面的用户也能看到并跳转过来
    await broadcaster.broadcast("tasks", {
        "event": "ask_user_pending",
        "task_id": task_id,
        "request_id": pending.request_id,
        "summary": summary,
    })

    try:
        answers = await asyncio.wait_for(pending.future, timeout=timeout)
    except asyncio.TimeoutError:
        ask_user_registry.discard(pending.request_id)
        await broadcaster.broadcast(f"task:{task_id}", {
            "event_type": "ask_user_resolved",
            "request_id": pending.request_id,
            "timed_out": True,
        })
        await broadcaster.broadcast("tasks", {
            "event": "ask_user_resolved",
            "task_id": task_id,
            "request_id": pending.request_id,
        })
        return {"answered": False, "timed_out": True}
    except asyncio.CancelledError:
        # hook 断开连接 → 清理 pending，避免泄漏
        ask_user_registry.discard(pending.request_id)
        raise
    finally:
        ask_user_registry.discard(pending.request_id)

    reason = format_answer_reason(body.questions, answers)
    return {"answered": True, "reason": reason, "answers": answers}


@router.get("/tasks/{task_id}/ask-user/pending")
async def ask_user_pending(task_id: int):
    """前端重连时回填仍在等待回答的卡片。"""
    pendings = ask_user_registry.list_for_task(task_id)
    return {
        "pending": [
            {"request_id": p.request_id, "questions": p.questions}
            for p in pendings
        ]
    }


@router.get("/ask-user/pending")
async def ask_user_pending_all():
    """全局：所有仍在等待回答的提问。

    前端刷新/重连时回填全局通知，让用户即便不在对应 task 页面也能看到
    哪些任务正在等待回答（避免 WS 卡片 live-only 在刷新后丢失）。
    """
    pendings = ask_user_registry.list_all()
    return {
        "pending": [
            {
                "task_id": p.task_id,
                "request_id": p.request_id,
                "summary": _questions_summary(p.questions),
            }
            for p in pendings
        ]
    }


@router.post("/tasks/{task_id}/ask-user/{request_id}")
async def ask_user_submit(task_id: int, request_id: str, body: AskUserAnswer):
    """前端卡片回包：把用户的选择 resolve 给阻塞中的 hook。"""
    from backend.main import broadcaster

    pending = ask_user_registry.get(request_id)
    if pending is None or pending.task_id != task_id:
        raise HTTPException(410, "提问已过期或不存在（hook 侧可能已超时放行）")

    ok = ask_user_registry.resolve(request_id, body.answers)
    if not ok:
        raise HTTPException(410, "提问已过期或不存在（hook 侧可能已超时放行）")

    # 持久化一条人类可读的回答记录（system_event 进 chat 历史）
    async with async_session() as db:
        db.add(LogEntry(
            instance_id=1,
            task_id=task_id,
            event_type="system_event",
            role="system",
            content=_answer_summary(pending.questions, body.answers),
            raw_json=json.dumps({"request_id": request_id}, ensure_ascii=False),
        ))
        await db.commit()

    await broadcaster.broadcast(f"task:{task_id}", {
        "event_type": "ask_user_resolved",
        "request_id": request_id,
        "answers": body.answers,
    })
    # 关掉别的页面上挂着的全局通知
    await broadcaster.broadcast("tasks", {
        "event": "ask_user_resolved",
        "task_id": task_id,
        "request_id": request_id,
    })
    return {"ok": True}


def _questions_summary(questions: list[dict]) -> str:
    qs = [q.get("question") or q.get("header") or "?" for q in questions]
    return "AskUserQuestion: " + " | ".join(qs)


def _answer_summary(questions: list[dict], answers: list[dict]) -> str:
    parts = []
    for idx, q in enumerate(questions):
        ans = answers[idx] if idx < len(answers) else {}
        labels = list(ans.get("labels") or [])
        text = (ans.get("text") or "").strip()
        if text:
            labels.append(f'"{text}"')
        header = q.get("header") or q.get("question") or f"Q{idx + 1}"
        parts.append(f"{header} → {', '.join(labels) if labels else '(无选择)'}")
    return "已回答: " + " | ".join(parts)
