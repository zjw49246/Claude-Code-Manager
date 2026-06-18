"""Organization registry models — members and team groups."""

from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class OrgMember(Base):
    __tablename__ = "org_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    feishu_open_id: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    ccm_url: Mapped[str] = mapped_column(String(500))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class OrgTeam(Base):
    __tablename__ = "org_teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class OrgTeamMember(Base):
    __tablename__ = "org_team_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("org_teams.id", ondelete="CASCADE")
    )
    feishu_open_id: Mapped[str] = mapped_column(String(100))

    __table_args__ = (
        {"sqlite_autoincrement": False},
    )
