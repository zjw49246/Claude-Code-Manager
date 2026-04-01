from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
        project_id: int | None = None, starred: bool | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[Task]:
        stmt = select(Task).order_by(Task.starred.desc(), Task.created_at.desc())
        if not include_archived:
            stmt = stmt.where(Task.archived == False)
        if status:
            stmt = stmt.where(Task.status == status)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if starred is not None:
            stmt = stmt.where(Task.starred == starred)
        stmt = stmt.limit(limit).offset(offset)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_tasks(
        self, status: str | None = None, include_archived: bool = False,
        project_id: int | None = None, starred: bool | None = None,
    ) -> int:
        stmt = select(func.count(Task.id))
        if not include_archived:
            stmt = stmt.where(Task.archived == False)
        if status:
            stmt = stmt.where(Task.status == status)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if starred is not None:
            stmt = stmt.where(Task.starred == starred)
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
        if task.status not in ("pending", "failed", "cancelled", "conflict"):
            return False
        await self.db.delete(task)
        await self.db.commit()
        return True

    async def dequeue(self, instance_model: str | None = None) -> Task | None:
        """Get the highest-priority pending task matching the instance model.

        Matching rules:
        - Tasks with no model (None) can be picked by any instance.
        - Tasks with a specific model are only picked by instances with that model.
        - Prefer model-matching tasks over unspecified tasks.
        """
        base = select(Task).where(Task.status == "pending")

        if instance_model and instance_model != "default":
            # First try exact model match, then fall back to tasks with no model specified
            stmt = (
                base
                .where((Task.model == instance_model) | (Task.model.is_(None)))
                .order_by(
                    # Prefer tasks that explicitly match this model
                    (Task.model == instance_model).desc(),
                    Task.priority.asc(),
                    Task.created_at.asc(),
                )
                .limit(1)
            )
        else:
            # Default instance: only pick tasks with no model specified
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
            .values(status="completed", completed_at=datetime.utcnow())
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
