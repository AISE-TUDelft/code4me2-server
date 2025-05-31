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
    # TODO: decide UUID or int
    config_id: int = Field(..., description="Configuration ID", ge=1)
    preference: str = Field(None, description="User preferences as JSON string")

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: SecretStr) -> SecretStr:
        return v


# Query
# class QueryBase(Queries.CreateQuery):
#     query_id: UUID = Field(..., description="Query ID")
#     user_id: UUID = Field(..., description="User ID")
#     telemetry_id: UUID = Field(..., description="Telemetry record ID")
#     context_id: UUID = Field(..., description="Context record ID")
#     total_serving_time: int = Field(..., description="Total serving time (ms)", ge=0)
#     timestamp: str = Field(..., description="Timestamp of the query")
#     server_version_id: UUID = Field(..., description="Server version ID")


class CompletionQueryBase(Queries.CreateCompletionQuery):
    metaquery_id: UUID = Field(..., description="MetaQuery ID")
    user_id: UUID = Field(..., description="User ID")
    contextual_telemetry_id: UUID = Field(
        ..., description="Contextual telemetry record ID"
    )
    behavioral_telemetry_id: UUID = Field(
        ..., description="Behavioral telemetry record ID"
    )
    context_id: UUID = Field(..., description="Context record ID")
    session_id: UUID = Field(..., description="Session ID")
    project_id: UUID = Field(..., description="Project ID")
    multifile_context_changes_indexes: str = Field(
        ..., description="Context change indexes as JSON"
    )
    total_serving_time: int = Field(..., description="Total serving time (ms)", ge=0)
    timestamp: str = Field(..., description="Timestamp of the query")
    server_version_id: int = Field(..., description="Server version ID", ge=0)


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


class CompletionItem(ModelBase):
    model_id: int = Field(..., description="Model ID", ge=0)
    model_name: str = Field(..., description="Model name")
    completion: str = Field(..., description="Generated code")
    generation_time: int = Field(..., description="Generation time", ge=0)
    confidence: float = Field(..., description="Confidence score")


class CompletionResponseData(ModelBase):
    metaquery_id: UUID = Field(..., description="MetaQuery ID")
    completions: list[CompletionItem] = Field(..., description="Generated completions")


class FeedbackResponseData(ModelBase):
    metaquery_id: UUID = Field(..., description="MetaQuery ID")
    model_id: int = Field(..., description="Model ID", ge=0)


class ContextBase(Queries.ContextData):
    context_id: UUID = Field(..., description="Context record ID")


class TelemetryBase(Queries.TelemetryData):
    telemetry_id: UUID = Field(..., description="Telemetry record ID")


class ContextualTelemetryBase(Queries.ContextualTelemetryData):
    contextual_telemetry_id: UUID = Field(
        ..., description="Contextual telemetry record ID"
    )


class BehavioralTelemetryBase(Queries.BehavioralTelemetryData):
    behavioral_telemetry_id: UUID = Field(
        ..., description="Behavioral telemetry record ID"
    )
