from datetime import datetime

from pydantic import BaseModel, model_validator


class TaskCreate(BaseModel):
    title: str = ""
    description: str = ""
    project_id: int | None = None
    target_repo: str | None = None
    target_branch: str = "main"
    priority: int = 0
    max_retries: int = 2
    mode: str = "auto"  # "auto", "plan", or "loop"
    todo_file_path: str | None = None  # required when mode="loop"
    max_iterations: int = 50  # loop only: max iterations before auto-abort
    model: str | None = None
    tags: list[str] | None = None
    image_paths: list[str] | None = None  # absolute paths of uploaded images
    secret_ids: list[int] | None = None  # IDs of secrets to inject into prompt

    @model_validator(mode='after')
    def validate_mode_fields(self):
        if self.mode != 'loop' and not self.description:
            raise ValueError('description is required for non-loop tasks')
        if self.mode == 'loop' and not self.todo_file_path:
            raise ValueError('todo_file_path is required for loop tasks')
        return self


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    project_id: int | None = None
    target_repo: str | None = None
    target_branch: str | None = None
    max_retries: int | None = None
    max_iterations: int | None = None
    mode: str | None = None
    starred: bool | None = None
    tags: list[str] | None = None


class TaskResponse(BaseModel):
    id: int
    title: str
    description: str | None
    status: str
    priority: int
    project_id: int | None
    target_repo: str | None
    target_branch: str
    result_branch: str | None
    merge_status: str
    instance_id: int | None
    retry_count: int
    max_retries: int
    mode: str
    todo_file_path: str | None
    loop_progress: str | None
    max_iterations: int
    plan_content: str | None
    plan_approved: bool | None
    session_id: str | None
    model: str | None
    starred: bool
    archived: bool
    has_unread: bool
    error_message: str | None
    tags: list[str] | None
    context_window_usage: dict | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}
