"""
Response schemas for API models involving users, completions, chat interactions,
telemetry, and associated metadata. Builds on shared base models and query schemas.
"""

import json
from abc import ABC
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

import Queries
from backend.utils import Fakable, SerializableBaseModel


class ResponseBase(SerializableBaseModel, Fakable, ABC):
    """Base class for all response models, combining serializability and fakability."""

    pass


class ResponseUser(Queries.CreateUser):
    """Response model representing a user in the system."""

    user_id: UUID = Field(..., description="Unique ID for the user")
    joined_at: datetime = Field(..., description="Timestamp when the user joined")
    password: SecretStr = Field(..., description="User's password (hashed)")
    verified: bool = Field(..., description="Whether the user's email is verified")
    preference: Optional[Dict[str, Any]] = Field(
        {}, description="User preferences for data management"
    )
    auth_token: Optional[UUID] = Field(
        default=None, description="Last authentication token used by the user"
    )

    @field_validator("preference", mode="before")
    @classmethod
    def parse_preference(cls, v):
        """Ensure preference is parsed from a JSON string if needed."""
        if isinstance(v, str):
            return json.loads(v)
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: SecretStr) -> SecretStr:
        """Placeholder password validation hook."""
        return v


class ResponseCompletionQuery(Queries.CreateCompletionQuery):
    """Metadata for a completion query."""

    meta_query_id: UUID = Field(..., description="Meta Query ID")
    timestamp: str = Field(..., description="Timestamp of the query")


class ResponseCompletionItem(ResponseBase):
    """Represents a successful model completion result."""

    model_id: int = Field(..., description="Model ID", ge=0)
    model_name: str = Field(..., description="Model name")
    completion: str = Field(..., description="Generated code")
    generation_time: int = Field(..., description="Generation time in ms", ge=0)
    confidence: float = Field(..., description="Confidence score of the result")


class CompletionErrorItem(ResponseBase):
    """Represents an error during code completion."""

    message: str = Field(default="Completion for model failed")
    model_name: str = Field(..., description="Model name that failed")


class ResponseCompletionResponseData(ResponseBase):
    """Container for completion responses for a given meta-query."""

    meta_query_id: UUID = Field(..., description="Meta Query ID")
    completions: List[Union[ResponseCompletionItem, CompletionErrorItem]] = Field(
        ..., description="List of generated completions or errors"
    )


class ResponseFeedbackResponseData(ResponseBase):
    """Response model for storing feedback about completions."""

    meta_query_id: UUID = Field(..., description="Meta Query ID")
    model_id: int = Field(..., description="Model ID", ge=0)


class ContextualTelemetry(Queries.ContextualTelemetryData):
    """Telemetry that includes contextual data."""

    contextual_telemetry_id: UUID = Field(
        ..., description="Unique ID for contextual telemetry record"
    )


class ResponseBehavioralTelemetry(Queries.BehavioralTelemetryData):
    """Telemetry data related to behavioral tracking."""

    behavioral_telemetry_id: UUID = Field(
        ..., description="Unique ID for behavioral telemetry record"
    )


class ChatMessageRole(str, Enum):
    """Enumeration of roles within a chat conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessageItem(ResponseBase):
    """Represents a single message in a chat conversation."""

    role: ChatMessageRole = Field(..., description="Sender's role")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Time the message was sent")
    meta_query_id: Optional[UUID] = Field(
        None, description="Associated meta query ID, if applicable"
    )


class ChatCompletionItem(ResponseBase):
    """Model-generated response in a chat conversation."""

    model_id: int = Field(..., description="Model ID", ge=0)
    model_name: str = Field(..., description="Model name")
    completion: str = Field(..., description="Generated text")
    generation_time: int = Field(..., description="Generation time in ms", ge=0)
    confidence: float = Field(..., description="Confidence score")
    was_accepted: bool = Field(..., description="User acceptance status")


class ChatCompletionErrorItem(ResponseBase):
    """Represents a failed attempt to generate a chat completion."""

    message: str = Field(default="Chat completion for model failed")
    model_name: str = Field(..., description="Model name that failed")


class ChatHistoryItem(ResponseBase):
    """Represents a single user-assistant interaction in a chat session."""

    user_message: ChatMessageItem = Field(..., description="User's input message")
    assistant_responses: List[Union[ChatCompletionItem, ChatCompletionErrorItem]] = (
        Field(..., description="Assistant's model responses")
    )


class ChatHistoryResponse(ResponseBase):
    """Complete chat conversation history for a session."""

    chat_id: UUID = Field(..., description="Unique chat session ID")
    title: str = Field(..., description="Title of the chat")
    history: List[ChatHistoryItem] = Field(..., description="List of message exchanges")


class ChatHistoryResponsePage(ResponseBase):
    """Paginated response containing multiple chat histories."""

    page: int = Field(..., description="Current page index")
    per_page: int = Field(..., description="Number of items per page")
    items: List[ChatHistoryResponse] = Field(..., description="Paginated chat sessions")


class DeleteChatSuccessResponse(ResponseBase):
    """Acknowledgement response for successful chat deletion."""

    message: str = "Chat deleted successfully"
