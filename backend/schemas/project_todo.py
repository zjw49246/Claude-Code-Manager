from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ProjectTodoStatus = Literal["open", "done", "archived"]


class ProjectTodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1)


class ProjectTodoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1)
    status: ProjectTodoStatus | None = None
    sort_order: int | None = None
    created_task_id: int | None = None


class ProjectTodoResponse(BaseModel):
    id: int
    project_id: int
    title: str
    prompt: str
    status: ProjectTodoStatus
    sort_order: int
    created_task_id: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
