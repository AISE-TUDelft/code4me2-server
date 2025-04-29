from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from src.backend.models.Bodies import User


class UserExistsPostResponse(BaseModel):
    exists: Optional[bool] = Field(None, description="Whether the user exists")


class UserNewPostResponse(BaseModel):
    message: Optional[str] = Field(
        None,
        example="User created successfully. Please check your email for verification.",
    )
    userId: Optional[UUID] = Field(None, example="123e4567-e89b-12d3-a456-426614174000")
    sessionToken: Optional[str] = Field(
        None, description="Session token for authentication"
    )


class UserAuthenticatePostResponse(BaseModel):
    token: Optional[str] = Field(None, description="JWT token for authentication")
    sessionToken: Optional[str] = Field(
        None, description="Session token for authentication"
    )
    user: Optional[User] = None


class ErrorResponse(BaseModel):
    error: Optional[str] = Field(None, description="ErrorResponse message")
