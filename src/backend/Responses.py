from abc import ABC
from typing import Any, Dict
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from response_models import (
    ResponseCompletionResponseData,
    ResponseFeedbackResponseData,
    ResponseUser,
    SerializableBaseModel,
)


class BaseResponse(SerializableBaseModel, ABC):
    message: str = Field(..., description="Response message")


class ErrorResponse(BaseResponse, ABC):
    message: str = Field(..., description="Error message")


class JsonResponseWithStatus(JSONResponse):
    def __init__(self, content: BaseModel, status_code: int):
        self.content = content
        self.status_code = status_code
        # Convert the Pydantic model to a dict
        super().__init__(content=jsonable_encoder(content), status_code=status_code)

    def dict(self) -> dict:
        """
        Convert the response content to a dictionary.
        """
        return {
            "status_code": self.status_code,
            "content": jsonable_encoder(self.content),
        }


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


class CreateUserError(ErrorResponse):
    message: str = Field(default="Server failed to create a new user!")


# api/user/authenticate
class AuthenticateUserPostResponse(BaseResponse, ABC):
    user: ResponseUser = Field(..., description="User details")  # Uncomment if needed
    config: Dict[str, Any] = Field(..., description="Users config json string")


class AuthenticateUserNormalPostResponse(AuthenticateUserPostResponse):
    message: str = Field(
        default="User authenticated successfully via email and password."
    )


class AuthenticateUserOAuthPostResponse(AuthenticateUserPostResponse):
    message: str = Field(default="User authenticated successfully via OAuth.")


class InvalidEmailOrPassword(ErrorResponse):
    message: str = Field(default="Invalid email or password!")


class AuthenticateUserError(ErrorResponse):
    message: str = Field(default="Server failed to authenticate the user!")


class ConfigNotFound(ErrorResponse):
    message: str = Field(default="Config not found!")


# /api/user/delete
class DeleteUserDeleteResponse(BaseResponse):
    message: str = Field(default="User is deleted successfully.")


class DeleteUserError(ErrorResponse):
    message: str = Field(default="Server failed to delete the user!")


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
    user: ResponseUser = Field(..., description="User details")  # Uncomment if needed


class InvalidPreviousPassword(BaseResponse):
    message: str = Field(default="Previous password is not correct!")


class UpdateUserError(ErrorResponse):
    message: str = Field(default="Server failed to update the user!")


# /api/completion/request
class CompletionPostResponse(BaseResponse):
    message: str = Field(
        default="Completions generated successfully. Ready for display in your IDE."
    )
    data: ResponseCompletionResponseData = Field(
        ..., description="Generated code completions including query ID and suggestions"
    )


class UserNotFoundError(ErrorResponse):
    message: str = Field(default="User not found")


class GenerateCompletionsError(ErrorResponse):
    message: str = Field(default="Failed to generate completions.")


# /api/completion/feedback
class CompletionFeedbackPostResponse(BaseResponse):
    message: str = Field(default="Feedback recorded successfully.")
    data: ResponseFeedbackResponseData = Field(
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


# /api/chat/request
class GenerateChatCompletionsError(ErrorResponse):
    message: str = Field(default="Failed to generate chat completions.")


# /api/chat/get
class RetrieveChatCompletionsError(ErrorResponse):
    message: str = Field(default="Failed to retrieve chat completions.")


# /api/completion/multi-file-context/update
class MultiFileContextUpdatePostResponse(BaseResponse):
    message: str = Field(default="Multi-file context updated successfully.")
    data: dict = Field(..., description="Data related to the multi-file context update")


class MultiFileContextUpdateError(ErrorResponse):
    message: str = Field(default="Failed to update multi-file context.")


# /api/project/create
class CreateProjectPostResponse(BaseResponse):
    message: str = Field(default="Project created successfully.")
    project_token: str = Field(..., description="Created project token.")


class CreateProjectError(ErrorResponse):
    message: str = Field(default="Server failed to create a project!")


# /api/project/activate
class ActivateProjectPostResponse(BaseResponse):
    message: str = Field(default="Project activated successfully.")


class ActivateProjectError(ErrorResponse):
    message: str = Field(default="Server failed to activate the project.")


class InvalidOrExpiredProjectToken(ErrorResponse):
    message: str = Field(
        default="Invalid or expired project token. Acquire a session token first."
    )


# /api/session/deactivate
class DeactivateSessionPostResponse(BaseResponse):
    message: str = Field(default="Session deactivated successfully.")


class DeactivateSessionError(ErrorResponse):
    message: str = Field(default="Server failed to deactivate session.")


############################################################
# /api/session/acquire
class AcquireSessionGetResponse(BaseResponse):
    message: str = Field(default="Session acquired successfully")
    session_token: str = Field(..., description="Session token")


class AcquireSessionError(BaseResponse):
    message: str = Field(default="Server failed to acquire a session.")


# /api/completion/feedback
class NoAccessToProvideFeedbackError(ErrorResponse):
    message: str = Field(
        default="You are not allowed to provide feedback for this query."
    )


# /api/completion/get
class NoAccessToGetQueryError(ErrorResponse):
    message: str = Field(default="You do not have permission to access this query.")


class TooManyRequests(ErrorResponse):
    message: str = Field(default="Too many requests. Please try again later.")
