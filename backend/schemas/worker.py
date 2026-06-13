from datetime import datetime
from pydantic import BaseModel, ConfigDict


class WorkerAccountIn(BaseModel):
    email: str
    token: str | None = None     # 171mail 接码 token 或 mail.com 邮箱密码（按后缀自动判断）


class WorkerCreate(BaseModel):
    accounts: list[WorkerAccountIn] = []
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
