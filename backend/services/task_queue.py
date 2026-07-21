from datetime import datetime

from sqlalchemy import Float, case, delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models.instance import Instance
from backend.models.log_entry import LogEntry
from backend.models.task import Task


def _effective_key_expr(auto_sort_on_access: bool = True):
    """Build the SQL expression for task sort key.

    auto_sort_on_access=True:  COALESCE(sort_order, ts(last_accessed_at ?? created_at))
    auto_sort_on_access=False: COALESCE(sort_order, ts(created_at))
    """
    if auto_sort_on_access:
        fallback = func.strftime("%s", func.coalesce(Task.last_accessed_at, Task.created_at))
    else:
        fallback = func.strftime("%s", Task.created_at)
    return func.coalesce(Task.sort_order, func.cast(fallback, Float))


class TaskQueue:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, **kwargs) -> Task:
        task = Task(**kwargs)
        self.db.add(task)
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def get(self, task_id: int) -> Task | None:
        return await self.db.get(Task, task_id)

    async def _auto_sort_enabled(self) -> bool:
        from backend.models.global_settings import GlobalSettings
        gs = await self.db.get(GlobalSettings, 1)
        return gs.auto_sort_on_access if gs and gs.auto_sort_on_access is not None else True

    async def list_tasks(
        self, status: str | None = None, include_archived: bool = False,
        archived_only: bool = False,
        project_id: int | None = None, starred: bool | None = None,
        has_unread: bool | None = None,
        limit: int = 50, offset: int = 0,
        user_id: int | None = None,
    ) -> list[Task]:
        auto_sort = await self._auto_sort_enabled()
        effective_key = _effective_key_expr(auto_sort)
        stmt = select(Task).where(Task.shared_from_id.is_(None)).order_by(Task.starred.desc(), effective_key.desc(), Task.id.desc())
        if archived_only:
            stmt = stmt.where(Task.archived == True)
        elif not include_archived:
            stmt = stmt.where(Task.archived == False)
        if status:
            parts = [s.strip() for s in status.split(",") if s.strip()]
            stmt = stmt.where(Task.status.in_(parts)) if len(parts) > 1 else stmt.where(Task.status == parts[0])
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if starred is not None:
            stmt = stmt.where(Task.starred == starred)
        if has_unread is not None:
            stmt = stmt.where(Task.has_unread == has_unread)
        # Team CCM: member sees tasks on own Workers, created by them, or shared to them
        if user_id is not None:
            from backend.models.worker import Worker
            from backend.models.team_share import TeamTaskShare, TeamProjectShare
            from backend.models.user_group import UserGroupMember
            owned_worker_ids_q = select(Worker.id).where(Worker.owner_user_id == user_id)
            user_group_ids_q = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
            shared_task_ids_q = select(TeamTaskShare.task_id).where(
                ((TeamTaskShare.target_type == "user") & (TeamTaskShare.target_id == user_id))
                | ((TeamTaskShare.target_type == "group") & TeamTaskShare.target_id.in_(user_group_ids_q))
            )
            shared_project_ids_q = select(TeamProjectShare.project_id).where(
                ((TeamProjectShare.target_type == "user") & (TeamProjectShare.target_id == user_id))
                | ((TeamProjectShare.target_type == "group") & TeamProjectShare.target_id.in_(user_group_ids_q))
            )
            stmt = stmt.where(
                (Task.created_by == user_id)
                | Task.worker_id.in_(owned_worker_ids_q)
                | Task.id.in_(shared_task_ids_q)
                | Task.project_id.in_(shared_project_ids_q)
            )
        stmt = stmt.limit(limit).offset(offset)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_tasks(
        self, status: str | None = None, include_archived: bool = False,
        archived_only: bool = False,
        project_id: int | None = None, starred: bool | None = None,
        has_unread: bool | None = None,
        user_id: int | None = None,
    ) -> int:
        stmt = select(func.count(Task.id)).where(Task.shared_from_id.is_(None))
        if archived_only:
            stmt = stmt.where(Task.archived == True)
        elif not include_archived:
            stmt = stmt.where(Task.archived == False)
        if status:
            parts = [s.strip() for s in status.split(",") if s.strip()]
            stmt = stmt.where(Task.status.in_(parts)) if len(parts) > 1 else stmt.where(Task.status == parts[0])
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if starred is not None:
            stmt = stmt.where(Task.starred == starred)
        if has_unread is not None:
            stmt = stmt.where(Task.has_unread == has_unread)
        if user_id is not None:
            from backend.models.worker import Worker
            from backend.models.team_share import TeamTaskShare, TeamProjectShare
            from backend.models.user_group import UserGroupMember
            owned_worker_ids_q = select(Worker.id).where(Worker.owner_user_id == user_id)
            user_group_ids_q = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
            shared_task_ids_q = select(TeamTaskShare.task_id).where(
                ((TeamTaskShare.target_type == "user") & (TeamTaskShare.target_id == user_id))
                | ((TeamTaskShare.target_type == "group") & TeamTaskShare.target_id.in_(user_group_ids_q))
            )
            shared_project_ids_q = select(TeamProjectShare.project_id).where(
                ((TeamProjectShare.target_type == "user") & (TeamProjectShare.target_id == user_id))
                | ((TeamProjectShare.target_type == "group") & TeamProjectShare.target_id.in_(user_group_ids_q))
            )
            stmt = stmt.where(
                (Task.created_by == user_id)
                | Task.worker_id.in_(owned_worker_ids_q)
                | Task.id.in_(shared_task_ids_q)
                | Task.project_id.in_(shared_project_ids_q)
            )
        result = await self.db.execute(stmt)
        return result.scalar() or 0

    async def star(self, task_id: int) -> Task | None:
        task = await self.get(task_id)
        if not task:
            return None
        task.starred = not task.starred
        auto_sort = await self._auto_sort_enabled()
        effective_key = _effective_key_expr(auto_sort)
        group_max = (
            await self.db.execute(
                select(func.max(effective_key)).where(
                    Task.archived == False,  # noqa: E712
                    Task.starred == task.starred,
                    Task.id != task_id,
                )
            )
        ).scalar()
        if group_max is not None:
            task.sort_order = group_max + 60
        else:
            task.sort_order = datetime.utcnow().timestamp()
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def archive(self, task_id: int) -> Task | None:
        task = await self.get(task_id)
        if not task:
            return None
        task.archived = not task.archived
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def update_task(self, task_id: int, **kwargs) -> Task | None:
        task = await self.get(task_id)
        if not task:
            return None
        for key, value in kwargs.items():
            if value is not None:
                setattr(task, key, value)
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def delete(self, task_id: int) -> bool:
        task = await self.get(task_id)
        if not task:
            return False
        if task.status not in ("pending", "failed", "cancelled", "conflict", "completed"):
            return False
        await self.db.execute(sa_delete(LogEntry).where(LogEntry.task_id == task_id))
        await self.db.execute(
            update(Instance)
            .where(Instance.current_task_id == task_id)
            .values(current_task_id=None)
        )
        from backend.models.monitor_session import MonitorSession, MonitorCheck
        ms_ids = (await self.db.execute(
            select(MonitorSession.id).where(MonitorSession.task_id == task_id)
        )).scalars().all()
        if ms_ids:
            await self.db.execute(
                sa_delete(MonitorCheck).where(MonitorCheck.monitor_session_id.in_(ms_ids))
            )
            await self.db.execute(
                sa_delete(MonitorSession).where(MonitorSession.task_id == task_id)
            )

        await self.db.delete(task)
        await self.db.commit()
        return True

    async def dequeue(
        self,
        exclude_ids: set[int] | None = None,
        *,
        instance_id: int | None = None,
    ) -> Task | None:
        """Atomically claim the highest-priority pending task.

        Selecting an ORM row and mutating it afterwards lets two independent
        sessions return the same task.  Ralph loops run concurrently (and may
        also overlap the global dispatcher), so the status transition itself
        must be a compare-and-swap.  A loser retries and may claim the next
        pending task instead.

        ``instance_id`` lets Ralph persist ownership in the same atomic claim;
        this leaves no cancellation window where a task is ``in_progress`` but
        has no identifiable owner.
        """

        while True:
            stmt = (
                select(Task.id)
                # worker task 不走本地 instance；shadow task (shared_from_id) 不执行
                .where(
                    Task.status == "pending",
                    Task.worker_id.is_(None),
                    Task.shared_from_id.is_(None),
                )
                .order_by(Task.priority.asc(), Task.created_at.asc())
                .limit(1)
            )
            if exclude_ids:
                stmt = stmt.where(Task.id.notin_(exclude_ids))

            candidate_id = (await self.db.execute(stmt)).scalar_one_or_none()
            if candidate_id is None:
                return None

            values = {
                "status": "in_progress",
                "started_at": datetime.utcnow(),
                "error_message": None,
            }
            if instance_id is not None:
                values["instance_id"] = instance_id

            claimed = await self.db.execute(
                update(Task)
                .where(
                    Task.id == candidate_id,
                    Task.status == "pending",
                    Task.worker_id.is_(None),
                    Task.shared_from_id.is_(None),
                )
                .values(**values)
            )
            await self.db.commit()
            if not claimed.rowcount:
                # Another dispatcher won after our candidate SELECT.  Expire a
                # potentially stale identity-map entry and try the next row.
                self.db.expire_all()
                continue

            task = await self.db.get(Task, candidate_id)
            if task is not None:
                await self.db.refresh(task)
            return task

    async def mark_status(self, task_id: int, status: str, **extra) -> None:
        """Generic status update with optional extra fields."""
        values = {"status": status, **extra}
        if status in ("completed", "failed"):
            values.setdefault("completed_at", datetime.utcnow())
        await self.db.execute(
            update(Task).where(Task.id == task_id).values(**values)
        )
        await self.db.commit()

    async def mark_completed(self, task_id: int) -> None:
        await self.db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status="completed", completed_at=datetime.utcnow(), error_message=None)
        )
        await self.db.commit()

    async def mark_failed(self, task_id: int, error: str) -> None:
        await self.db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status="failed", error_message=error, completed_at=datetime.utcnow())
        )
        await self.db.commit()

    async def defer(
        self,
        task_id: int,
        reason: str,
        *,
        instance_id: int | None = None,
    ) -> bool:
        """Return an active task to pending without consuming retry budget.

        Account routing can be temporarily unavailable before a process starts
        (for example, every Codex account is cooling down or one account is
        under login maintenance).  This is scheduling backpressure, not an
        execution failure, so ``retry_count`` must remain unchanged.

        The active-status guard is intentional: a concurrent user cancellation
        must win instead of being overwritten back to ``pending``.
        """
        predicate = [
            Task.id == task_id,
            Task.status.in_(("in_progress", "executing")),
        ]
        if instance_id is not None:
            predicate.append(Task.instance_id == instance_id)
        result = await self.db.execute(
            update(Task)
            .where(*predicate)
            .values(
                status="pending",
                instance_id=None,
                error_message=reason,
                started_at=None,
                completed_at=None,
            )
        )
        await self.db.commit()
        return bool(result.rowcount)

    async def retry(self, task_id: int) -> Task | None:
        task = await self.get(task_id)
        if not task:
            return None
        task.status = "pending"
        task.retry_count += 1
        task.error_message = None
        task.started_at = None
        task.completed_at = None
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def cancel(self, task_id: int) -> Task | None:
        task = await self.get(task_id)
        if not task or task.status not in ("pending", "in_progress", "executing", "merging"):
            return None
        task.status = "cancelled"
        task.completed_at = datetime.utcnow()

        from backend.models.monitor_session import MonitorSession
        await self.db.execute(
            update(MonitorSession)
            .where(MonitorSession.task_id == task_id, MonitorSession.status == "running")
            .values(status="cancelled", completed_at=datetime.utcnow())
        )

        await self.db.commit()
        await self.db.refresh(task)
        return task
