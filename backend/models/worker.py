from datetime import datetime
from sqlalchemy import Boolean, Integer, String, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


class Worker(Base):
    """分布式 Worker：一台跑完整 CCM 的 EC2，由 Manager 全生命周期管理。

    设计文档见 docs/plans/elastic-worker-design.md。
    status 状态机:
      creating → bootstrapping → ready ⇄ (stopping → stopped → starting)
      ready ⇄ error（健康检查自动降级/恢复）
      任意 → destroying → terminated
    """

    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)  # "{manager主机名}-worker-{id}"
    status: Mapped[str] = mapped_column(String(20), default="creating", server_default="creating")

    # 云实例信息
    cloud_instance_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    private_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # 主通信地址（VPC 内网）
    public_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # 仅记录，不用于通信
    adopted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # 收养的已有实例（destroy 时只 stop 不 terminate）

    # 连接信息
    ssh_user: Mapped[str] = mapped_column(String(50), default="ubuntu", server_default="ubuntu")
    ssh_key_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ccm_port: Mapped[int] = mapped_column(Integer, default=8000, server_default="8000")
    auth_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ccm_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 版本锁定校验

    # 账号信息（在 Worker 本机登录；密码经 secrets 机制加密后只存引用）
    accounts: Mapped[list | None] = mapped_column(JSON, default=list)  # [{"email", "status": pending/logged_in/failed}]

    # Project ID 映射（manager_project_id → worker_project_id）
    project_mapping: Mapped[dict | None] = mapped_column(JSON, default=dict)

    # 健康监控 / bootstrap 进度
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bootstrap_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bootstrap_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    bootstrap_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
