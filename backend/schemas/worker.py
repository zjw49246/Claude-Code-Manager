from datetime import datetime
from pydantic import BaseModel, ConfigDict


class WorkerAccountIn(BaseModel):
    email: str
    password: str | None = None


class WorkerCreate(BaseModel):
    accounts: list[WorkerAccountIn] = []
    # 收养已有 EC2 实例（不新开机器；destroy 时只 stop 不 terminate）
    adopt_instance_id: str | None = None
    # 覆盖自动命名
    name: str | None = None


class WorkerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    cloud_instance_id: str | None
    private_ip: str | None
    public_ip: str | None
    adopted: bool
    ssh_user: str
    ccm_port: int
    ccm_commit: str | None
    accounts: list | None
    last_heartbeat: datetime | None
    bootstrap_step: str | None
    bootstrap_error: str | None
    created_at: datetime
    updated_at: datetime


class WorkerLogsResponse(BaseModel):
    id: int
    bootstrap_log: str | None
