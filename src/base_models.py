from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, SecretStr, Field


class UserBase(BaseModel):
    id: Optional[UUID] = Field(None, description="Unique identifier for the user")
    email: Optional[EmailStr] = Field(None, description="User's email address")
    name: Optional[str] = Field(None, description="User's full name")
    createdAt: Optional[datetime] = Field(None, description="When the user was created")
    verified: Optional[bool] = Field(
        None, description="Whether the user's email has been verified"
    )


# Query
class QueryBase(BaseModel):
    query_id: str
    user_id: str
    telemetry_id: str
    context_id: str
    total_serving_time: int
    timestamp: str
    server_version_id: int


# Model Name
class ModelNameBase(BaseModel):
    model_name: str

    class Config:
        protected_namespaces = ()


# Plugin Version
class PluginVersionBase(BaseModel):
    version_name: str
    ide_type: str
    description: str
