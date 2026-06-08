from datetime import datetime

from pydantic import BaseModel


class InstanceCreate(BaseModel):
    name: str
    config: dict | None = None


class InstanceResponse(BaseModel):
    id: int
    name: str
    pid: int | None
    status: str
    current_task_id: int | None
    worktree_path: str | None
    worktree_branch: str | None
    provider: str
    model: str
    effort_level: str | None
    thinking_budget: int | None
    total_tasks_completed: int
    total_cost_usd: float
    config: dict | None
    started_at: datetime | None
    last_heartbeat: datetime | None

    model_config = {"from_attributes": True}
