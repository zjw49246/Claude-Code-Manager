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
    mode: str = "auto"  # "auto", "plan", "loop", or "goal"
    todo_file_path: str | None = None  # required when mode="loop"
    max_iterations: int = 50  # loop only: max iterations before auto-abort
    must_complete: bool = False  # loop only: reject done until all items finished
    goal_condition: str | None = None  # goal only: natural-language completion condition
    goal_max_turns: int = 30  # goal only: max turns before auto-fail
    goal_evaluator_model: str | None = None  # goal only: evaluator model (default haiku)
    provider: str = "claude"
    model: str | None = None
    effort_level: str | None = None
    thinking_budget: int | None = None
    enable_workflows: bool = False
    tags: list[str] | None = None
    image_paths: list[str] | None = None  # kept for backwards compat
    file_paths: list[str] | None = None
    attachments: list[dict] | None = None  # [{url, name, is_image}, ...]
    secret_ids: list[int] | None = None
    clone_from_task_id: int | None = None
    starred: bool = False

    @model_validator(mode='after')
    def validate_mode_fields(self):
        if self.mode not in ('loop',) and not self.description:
            raise ValueError('description is required for non-loop tasks')
        if self.mode == 'loop' and not self.todo_file_path:
            raise ValueError('todo_file_path is required for loop tasks')
        if self.mode == 'goal' and not self.goal_condition:
            raise ValueError('goal_condition is required for goal tasks')
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
    must_complete: bool | None = None
    mode: str | None = None
    goal_condition: str | None = None
    goal_max_turns: int | None = None
    goal_evaluator_model: str | None = None
    enable_workflows: bool | None = None
    provider: str | None = None
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
    must_complete: bool
    goal_condition: str | None
    goal_evaluator_model: str | None
    goal_max_turns: int
    goal_turns_used: int
    goal_last_reason: str | None
    plan_content: str | None
    plan_approved: bool | None
    session_id: str | None
    provider: str
    model: str | None
    effort_level: str | None
    thinking_budget: int | None
    enable_workflows: bool
    starred: bool
    archived: bool
    has_unread: bool
    error_message: str | None
    tags: list[str] | None
    metadata_: dict | None = None
    context_window_usage: dict | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}
