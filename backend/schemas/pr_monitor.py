from datetime import datetime
from pydantic import BaseModel, field_validator


class MonitoredRepoCreate(BaseModel):
    repo_full_name: str
    project_id: int | None = None
    auto_merge: bool = False
    review_model: str | None = None
    default_branch: str = "main"
    allowed_authors: list[str] = []

    @field_validator("repo_full_name")
    @classmethod
    def validate_repo_name(cls, v: str) -> str:
        if "/" not in v or len(v.split("/")) != 2:
            raise ValueError("repo_full_name must be in 'owner/repo' format")
        return v


class MonitoredRepoUpdate(BaseModel):
    project_id: int | None = None
    auto_merge: bool | None = None
    review_model: str | None = None
    default_branch: str | None = None
    allowed_authors: list[str] | None = None
    enabled: bool | None = None


class MonitoredRepoResponse(BaseModel):
    id: int
    repo_full_name: str
    project_id: int | None
    enabled: bool
    auto_merge: bool
    webhook_secret: str
    review_model: str | None
    default_branch: str
    allowed_authors: list[str]
    status: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("webhook_secret", mode="before")
    @classmethod
    def mask_secret(cls, v: str) -> str:
        if v and len(v) > 4:
            return v[:4] + "***"
        return "***"

    @field_validator("allowed_authors", mode="before")
    @classmethod
    def ensure_list(cls, v) -> list[str]:
        if v is None:
            return []
        return v


class MonitoredRepoDetailResponse(MonitoredRepoResponse):
    """Full detail response — shows unmasked webhook_secret."""

    @field_validator("webhook_secret", mode="before")
    @classmethod
    def no_mask(cls, v: str) -> str:
        return v


class PRReviewResponse(BaseModel):
    id: int
    repo_id: int
    pr_number: int
    pr_title: str
    pr_author: str
    pr_url: str
    task_id: int | None
    status: str
    review_summary: str | None
    action_taken: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}
