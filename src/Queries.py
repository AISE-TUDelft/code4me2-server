import re
from abc import ABC
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional
from uuid import UUID

from pydantic import ConfigDict, EmailStr, Field, SecretStr, field_validator

from backend.utils import Fakable, SerializableBaseModel


class Provider(Enum):
    google = "google"


class ContextChangeType(Enum):
    update = "update"
    insert = "insert"
    remove = "remove"


class QueryBase(SerializableBaseModel, Fakable, ABC):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class CreateUser(QueryBase):
    email: EmailStr = Field(
        ..., description="User's email address", min_length=3, max_length=50
    )
    name: str = Field(..., description="User's full name", min_length=3, max_length=50)
    password: SecretStr = Field(
        ..., description="User's password (will be hashed)", min_length=8, max_length=50
    )
    config_id: int = Field(..., description="Configuration ID to use", ge=1)
    preference: Optional[str] = Field(
        None, description="User preferences as JSON string"
    )

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: SecretStr) -> SecretStr:
        """
        Validate the password to ensure it meets the criteria:
        - At least 8 characters long
        - Contains at least one uppercase letter
        - Contains at least one lowercase letter
        - Contains at least one digit
        """
        pattern = r"^(?=.*[A-Z])(?=.*[a-z])(?=.*\d)\S{8,}$"
        password_value = v.get_secret_value()
        if not password_value:
            raise ValueError("Password cannot be empty")
        elif not re.match(pattern, password_value):
            raise ValueError(
                "Password must be at least 8 characters long, "
                "contain at least one uppercase letter, "
                "one lowercase letter, and one digit."
            )
        return v


class CreateUserOauth(CreateUser):
    token: str = Field(..., description="JWT token for authentication")
    provider: Provider = Field(
        ..., description="OAuth provider (Google, Microsoft, etc.)"
    )


class AuthenticateUserEmailPassword(QueryBase):
    email: EmailStr = Field(..., description="User's email address")
    password: SecretStr = Field(..., description="User's password")


class AuthenticateUserOAuth(QueryBase):
    provider: Provider = Field(..., description="OAuth provider")
    token: str = Field(..., description="OAuth token in JWT format")


class UpdateUser(QueryBase):
    # should we get user_id?
    name: Optional[str] = Field(
        None, description="User's new name", min_length=3, max_length=50
    )
    preference: Optional[str] = Field(
        None, description="Updated user preferences as JSON string"
    )
    config_id: Optional[int] = Field(None, description="New configuration ID", ge=1)


class CreateConfig(QueryBase):
    config_data: str = Field(..., description="Configuration data as JSON string")


# class ContextData(QueryBase):
#     prefix: str = Field(..., description="Code before cursor")
#     suffix: str = Field(..., description="Code after cursor")
#     # TODO make things optional
#     file_name: str = Field(..., description="File name")
#     # TODO maybe we can change trigger type to enum since it doesn't update that frequently
#     language_id: Optional[int] = Field(
#         default=1, description="Programming language ID", ge=0
#     )
#     trigger_type_id: Optional[int] = Field(
#         default=1, description="Trigger type ID", ge=0
#     )
#     version_id: Optional[int] = Field(default=1, description="Plugin version ID", ge=0)
#     context_files: Optional[list[str]] = Field(
#         default=[],
#         description="List of context files to include upon completion. "
#         "If ['*'] is passed, all files will be included. "
#         "If empty list is passed, no files will be included.",
#     )
# class TelemetryData(QueryBase):
#     time_since_last_completion: int = Field(
#         ..., description="Time since last completion (ms)", ge=0
#     )
#     typing_speed: int = Field(..., description="Typing speed (chars per minute)", ge=0)
#     document_char_length: int = Field(
#         ..., description="Document length in characters", ge=0
#     )
#     relative_document_position: float = Field(
#         ..., description="Cursor position as fraction of document"
#     )


class ContextData(QueryBase):
    prefix: Optional[str] = Field(None, description="Code before cursor")
    suffix: Optional[str] = Field(None, description="Code after cursor")
    file_name: Optional[str] = Field(None, description="File name")
    selected_text: Optional[str] = Field(None, description="Selected text in editor")


class ContextualTelemetryData(QueryBase):
    version_id: int = Field(..., description="Plugin version ID", ge=1)
    trigger_type_id: int = Field(..., description="Trigger type ID", ge=1)
    language_id: int = Field(..., description="Programming language ID", ge=1)
    file_path: Optional[str] = Field(None, description="Path to the file being edited")
    caret_line: Optional[int] = Field(None, description="Line number of cursor", ge=0)
    document_char_length: Optional[int] = Field(
        None, description="Document length in characters", ge=0
    )
    relative_document_position: Optional[float] = Field(
        None, description="Cursor position as fraction of document", ge=0.0, le=1.0
    )


class BehavioralTelemetryData(QueryBase):
    time_since_last_shown: Optional[int] = Field(
        None, description="Time since last completion shown (ms)", ge=0
    )
    time_since_last_accepted: Optional[int] = Field(
        None, description="Time since last completion accepted (ms)", ge=0
    )
    typing_speed: Optional[int] = Field(
        None, description="Typing speed (chars per minute)", ge=0
    )


class RequestCompletion(QueryBase):
    model_ids: List[int] = Field(..., description="Models to use for completion")
    context: Mapping[str, Any] = Field(..., description="Context data for completion")
    contextual_telemetry: Mapping[str, Any] = Field(
        ..., description="Contextual telemetry data"
    )
    behavioral_telemetry: Mapping[str, Any] = Field(
        ..., description="Behavioral telemetry data"
    )
    # telemetry: Mapping[str, Any] = Field(
    #     ..., description="Telemetry data for completion"
    # )


class CreateCompletionQuery(QueryBase):
    user_id: Optional[UUID] = Field(None, description="User ID")
    contextual_telemetry_id: UUID = Field(
        ..., description="Contextual telemetry record ID"
    )
    behavioral_telemetry_id: UUID = Field(
        ..., description="Behavioral telemetry record ID"
    )
    context_id: UUID = Field(..., description="Context record ID")
    session_id: UUID = Field(..., description="Session ID")
    project_id: UUID = Field(..., description="Project ID")
    multifile_context_changes_indexes: Optional[str] = Field(
        default="{}", description="Context change indexes as JSON"
    )
    total_serving_time: Optional[int] = Field(
        None, description="Total serving time (ms)", ge=0
    )
    server_version_id: Optional[int] = Field(
        None, description="Server version ID", ge=0
    )


class CreateChatQuery(QueryBase):
    user_id: Optional[UUID] = Field(None, description="User ID")
    contextual_telemetry_id: UUID = Field(
        ..., description="Contextual telemetry record ID"
    )
    behavioral_telemetry_id: UUID = Field(
        ..., description="Behavioral telemetry record ID"
    )
    context_id: UUID = Field(..., description="Context record ID")
    session_id: UUID = Field(..., description="Session ID")
    project_id: UUID = Field(..., description="Project ID")
    chat_id: UUID = Field(..., description="Chat ID")
    multifile_context_changes_indexes: Optional[str] = Field(
        default="{}", description="Context change indexes as JSON"
    )
    total_serving_time: Optional[int] = Field(
        None, description="Total serving time (ms)", ge=0
    )
    server_version_id: Optional[int] = Field(
        None, description="Server version ID", ge=0
    )
    web_enabled: Optional[bool] = Field(
        default=False, description="Whether web access is enabled"
    )


class CreateGeneration(QueryBase):
    metaquery_id: UUID = Field(..., description="MetaQuery ID")
    model_id: int = Field(..., description="Model ID", ge=0)
    completion: str = Field(..., description="Generated code/text")
    generation_time: int = Field(..., description="Generation time (ms)", ge=0)
    shown_at: List[str] = Field(..., description="Timestamps when shown")
    was_accepted: bool = Field(..., description="Whether accepted by user")
    confidence: float = Field(..., description="Confidence score")
    logprobs: List[float] = Field(..., description="Token log probabilities")


class FileContextChangeData(QueryBase):
    change_type: ContextChangeType = Field(
        ..., description="Type of change ('update', 'insert', 'remove')"
    )
    start_line: int = Field(..., description="Start line number", ge=0)
    end_line: int = Field(..., description="End line number", ge=0)
    new_lines: List[str] = Field(..., description="New lines of code")


class UpdateMultiFileContext(QueryBase):
    context_updates: Dict[str, List[FileContextChangeData]] = Field(
        ..., description="Updates to the context for multiple files"
    )


# consider refactoring
class FeedbackCompletion(QueryBase):
    metaquery_id: UUID = Field(..., description="MetaQuery ID")
    model_id: int = Field(..., description="Model ID", ge=0)
    was_accepted: bool = Field(..., description="Whether completion was accepted")
    ground_truth: Optional[str] = Field(None, description="Actual code if available")


class CreateGroundTruth(QueryBase):
    completionquery_id: UUID = Field(..., description="CompletionQuery ID")
    ground_truth: str = Field(..., description="Ground truth code")
    # truth_timestamp: str = Field(..., description="Timestamp of ground truth")


class CreateProject(QueryBase):
    project_name: str = Field(
        ..., description="Project name", min_length=1, max_length=100
    )
    multi_file_contexts: Optional[str] = Field(
        default="{}", description="Multi-file contexts as JSON"
    )
    multi_file_context_changes: Optional[str] = Field(
        default="{}", description="Context changes as JSON"
    )


class UpdateProject(QueryBase):
    project_name: Optional[str] = Field(
        None, description="Updated project name", min_length=1, max_length=100
    )
    multi_file_contexts: Optional[str] = Field(
        None, description="Updated multi-file contexts as JSON"
    )
    multi_file_context_changes: Optional[str] = Field(
        None, description="Updated context changes as JSON"
    )


class AddUserToProject(QueryBase):
    project_id: UUID = Field(..., description="Project ID")
    user_id: UUID = Field(..., description="User ID")
    # role: Optional[str] = Field(default="member", description="User role in project")


# TODO check naming consistency
class ActivateSession(QueryBase):
    session_token: str = Field(..., description="Session token to activate")


# TODO check naming consistency
class DeactivateSession(QueryBase):
    session_token: str = Field(..., description="Session token to deactivate")


class CreateSession(QueryBase):
    user_id: UUID = Field(..., description="User ID")
    project_id: UUID = Field(..., description="Project ID")
    # Should start_time be given?


class UpdateSession(QueryBase):
    # should we get session_id?
    end_time: Optional[str] = Field(None, description="Session end time")


class DeleteUser(QueryBase):
    delete_data: Optional[bool] = Field(
        default=False, description="Whether to delete user data or just the account"
    )


class CreateModel(QueryBase):
    model_name: str = Field(..., description="Model name")
    is_instructionTuned: Optional[bool] = Field(
        default=False, description="Whether model is instruction-tuned"
    )


class UpdateChat(QueryBase):
    title: Optional[str] = Field(
        None, description="Updated chat title", min_length=1, max_length=200
    )


class CreateChat(QueryBase):
    project_id: UUID = Field(..., description="Project ID")
    user_id: UUID = Field(..., description="Chat owner user ID")
    title: str = Field(..., description="Chat title", min_length=1, max_length=200)


class UpdateGenerationAcceptance(QueryBase):
    metaquery_id: UUID = Field(..., description="MetaQuery ID")
    model_id: int = Field(..., description="Model ID", ge=0)
    was_accepted: bool = Field(..., description="Whether completion was accepted")


if __name__ == "__main__":
    fake_update_multi_file_context = UpdateMultiFileContext.fake(1)
    res = fake_update_multi_file_context.dict()
    print(res)
