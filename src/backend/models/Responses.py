from typing import Optional, List
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from base_models import UserBase


class MessageResponse(BaseModel):
    message: str = Field(..., description="Response message")


class UserExistsPostResponse(MessageResponse):
    exists: bool = Field(..., description="Whether the user exists")


class CreateUserPostResponse(MessageResponse):
    user_id: UUID = Field(..., description="Created user id")
    session_id: Optional[UUID] = Field(None, description="Created session id")


class UserAuthenticatePostResponse(MessageResponse):
    user_id: UUID = Field(..., description="User id for authentication")
    session_id: Optional[UUID] = Field(
        None, description="Session id for authentication"
    )
    user: UserBase = Field(..., description="User details")  # Uncomment if needed


class ErrorResponse(MessageResponse):
    pass


class JsonResponseWithStatus(JSONResponse):
    def __init__(self, content: BaseModel, status_code: int):
        # Convert the Pydantic model to a dict
        super().__init__(content=jsonable_encoder(content), status_code=status_code)


# New response models for completions
#TODO Decide to remain here or in elsewhere
class CompletionItem(BaseModel):
    model_id: int = Field(..., description="Model ID")
    model_name: str = Field(..., description="Model name")
    completion: str = Field(..., description="Generated code")
    confidence: float = Field(..., description="Confidence score")


class CompletionResponseData(BaseModel):
    query_id: UUID = Field(..., description="Query ID")
    completions: List[CompletionItem] = Field(..., description="Generated completions")


class CompletionResponse(MessageResponse):
    data: CompletionResponseData = Field(..., description="Completion data")


class FeedbackResponseData(BaseModel):
    query_id: UUID = Field(..., description="Query ID")
    model_id: int = Field(..., description="Model ID")


class FeedbackResponse(MessageResponse):
    data: FeedbackResponseData = Field(..., description="Feedback data")