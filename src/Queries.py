import re
from abc import ABC
from enum import Enum
from typing import Dict, List, Optional
from uuid import UUID

from pydantic import EmailStr, Field, SecretStr, field_validator

from backend.utils import Fakable, SerializableBaseModel


class Provider(Enum):
    google = "google"


class QueryBase(SerializableBaseModel, Fakable, ABC):
    pass


class CreateUser(QueryBase):
    email: EmailStr = Field(
        ..., description="User's email address", min_length=3, max_length=50
    )
    name: str = Field(..., description="User's full name", min_length=3, max_length=50)
    password: SecretStr = Field(
        ..., description="User's password (will be hashed)", min_length=8, max_length=50
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
    # TODO: User's preference update fields should be added here later on
    name: str = Field(..., description="User's new name", min_length=3, max_length=50)


class CreateTelemetry(QueryBase):
    time_since_last_completion: int = Field(
        ..., description="Time since last completion (ms)"
    )
    typing_speed: int = Field(..., description="Typing speed (chars per minute)")
    document_char_length: int = Field(..., description="Document length in characters")
    relative_document_position: float = Field(
        ..., description="Cursor position as fraction of document"
    )


class CreateContext(QueryBase):
    prefix: str = Field(..., description="Code before cursor")
    suffix: str = Field(..., description="Code after cursor")
    file_name: str = Field(..., description="File name")
    language_id: int = Field(..., description="Programming language ID")
    # TODO maybe we can change trigger type to enum since it doesn't update that frequently
    trigger_type_id: int = Field(..., description="Trigger type ID")
    version_id: int = Field(..., description="Plugin version ID")


# Updated CompletionRequest with nested structures
class RequestCompletion(QueryBase):
    model_ids: List[int] = Field(..., description="Models to use for completion")
    context: CreateContext = Field(..., description="Context data for completion")
    telemetry: CreateTelemetry = Field(..., description="Telemetry data for completion")


class CreateQuery(QueryBase):
    user_id: UUID = Field(..., description="User ID")
    telemetry_id: UUID = Field(..., description="Telemetry record ID")
    context_id: UUID = Field(..., description="Context record ID")
    total_serving_time: int = Field(..., description="Total serving time (ms)")
    server_version_id: int = Field(..., description="Server version ID")


class CreateGeneration(QueryBase):
    query_id: UUID = Field(..., description="Query ID")
    model_id: int = Field(..., description="Model ID")
    completion: str = Field(..., description="Generated code")
    generation_time: int = Field(..., description="Generation time (ms)")
    shown_at: List[str] = Field(..., description="Timestamps when shown")
    was_accepted: bool = Field(..., description="Whether accepted by user")
    confidence: float = Field(..., description="Confidence score")
    logprobs: List[float] = Field(..., description="Token log probabilities")


class UpdateMultiFileContext(QueryBase):
    context_updates: Dict[str, str] = Field(
        ..., description="Updates to the context for multiple files"
    )


# consider refactoring
class FeedbackCompletion(QueryBase):
    query_id: UUID = Field(..., description="Query ID")
    model_id: int = Field(..., description="Model ID")
    was_accepted: bool = Field(..., description="Whether completion was accepted")
    ground_truth: Optional[str] = Field(None, description="Actual code if available")


class CreateGroundTruth(QueryBase):
    query_id: UUID = Field(..., description="Query ID")
    truth_timestamp: str = Field(..., description="Timestamp of ground truth")
    ground_truth: str = Field(..., description="Ground truth code")
