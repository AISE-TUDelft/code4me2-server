from enum import Enum

from pydantic import BaseModel, EmailStr, SecretStr, Field


class Provider(Enum):
    google = "google"


class QueryBase(BaseModel):
    pass


class CreateUser(QueryBase):
    email: EmailStr = Field(..., description="User's email address")
    name: str = Field(..., description="User's full name")
    password: SecretStr = Field(..., description="User's password (will be hashed)")


class CreateUserAuth(CreateUser):
    token: str = Field(..., description="JWT token for authentication")
    provider: Provider = Field(
        ..., description="OAuth provider (Google, Microsoft, etc.)"
    )


class AuthenticateUserEmailPassword(BaseModel):
    email: EmailStr = Field(..., description="User's email address")
    password: SecretStr = Field(..., description="User's password")


class AuthenticateUserOAuth(BaseModel):
    provider: Provider = Field(..., description="OAuth provider")
    token: str = Field(..., description="OAuth token in JWT format")
