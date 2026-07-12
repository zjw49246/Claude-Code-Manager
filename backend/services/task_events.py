"""Task status_change 广播收口。

约定：任何写 Task.status 的路径，commit 之后必须广播 status_change 到
"tasks" 频道（broadcaster 会自动镜像到 task:{id} 频道）。此前 cancel/retry/
plan 审批/stop-session/stale 兜底/worker 断连等路径只写库不广播，导致
ChatView（WS 驱动）与任务列表（轮询驱动）状态分叉（2026-07 状态显示大排查）。

必须在 db.commit() 之后调用——先广播会让手快的客户端立刻回读到旧状态。
"""

import logging

logger = logging.getLogger(__name__)


async def broadcast_status_change(
    task_id: int, new_status: str, instance_id: int | None = None
) -> None:
    """Broadcast a task status_change on the "tasks" channel (best-effort)."""
    try:
        from backend.main import broadcaster

        data: dict = {
            "event": "status_change",
            "task_id": task_id,
            "new_status": new_status,
        }
        if instance_id is not None:
            data["instance_id"] = instance_id
        await broadcaster.broadcast("tasks", data)
    except Exception:
        # 广播失败不能影响状态写入本身；前端有 5s 轮询兜底
        logger.exception("status_change broadcast failed for task %s", task_id)
