from datetime import datetime
from pydantic import BaseModel, field_validator


class MonitorSessionCreate(BaseModel):
    description: str
    monitor_context: str | None = None
    interval: int = 120
    max_checks: int = 50
    model: str | None = None

    @field_validator("interval")
    @classmethod
    def interval_must_be_positive(cls, v: int) -> int:
        if v < 5:
            raise ValueError("interval must be at least 5 seconds")
        return v

    @field_validator("max_checks")
    @classmethod
    def max_checks_must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_checks must be at least 1")
        return v


class MonitorSessionResponse(BaseModel):
    id: int
    task_id: int
    agent_type: str = "monitor"
    source: str = "ccm"
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
