from datetime import datetime
from pydantic import BaseModel


class MonitorSessionCreate(BaseModel):
    description: str
    monitor_context: str | None = None
    interval: int = 120
    max_checks: int = 50
    model: str | None = None


class MonitorSessionResponse(BaseModel):
    id: int
    task_id: int
    description: str
    monitor_context: str | None
    interval: int
    max_checks: int
    model: str | None
    status: str
    checks_done: int
    last_summary: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class MonitorCheckCreate(BaseModel):
    summary: str
    status: str = "success"
    is_important: bool = False


class MonitorCompleteRequest(BaseModel):
    reason: str


class MonitorCheckResponse(BaseModel):
    id: int
    monitor_session_id: int
    check_number: int
    status: str
    summary: str | None
    full_output: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
