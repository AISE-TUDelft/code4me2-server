from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, SecretStr, Field


class CreateUser(BaseModel):
    email: EmailStr = Field(..., description="User's email address")
    name: str = Field(..., description="User's full name")
    password: SecretStr = Field(..., description="User's password (will be hashed)")
    googleId: Optional[str] = Field(
        None, description="Google ID for users signing up with Google (optional)"
    )
    googleCredential: Optional[str] = Field(
        None, description="Google JWT credential for authentication (optional)"
    )


class EmailPasswordAuth(BaseModel):
    email: EmailStr = Field(..., description="User's email address")
    password: SecretStr = Field(..., description="User's password")


class Provider(Enum):
    google = "google"


class OAuthAuth(BaseModel):
    provider: Provider = Field(..., description="OAuth provider")
    token: str = Field(..., description="OAuth token in JWT format")
