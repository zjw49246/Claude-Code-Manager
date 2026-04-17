from datetime import datetime

from sqlalchemy import Integer, String, Float, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")
    current_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    worktree_branch: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str] = mapped_column(String(50), default="default")
    effort_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Optional Extended Thinking budget (max tokens). Forwarded to Claude Code
    # subprocess via MAX_THINKING_TOKENS env var. NULL = use CLI default.
    thinking_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
