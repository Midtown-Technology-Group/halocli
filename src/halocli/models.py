from __future__ import annotations

from pydantic import BaseModel, Field


class ListResult(BaseModel):
    resource: str
    count: int
    items: list[dict]


class RawResult(BaseModel):
    ok: bool
    status_code: int
    body: object = None
    category: str | None = None
    diagnostic: str = ""


class TokenPayload(BaseModel):
    access_token: str
    expires_in: int = Field(default=3600)
    refresh_token: str | None = None
    token_type: str = "Bearer"
    scope: str | None = None
