"""Notify shared users when a task's status changes (completed/failed)."""

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.task import Task
from backend.models.task_share import TaskShare
from backend.services import feishu_notify

logger = logging.getLogger(__name__)

_NOTIFY_STATUSES = {"completed", "failed", "cancelled"}


async def notify_shared_users_on_status_change(
    db_factory,
    task_id: int,
    new_status: str,
):
    """Best-effort notify all share recipients when a task reaches a terminal status."""
    if new_status not in _NOTIFY_STATUSES:
        return

    try:
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            if not task:
                return

            result = await db.execute(
                select(TaskShare).where(
                    TaskShare.task_id == task_id,
                    TaskShare.status == "active",
                )
            )
            shares = result.scalars().all()
            if not shares:
                return

            # Get sharer's name
            from backend.models.feishu_binding import FeishuUserBinding
            binding_result = await db.execute(select(FeishuUserBinding).limit(1))
            binding = binding_result.scalar_one_or_none()
            sharer_name = binding.feishu_name if binding else "Someone"

            task_title = task.title or f"Task #{task_id}"
            status_emoji = {"completed": "done", "failed": "failed", "cancelled": "cancelled"}.get(new_status, new_status)

            for share in shares:
                try:
                    await feishu_notify.send_status_notification(
                        recipient_open_id=share.shared_to_open_id,
                        sharer_name=sharer_name,
                        task_title=task_title,
                        new_status=status_emoji,
                    )
                except Exception:
                    logger.debug("Failed to send status notification for task %d to %s", task_id, share.shared_to_open_id)

    except Exception:
        logger.debug("notify_shared_users_on_status_change failed for task %d", task_id)
