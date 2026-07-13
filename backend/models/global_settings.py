from sqlalchemy import Boolean, Float, Integer, JSON, String, Text
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
    # Context compaction threshold (0-1); None = follow env default
    # (settings.context_compact_threshold)
    context_compact_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Org registry URL override (set via registry-changed callback, takes precedence over env)
    org_registry_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Default skills/plugins selection for new tasks
    default_enabled_plugins: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    default_enabled_user_skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # CC settings template (JSON string) synced to all pool account config dirs
    cc_settings_template: Mapped[str | None] = mapped_column(Text, nullable=True)
