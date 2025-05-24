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


class InvalidOrExpiredJWTToken(ErrorResponse):
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


class InvalidOrExpiredSessionToken(ErrorResponse):
    message: str = Field(
        default="Session not found! You are not authenticated or your session has expired. "
        "Login before you can perform this action."
    )


class InvalidOrExpiredAuthToken(ErrorResponse):
    message: str = Field(
        default="Authentication token not found! You are not authenticated or your token has expired. "
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
    message: str = Field(default="Failed to generate completions.")


# /api/completion/feedback
class CompletionFeedbackPostResponse(BaseResponse):
    message: str = Field(default="Feedback recorded successfully.")
    data: FeedbackResponseData = Field(
        ..., description="Information about the recorded feedback"
    )


class FeedbackRecordingError(ErrorResponse):
    message: str = Field(default="Failed to record feedback.")


class GenerationNotFoundError(ErrorResponse):
    message: str = Field(default="Generation record not found")


class QueryNotFoundError(ErrorResponse):
    message: str = Field(default="Query not found")


class CompletionsNotFoundError(ErrorResponse):
    message: str = Field(default="No completions found for this query")


class RetrieveCompletionsError(ErrorResponse):
    message: str = Field(default="Failed to retrieve completions.")


# /api/completion/multi-file-context/update
class MultiFileContextUpdatePostResponse(BaseResponse):
    message: str = Field(default="Multi-file context updated successfully.")
    data: dict = Field(..., description="Data related to the multi-file context update")


class MultiFileContextUpdateError(ErrorResponse):
    message: str = Field(default="Failed to update multi-file context.")


# /api/session/create
class CreateSessionPostResponse(BaseResponse):
    message: str = Field(default="Session created successfully.")
    session_token: UUID = Field(..., description="Created session token")


class CreateSessionError(ErrorResponse):
    message: str = Field(
        default="Failed to create session. User not found or invalid data."
    )


# /api/session/activate
class ActivateSessionPostResponse(BaseResponse):
    message: str = Field(default="Session activated successfully.")


class ActivateSessionError(ErrorResponse):
    message: str = Field(
        default="Failed to activate session. Session not found or expired."
    )


# /api/session/deactivate
class DeactivateSessionPostResponse(BaseResponse):
    message: str = Field(default="Session deactivated successfully.")


class DeactivateSessionError(ErrorResponse):
    message: str = Field(default="Failed to deactivate session.")
