"""Skill evolution lessons — learned from tool failures and distill."""

from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class SkillLesson(Base):
    __tablename__ = "skill_lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_name: Mapped[str] = mapped_column(String(100), index=True)
    lesson: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50), default="evolution")
    tool_name: Mapped[str | None] = mapped_column(String(100))
    worker_id: Mapped[int | None] = mapped_column(Integer)
    lesson_hash: Mapped[str | None] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class SkillUsage(Base):
    __tablename__ = "skill_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_name: Mapped[str] = mapped_column(String(100), index=True)
    trigger_type: Mapped[str] = mapped_column(String(50))
    task_id: Mapped[int | None] = mapped_column(Integer)
    project_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
