from typing import Union

from pydantic import BaseModel, EmailStr, Field

from backend.models.Bodies import EmailPasswordAuth, OAuthAuth


class UserAuthenticatePostRequest(BaseModel):
    authentication: Union[EmailPasswordAuth, OAuthAuth] = Field(
        ..., description="Authentication method (email/password or OAuth)"
    )


class UserExistsPostRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address to check")
