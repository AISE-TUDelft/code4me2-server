from abc import ABC
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from base_models import (
    CompletionResponseData,
    FeedbackResponseData,
    SerializableBaseModel,
    UserBase,
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

class UserNotFoundError(ErrorResponse):
    message: str = Field(default="User not found")

class GenerateCompletionsError(ErrorResponse):
    def __init__(self, error_message: str):
        super().__init__(message=f"Failed to generate completions: {error_message}")

# /api/completion/feedback
class CompletionFeedbackPostResponse(BaseResponse):
    message: str = Field(default="Feedback recorded successfully.")
    data: FeedbackResponseData = Field(
        ..., description="Information about the recorded feedback"
    )

# TODO Consider sending a more generic response after development phase
class FeedbackRecordingError(ErrorResponse):
    def __init__(self, error_message: str):
        super().__init__(message=f"Failed to record feedback: {error_message}")

class GenerationNotFoundError(ErrorResponse):
    message: str = Field(default="Generation record not found")

class QueryNotFoundError(ErrorResponse):
    message: str = Field(default="Query not found")

class CompletionsNotFoundError(ErrorResponse):
    message: str = Field(default="No completions found for this query")

# TODO Consider sending a more generic response after development phase
class RetrieveCompletionsError(ErrorResponse):
    def __init__(self, error_message: str):
        super().__init__(message=f"Failed to retrieve completions: {error_message}")

