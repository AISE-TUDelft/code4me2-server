from abc import ABC
from datetime import datetime
from uuid import UUID

from pydantic import EmailStr, Field, SecretStr, field_validator

import Queries
from backend.utils import Fakable, SerializableBaseModel


class ModelBase(SerializableBaseModel, Fakable, ABC):
    pass


class UserBase(Queries.CreateUser):
    user_id: UUID = Field(..., description="Unique id for the user")
    email: EmailStr = Field(..., description="User's email address")
    name: str = Field(..., description="User's full name")
    password: SecretStr = Field(
        ...,
        description="User's password",
    )
    joined_at: datetime = Field(..., description="When the user was created")
    verified: bool = Field(
        ..., description="Whether the user's email has been verified"
    )
    model_config = {
        "from_attributes": True,  # enables reading from ORM objects
        "extra": "ignore",  # Disallow extra fields
    }

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: SecretStr) -> SecretStr:
        return v


# Query
class QueryBase(Queries.CreateQuery):
    query_id: UUID
    user_id: UUID
    telemetry_id: UUID
    context_id: UUID
    total_serving_time: int
    timestamp: str
    server_version_id: UUID


# Model Name
# class ModelNameBase(ModelBase):
#     model_name: str
#
#     model_config = {"protected_namespaces": ()}


# Plugin Version
# class PluginVersionBase(ModelBase):
#     version_name: str
#     ide_type: str
#     description: str


class ContextBase(Queries.CreateContext):
    context_id: UUID = Field(..., description="Context record ID")


class TelemetryBase(Queries.CreateTelemetry):
    telemetry_id: UUID = Field(..., description="Telemetry record ID")


class CompletionItem(ModelBase):
    model_id: int = Field(..., description="Model ID")
    model_name: str = Field(..., description="Model name")
    completion: str = Field(..., description="Generated code")
    generation_time: int = Field(..., description="Generation time")
    confidence: float = Field(..., description="Confidence score")


class CompletionResponseData(ModelBase):
    query_id: UUID = Field(..., description="Query ID")
    completions: list[CompletionItem] = Field(..., description="Generated completions")


class FeedbackResponseData(ModelBase):
    query_id: UUID = Field(..., description="Query ID")
    model_id: int = Field(..., description="Model ID")
