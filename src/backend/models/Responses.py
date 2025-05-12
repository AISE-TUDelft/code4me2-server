from abc import ABC
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


from base_models import (
    UserBase,
    SerializableBaseModel,
    CompletionResponseData,
    FeedbackResponseData,
)


class BaseResponse(SerializableBaseModel, ABC):
    message: str = Field(..., description="Response message")


class ErrorResponse(BaseResponse, ABC):
    message: str = Field(..., description="Error message")


class JsonResponseWithStatus(JSONResponse):
    def __init__(self, content: BaseModel, status_code: int):
        # Convert the Pydantic model to a dict
        super().__init__(content=jsonable_encoder(content), status_code=status_code)


# api/user/create
class CreateUserPostResponse(BaseResponse):
    message: str = Field(
        default="User created successfully. Please check your email for verification."
    )
    user_id: UUID = Field(..., description="Created user id")


class UserAlreadyExistsWithThisEmail(ErrorResponse):
    message: str = Field(default="User already exists with this email!")


class InvalidOrExpiredToken(ErrorResponse):
    message: str = Field(default="Invalid or expired token!")


# api/user/authenticate
class AuthenticateUserPostResponse(BaseResponse, ABC):
    user: UserBase = Field(..., description="User details")  # Uncomment if needed


class AuthenticateUserNormalPostResponse(AuthenticateUserPostResponse):
    message: str = Field(
        default="User authenticated successfully via email and password."
    )


class AuthenticateUserOAuthPostResponse(AuthenticateUserPostResponse):
    message: str = Field(default="User authenticated successfully via OAuth.")


class InvalidEmailOrPassword(ErrorResponse):
    message: str = Field(default="Invalid email or password!")


# /api/user/delete
class DeleteUserDeleteResponse(BaseResponse):
    message: str = Field(default="User is deleted successfully.")


class InvalidSessionToken(ErrorResponse):
    message: str = Field(
        default="Session not found! You are not authenticated or your session has expired. "
        "Login before you can perform this action."
    )


# /api/user/update
class UpdateUserPutResponse(BaseResponse):
    message: str = Field(default="User is updated successfully.")
    user: UserBase = Field(..., description="User details")  # Uncomment if needed


# /api/completion/request
class CompletionPostResponse(BaseResponse):
    message: str = Field(
        default="Completions generated successfully. Ready for display in your IDE."
    )
    data: CompletionResponseData = Field(
        ..., description="Generated code completions including query ID and suggestions"
    )


# /api/completion/feedback
class CompletionFeedbackPostResponse(BaseResponse):
    message: str = Field(default="Feedback recorded successfully.")
    data: FeedbackResponseData = Field(
        ..., description="Information about the recorded feedback"
    )
