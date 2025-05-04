from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserBase(BaseModel):
    token: str = Field(..., description="Unique token for the user")
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
