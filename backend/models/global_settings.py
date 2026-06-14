from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class GlobalSettings(Base):
    """Singleton table (id=1) for global fallback git configuration."""
    __tablename__ = "global_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Git identity (commit author)
    git_author_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    git_author_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Git credentials
    git_credential_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "ssh" | "https" | None
    git_ssh_key_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    git_https_username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    git_https_token: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Runtime mode switches (None = follow env default)
    use_pty_mode: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Task sort: auto-move accessed task to top of its group (None = True)
    auto_sort_on_access: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
