"""Generic sub-agent session models.

子 agent 是分类别的一等概念：`agent_type` 区分类别，`source` 区分由谁启动。

- agent_type="monitor", source="ccm"        — $monitor 命令启动的 CCM 监控子 agent
- agent_type="native-agent", source="native" — 模型自己用 Agent/Task 工具开的子 agent
- agent_type="native-monitor", source="native" — 模型用内置 Monitor 工具挂的后台监视器
- 将来: researcher / builder / ... 复用同两张表

历史：表由 monitor_sessions / monitor_checks 重命名而来（迁移 a9c2e1f0b3d4），
旧名通过 backend.models.monitor_session 的别名保持兼容。
"""

from datetime import datetime
from sqlalchemy import Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, synonym
from backend.database import Base


class SubAgentSession(Base):
    __tablename__ = "sub_agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    # 类别（monitor / native-agent / native-monitor / ...）
    agent_type: Mapped[str] = mapped_column(String(50), default="monitor")
    # 启动方：ccm（$命令经 CCM API 启动）| native（模型在 session 内自己开）
    source: Mapped[str] = mapped_column(String(20), default="ccm")
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    monitor_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 类别专属配置（monitor: 检查节奏；native-*: 无意义，保留默认值）
    interval: Mapped[int] = mapped_column(Integer, default=120)
    max_checks: Mapped[int] = mapped_column(Integer, default=50)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    checks_done: Mapped[int] = mapped_column(Integer, default=0)
    last_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 类别专属元数据 JSON（native-*: tool_use_id / agent_id / background ...）
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SubAgentReport(Base):
    __tablename__ = "sub_agent_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    check_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 兼容旧字段名（monitor_checks.monitor_session_id 时代的调用点）
    monitor_session_id = synonym("session_id")
