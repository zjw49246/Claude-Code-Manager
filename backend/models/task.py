from datetime import datetime

from sqlalchemy import Integer, String, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)  # nullable for loop tasks
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_repo: Mapped[str] = mapped_column(String(500), nullable=True, default="")
    target_branch: Mapped[str] = mapped_column(String(100), default="main")
    result_branch: Mapped[str | None] = mapped_column(String(100), nullable=True)
    merge_status: Mapped[str] = mapped_column(String(20), default="pending")
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    mode: Mapped[str] = mapped_column(String(20), default="auto")  # "auto", "plan", or "loop"
    todo_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # loop only: path relative to target_repo
    loop_progress: Mapped[str | None] = mapped_column(String(200), nullable=True)  # loop only: e.g. "3/5", written by Claude
    max_iterations: Mapped[int] = mapped_column(Integer, default=50)  # loop only: max iterations before auto-abort
    plan_content: Mapped[str | None] = mapped_column(Text, nullable=True)  # Claude's proposed plan
    plan_approved: Mapped[bool | None] = mapped_column(default=None)  # None=pending, True=approved, False=rejected
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_cwd: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    context_window_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    starred: Mapped[bool] = mapped_column(default=False, server_default="0", index=True)
    archived: Mapped[bool] = mapped_column(default=False, server_default="0", index=True)
    has_unread: Mapped[bool] = mapped_column(default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
