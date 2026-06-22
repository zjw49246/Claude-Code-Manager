from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


def _utcnow() -> datetime:
    """Naive UTC now. Avoids the deprecated datetime.utcnow() while staying naive
    like every other model's timestamps (so cross-model comparisons don't hit the
    aware/naive TypeError)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProjectTodo(Base):
    __tablename__ = "project_todos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Provenance link: the task this todo spawned (via "Run"). Soft link (no FK
    # constraint — SQLite doesn't enforce FKs here; see database.py). Enables a
    # future "task completed → mark todo done" sync by looking up created_task_id.
    created_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
