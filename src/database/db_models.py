from pydantic import BaseModel

from base_models import UserBase

# pydantic classes for the database models


# User
class UserAuth(BaseModel):
    email: str
    password: str


class UserWithToken(BaseModel):
    token: str
    email: str
    name: str
    verified: bool
    is_google_signup: bool
    joined_at: str

    class Config:
        from_attributes = True


class UserDB(UserBase):
    password: str  # This will be converted to password_hash before storage


# class ModelNameDB(ModelNameBase):
#     model_id: int = Field(..., alias="id")
#
#     class Config:
#         from_attributes = True
#         protected_namespaces = ()


# class PluginVersionDB(PluginVersionBase):
#     version_id: int
#
#     class Config:
#         from_attributes = True


# # Trigger Type
# class TriggerTypeBase(BaseModel):
#     trigger_type_name: str
#
#
# class TriggerType(TriggerTypeBase):
#     trigger_type_id: int
#
#     class Config:
#         from_attributes = True
#
#
# class TriggerTypeCreate(TriggerTypeBase):
#     pass
#
#
# # Programming Language
# class ProgrammingLanguageBase(BaseModel):
#     language_name: str
#
#
# class ProgrammingLanguage(ProgrammingLanguageBase):
#     language_id: int
#
#     class Config:
#         from_attributes = True
#
#
# class ProgrammingLanguageCreate(ProgrammingLanguageBase):
#     pass
#
#
# # Had Generation
# class HadGenerationBase(BaseModel):
#     query_id: str
#     model_id: int
#     completion: str
#     generation_time: int
#     shown_at: list[str]
#     was_accepted: bool
#     confidence: float
#     logprobs: list[float]
#
#
# class HadGeneration(HadGenerationBase):
#     class Config:
#         from_attributes = True
#
#
# class HadGenerationCreate(HadGenerationBase):
#     pass
#
#
# class HadGenerationUpdate(BaseModel):
#     shown_at: list[str]
#     was_accepted: bool
#
#
# # Ground Truth
# class GroundTruthBase(BaseModel):
#     query_id: str
#     truth_timestamp: str
#     ground_truth: str
#
#
# class GroundTruth(GroundTruthBase):
#     class Config:
#         from_attributes = True
#
#
# class GroundTruthCreate(GroundTruthBase):
#     pass
#
#
# # Context
# class ContextBase(BaseModel):
#     context_id: str
#     prefix: str
#     suffix: str
#     language_id: int
#     trigger_type_id: int
#     version_id: int
#
#
# class Context(ContextBase):
#     class Config:
#         from_attributes = True
#
#
# class ContextCreate(ContextBase):
#     pass
#
#
# # Telemetry
# class TelemetryBase(BaseModel):
#     telemetry_id: str
#     time_since_last_completion: int
#     typing_speed: int
#     document_char_length: int
#     relative_document_position: float
#
#
# class Telemetry(TelemetryBase):
#     class Config:
#         from_attributes = True
#
#
# class TelemetryCreate(TelemetryBase):
#     pass
