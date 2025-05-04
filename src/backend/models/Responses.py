from typing import Optional

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from base_models import UserBase


class MessageResponse(BaseModel):
    message: str = Field(..., description="Response message")


class UserExistsPostResponse(MessageResponse):
    exists: bool = Field(..., description="Whether the user exists")


class CreateUserPostResponse(MessageResponse):
    user_token: str = Field(..., description="user token for authentication")
    session_token: Optional[str] = Field(
        None, description="Session token for authentication"
    )


class UserAuthenticatePostResponse(MessageResponse):
    user_token: str = Field(..., description="user token for authentication")
    session_token: Optional[str] = Field(
        None, description="Session token for authentication"
    )
    user: UserBase = Field(..., description="User details")  # Uncomment if needed


class ErrorResponse(MessageResponse):
    pass


class JsonResponseWithStatus(JSONResponse):
    def __init__(self, content: BaseModel, status_code: int):
        # Convert the Pydantic model to a dict
        super().__init__(content=jsonable_encoder(content), status_code=status_code)
