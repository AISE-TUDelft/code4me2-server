import json
from abc import ABC
from datetime import datetime
from typing import Any, Dict, Optional, Union
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

import Queries
from backend.utils import Fakable, SerializableBaseModel


class ResponseBase(SerializableBaseModel, Fakable, ABC):
    pass


class ResponseUser(Queries.CreateUser):
    user_id: UUID = Field(..., description="Unique id for the user")
    joined_at: datetime = Field(..., description="When the user was created")
    password: SecretStr = Field(..., description="User's password (will be hashed)")
    verified: bool = Field(
        ..., description="Whether the user's email has been verified"
    )
    preference: Optional[Dict[str, Any]] = Field(
        {}, description="Users preference for data management"
    )
    auth_token: Optional[UUID] = Field(
        default=None, description="Last auth token used to login"
    )

    @field_validator("preference", mode="before")
    @classmethod
    def parse_preference(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v

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


class ResponseCompletionQuery(Queries.CreateCompletionQuery):
    meta_query_id: UUID = Field(..., description="Meta Query ID")
    timestamp: str = Field(..., description="Timestamp of the query")


# Model Name
# class ModelNameBase(ResponseBase):
#     model_name: str
#
#     model_config = {"protected_namespaces": ()}


# Plugin Version
# class PluginVersionBase(ResponseBase):
#     version_name: str
#     ide_type: str
#     description: str


class ResponseCompletionItem(ResponseBase):
    model_id: int = Field(..., description="Model ID", ge=0)
    model_name: str = Field(..., description="Model name")
    completion: str = Field(..., description="Generated code")
    generation_time: int = Field(..., description="Generation time", ge=0)
    confidence: float = Field(..., description="Confidence score")


class CompletionErrorItem(ResponseBase):
    message: str = Field(default="Completion for model failed")
    model_name: str = Field(..., description="Model name")


class ResponseCompletionResponseData(ResponseBase):
    meta_query_id: UUID = Field(..., description="Meta Query ID")
    completions: list[Union[ResponseCompletionItem, CompletionErrorItem]] = Field(
        ..., description="Generated completions"
    )


class ResponseFeedbackResponseData(ResponseBase):
    meta_query_id: UUID = Field(..., description="Meta Query ID")
    model_id: int = Field(..., description="Model ID", ge=0)


# class ResponseContext(Queries.ContextData):
#     context_id: UUID = Field(..., description="Context record ID")


# class ResponseTelemetry(Queries.TelemetryData):
#     telemetry_id: UUID = Field(..., description="Telemetry record ID")


class ContextualTelemetry(Queries.ContextualTelemetryData):
    contextual_telemetry_id: UUID = Field(
        ..., description="Contextual telemetry record ID"
    )


class ResopnseBehavioralTelemetry(Queries.BehavioralTelemetryData):
    behavioral_telemetry_id: UUID = Field(
        ..., description="Behavioral telemetry record ID"
    )
