from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class UserBase(BaseModel):
    user_id: UUID = Field(..., description="Unique id for the user")
    email: EmailStr = Field(..., description="User's email address")
    name: str = Field(..., description="User's full name")
    joined_at: datetime = Field(..., description="When the user was created")
    verified: bool = Field(
        ..., description="Whether the user's email has been verified"
    )

    model_config = {
        "from_attributes": True,  # enables reading from ORM objects
        "extra": "ignore",  # Disallow extra fields
    }


# Query
class QueryBase(BaseModel):
    query_id: UUID
    user_id: UUID
    telemetry_id: UUID
    context_id: UUID
    total_serving_time: int
    timestamp: str
    server_version_id: UUID


# Model Name
class ModelNameBase(BaseModel):
    model_name: str

    model_config = {"protected_namespaces": ()}


# Plugin Version
class PluginVersionBase(BaseModel):
    version_name: str
    ide_type: str
    description: str
