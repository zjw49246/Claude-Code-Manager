from pydantic import BaseModel


class DiscussionCreate(BaseModel):
    title: str
    project_id: int | None = None
    max_agents: int = 5
    facilitator_model: str = "claude-opus-4-6"
    agent_model: str = "claude-opus-4-6"


class DiscussionMessageOut(BaseModel):
    id: int
    discussion_id: int
    role: str
    agent_role_name: str | None
    content: str
    created_at: str

    model_config = {"from_attributes": True}


class DiscussionOut(BaseModel):
    id: int
    title: str
    project_id: int | None
    max_agents: int
    facilitator_model: str
    agent_model: str
    status: str
    created_at: str
    messages: list[DiscussionMessageOut] = []

    model_config = {"from_attributes": True}


class DiscussionSendMessage(BaseModel):
    message: str
