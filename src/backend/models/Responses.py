from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from base_models import UserBase
from fastapi.responses import JSONResponse


class UserExistsPostResponse(BaseModel):
    exists: bool = Field(..., description="Whether the user exists")


class CreateUserPostResponse(BaseModel):
    message: str = Field(
        ...,
        example="User created successfully. Please check your email for verification.",
    )
    user_token: str = Field(..., description="user token for authentication")
    session_token: Optional[str] = Field(
        None, description="Session token for authentication"
    )


class UserAuthenticatePostResponse(BaseModel):
    user_token: str = Field(..., description="user token for authentication")
    session_token: Optional[str] = Field(
        None, description="Session token for authentication"
    )
    user: UserBase = Field(..., description="User details")  # Uncomment if needed


class ErrorResponse(BaseModel):
    error_message: str


class JsonResponseWithStatus(JSONResponse):
    def __init__(self, content: BaseModel, status_code: int):
        # Convert the Pydantic model to a dict
        content_dict = content.dict() if isinstance(content, BaseModel) else content
        super().__init__(content=content_dict, status_code=status_code)
