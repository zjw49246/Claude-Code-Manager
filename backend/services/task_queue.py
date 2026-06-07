from datetime import datetime

from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models.instance import Instance
from backend.models.log_entry import LogEntry
from backend.models.task import Task


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

    async def list_tasks(
        self, status: str | None = None, include_archived: bool = False,
        archived_only: bool = False,
        project_id: int | None = None, starred: bool | None = None,
        has_unread: bool | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[Task]:
        stmt = select(Task).order_by(Task.starred.desc(), Task.created_at.desc())
        if archived_only:
            stmt = stmt.where(Task.archived == True)
        elif not include_archived:
            stmt = stmt.where(Task.archived == False)
        if status:
            stmt = stmt.where(Task.status == status)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if starred is not None:
            stmt = stmt.where(Task.starred == starred)
        if has_unread is not None:
            stmt = stmt.where(Task.has_unread == has_unread)
        stmt = stmt.limit(limit).offset(offset)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_tasks(
        self, status: str | None = None, include_archived: bool = False,
        archived_only: bool = False,
        project_id: int | None = None, starred: bool | None = None,
        has_unread: bool | None = None,
    ) -> int:
        stmt = select(func.count(Task.id))
        if archived_only:
            stmt = stmt.where(Task.archived == True)
        elif not include_archived:
            stmt = stmt.where(Task.archived == False)
        if status:
            stmt = stmt.where(Task.status == status)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if starred is not None:
            stmt = stmt.where(Task.starred == starred)
        if has_unread is not None:
            stmt = stmt.where(Task.has_unread == has_unread)
        result = await self.db.execute(stmt)
        return result.scalar() or 0

    async def star(self, task_id: int) -> Task | None:
        task = await self.get(task_id)
        if not task:
            return None
        task.starred = not task.starred
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
        await self.db.delete(task)
        await self.db.commit()
        return True

    async def dequeue(self, instance_model: str | None = None, instance_provider: str | None = "claude") -> Task | None:
        """Get the highest-priority pending task matching instance provider/model.

        Matching rules:
        - Tasks are first constrained by provider.
        - Tasks with no model (None) can be picked by any instance.
        - Tasks with a specific model are only picked by instances with that model.
        - Prefer model-matching tasks over unspecified tasks.
        - "default" is treated as equivalent to the configured default_model.
        """
        effective_provider = instance_provider or settings.default_provider or "claude"
        base = select(Task).where(
            Task.status == "pending",
            Task.provider == effective_provider,
        )

        # Normalize "default" to the actual default model name
        effective_model = instance_model
        if effective_model == "default":
            effective_model = (
                settings.default_codex_model
                if effective_provider == "codex"
                else settings.default_model
            )
        default_model = (
            settings.default_codex_model
            if effective_provider == "codex"
            else settings.default_model
        )

        if effective_model:
            # First try exact model match, then fall back to tasks with no model specified
            # Also match tasks with "default" if this instance uses the default model
            model_match = Task.model == effective_model
            if effective_model == default_model:
                model_match = (Task.model == effective_model) | (Task.model == "default")
            stmt = (
                base
                .where(model_match | (Task.model.is_(None)))
                .order_by(
                    # Prefer tasks that explicitly match this model
                    model_match.desc(),
                    Task.priority.asc(),
                    Task.created_at.asc(),
                )
                .limit(1)
            )
        else:
            # No model instance: only pick tasks with no model specified
            stmt = (
                base
                .where(Task.model.is_(None))
                .order_by(Task.priority.asc(), Task.created_at.asc())
                .limit(1)
            )

        result = await self.db.execute(stmt)
        task = result.scalar_one_or_none()
        if task:
            task.status = "in_progress"
            task.started_at = datetime.utcnow()
            task.error_message = None
            await self.db.commit()
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
        await self.db.commit()
        await self.db.refresh(task)
        return task
