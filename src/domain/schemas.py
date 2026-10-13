from uuid import UUID

from pydantic import BaseModel


class UserSchema(BaseModel):
    id: UUID
    email: str


class TokenSchema(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
