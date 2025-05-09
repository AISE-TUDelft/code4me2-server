from abc import ABC
from typing import Optional
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from base_models import UserBase


class MessageResponse(BaseModel, ABC):
    message: str = Field(..., description="Response message")


class ErrorResponse(MessageResponse):
    message: str = Field(..., description="Error message")


class JsonResponseWithStatus(JSONResponse):
    def __init__(self, content: BaseModel, status_code: int):
        # Convert the Pydantic model to a dict
        super().__init__(content=jsonable_encoder(content), status_code=status_code)


# api/user/create
class CreateUserPostResponse(MessageResponse):
    message: str = Field(
        default="User created successfully. Please check your email for verification."
    )
    user_id: UUID = Field(..., description="Created user id")


class UserAlreadyExistsWithThisEmail(ErrorResponse):
    message: str = Field(default="User already exists with this email!")


class InvalidOrExpiredToken(ErrorResponse):
    message: str = Field(default="Invalid or expired token!")


# api/user/authenticate
class AuthenticateUserPostResponse(MessageResponse, ABC):
    user_id: UUID = Field(..., description="User id for authentication")
    session_token: Optional[UUID] = Field(
        None, description="Session token for authentication"
    )
    user: UserBase = Field(..., description="User details")  # Uncomment if needed


class AuthenticateUserNormalPostResponse(AuthenticateUserPostResponse):
    message: str = Field(
        default="User authenticated successfully via email and password."
    )


class AuthenticateUserOAuthPostResponse(AuthenticateUserPostResponse):
    message: str = Field(default="User authenticated successfully via OAuth.")


class InvalidEmailOrPassword(ErrorResponse):
    message: str = Field(default="Invalid email or password!")


# New response models for completions
# TODO Decide to remain here or in elsewhere
class CompletionItem(BaseModel):
    model_id: int = Field(..., description="Model ID")
    model_name: str = Field(..., description="Model name")
    completion: str = Field(..., description="Generated code")
    confidence: float = Field(..., description="Confidence score")


class CompletionResponseData(BaseModel):
    query_id: UUID = Field(..., description="Query ID")
    completions: list[CompletionItem] = Field(..., description="Generated completions")


class CompletionResponse(MessageResponse):
    data: CompletionResponseData = Field(..., description="Completion data")


class FeedbackResponseData(BaseModel):
    query_id: UUID = Field(..., description="Query ID")
    model_id: int = Field(..., description="Model ID")


class FeedbackResponse(MessageResponse):
    data: FeedbackResponseData = Field(..., description="Feedback data")
