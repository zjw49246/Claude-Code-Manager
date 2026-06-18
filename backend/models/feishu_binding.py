"""Feishu user binding — links this CCM instance to a Feishu identity."""

from datetime import datetime

from sqlalchemy import String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class FeishuUserBinding(Base):
    __tablename__ = "feishu_user_binding"

    id: Mapped[int] = mapped_column(primary_key=True)
    feishu_open_id: Mapped[str] = mapped_column(String(100), unique=True)
    feishu_name: Mapped[str | None] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    access_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
