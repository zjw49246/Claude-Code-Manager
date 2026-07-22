from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_serializer


class WorkerAccountIn(BaseModel):
    email: str
    provider: str = "codex"
    token: str | None = None
    password: str | None = None
    login_method: str = ""  # 171mail | mailcom | onet | gazeta | "" (auto-detect)


class WorkerCreate(BaseModel):
    accounts: list[WorkerAccountIn] = Field(default_factory=list)
    # 覆盖自动命名
    name: str | None = None


class WorkerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    owner_user_id: int | None = None
    max_tasks: int = 8
    cloud_instance_id: str | None
    private_ip: str | None
    public_ip: str | None
    ssh_user: str
    ssh_key_path: str | None = None
    ccm_port: int
    ccm_commit: str | None
    accounts: list | None

    @field_serializer("accounts")
    @classmethod
    def strip_tokens(cls, v: list | None) -> list | None:
        if not v:
            return v
        return [
            {
                "email": account.get("email", ""),
                # Records created before provider-aware Worker login are Claude
                # accounts. Keep that compatibility rule in API responses too.
                "provider": account.get("provider") or "claude",
                "status": account.get("status", ""),
            }
            for account in v
        ]

    last_heartbeat: datetime | None
    bootstrap_step: str | None
    bootstrap_error: str | None
    created_at: datetime
    updated_at: datetime


class WorkerLogsResponse(BaseModel):
    id: int
    bootstrap_log: str | None
