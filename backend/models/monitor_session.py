from datetime import datetime

from sqlalchemy import Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class MonitorSession(Base):
    __tablename__ = "monitor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    monitor_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval: Mapped[int] = mapped_column(Integer, default=300)
    max_checks: Mapped[int] = mapped_column(Integer, default=100)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    checks_done: Mapped[int] = mapped_column(Integer, default=0)
    last_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MonitorCheck(Base):
    __tablename__ = "monitor_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_session_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    check_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
