from pydantic import BaseModel


class QuickPhraseCreate(BaseModel):
    label: str
    content: str
    sort_order: int = 0


class QuickPhraseUpdate(BaseModel):
    label: str | None = None
    content: str | None = None
    sort_order: int | None = None
