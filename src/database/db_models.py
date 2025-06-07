from pydantic import BaseModel

from response_models import UserBase

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
#


# class ConfigBase(BaseModel):
#     config_data: str
#
#
# class Config(ConfigBase):
#     config_id: int
#
#     class Config:
#         from_attributes = True
#
#
# class ConfigCreate(ConfigBase):
#     pass
#
#
# # User models
# class UserBase(BaseModel):
#     email: str
#     name: str
#     is_oauth_signup: bool = False
#     verified: bool = False
#
#
# class UserAuth(BaseModel):
#     email: str
#     password: str
#
#
# class UserWithToken(BaseModel):
#     token: str
#     email: str
#     name: str
#     verified: bool
#     is_oauth_signup: bool
#     joined_at: str
#
#     class Config:
#         from_attributes = True
#
#
# class UserDB(UserBase):
#     user_id: UUID
#     joined_at: datetime
#     password: str
#     config_id: int
#     preference: Optional[str] = None
#
#     class Config:
#         from_attributes = True
#
#
# class UserCreate(BaseModel):
#     email: str
#     name: str
#     password: str
#     config_id: int
#     preference: Optional[str] = None
#
#
# class UserUpdate(BaseModel):
#     name: Optional[str] = None
#     preference: Optional[str] = None
#     config_id: Optional[int] = None
#
#
# # Model name models
# class ModelNameBase(BaseModel):
#     model_name: str
#     is_instruction_tuned: bool = False
#
#
# class ModelName(ModelNameBase):
#     model_id: int
#
#     class Config:
#         from_attributes = True
#
#
# class ModelNameCreate(ModelNameBase):
#     pass
#
#
# # Context models
# class ContextBase(BaseModel):
#     prefix: Optional[str] = None
#     suffix: Optional[str] = None
#     file_name: Optional[str] = None
#     selected_text: Optional[str] = None
#
#
# class Context(ContextBase):
#     context_id: UUID
#
#     class Config:
#         from_attributes = True
#
#
# class ContextCreate(ContextBase):
#     pass
#
#
# # Telemetry models
# class ContextualTelemetryBase(BaseModel):
#     version_id: int
#     trigger_type_id: int
#     language_id: int
#     file_path: Optional[str] = None
#     caret_line: Optional[int] = None
#     document_char_length: Optional[int] = None
#     relative_document_position: Optional[float] = None
#
#
# class ContextualTelemetry(ContextualTelemetryBase):
#     contextual_telemetry_id: UUID
#
#     class Config:
#         from_attributes = True
#
#
# class ContextualTelemetryCreate(ContextualTelemetryBase):
#     pass
#
#
# class BehavioralTelemetryBase(BaseModel):
#     time_since_last_shown: Optional[int] = None
#     time_since_last_accepted: Optional[int] = None
#     typing_speed: Optional[int] = None
#
#
# class BehavioralTelemetry(BehavioralTelemetryBase):
#     behavioral_telemetry_id: UUID
#
#     class Config:
#         from_attributes = True
#
#
# class BehavioralTelemetryCreate(BehavioralTelemetryBase):
#     pass
#
#
# # Project models
# class ProjectBase(BaseModel):
#     project_name: str
#     multi_file_contexts: str = "{}"
#     multi_file_context_changes: str = "{}"
#
#
# class Project(ProjectBase):
#     project_id: UUID
#     created_at: datetime
#
#     class Config:
#         from_attributes = True
#
#
# class ProjectCreate(ProjectBase):
#     pass
#
#
# class ProjectUpdate(BaseModel):
#     project_name: Optional[str] = None
#     multi_file_contexts: Optional[str] = None
#     multi_file_context_changes: Optional[str] = None
#
#
# # Project User models
# class ProjectUserBase(BaseModel):
#     role: str = "member"
#
#
# class ProjectUser(ProjectUserBase):
#     project_id: UUID
#     user_id: UUID
#     joined_at: datetime
#
#     class Config:
#         from_attributes = True
#
#
# class ProjectUserCreate(ProjectUserBase):
#     project_id: UUID
#     user_id: UUID
#
#
# # Session models
# class SessionBase(BaseModel):
#     start_time: datetime
#     end_time: Optional[datetime] = None
#
#
# class Session(SessionBase):
#     session_id: UUID
#     user_id: UUID
#     project_id: UUID
#
#     class Config:
#         from_attributes = True
#
#
# class SessionCreate(SessionBase):
#     user_id: UUID
#     project_id: UUID
#
#
# class SessionUpdate(BaseModel):
#     end_time: Optional[datetime] = None
#
#
# # Chat models
# class ChatBase(BaseModel):
#     title: str
#
#
# class Chat(ChatBase):
#     chat_id: UUID
#     project_id: UUID
#     user_id: UUID
#     created_at: datetime
#
#     class Config:
#         from_attributes = True
#
#
# class ChatCreate(ChatBase):
#     project_id: UUID
#     user_id: UUID
#
#
# class ChatUpdate(BaseModel):
#     title: Optional[str] = None
#
#
# # MetaQuery models
# class MetaQueryBase(BaseModel):
#     contextual_telemetry_id: UUID
#     behavioral_telemetry_id: UUID
#     context_id: UUID
#     session_id: UUID
#     project_id: UUID
#     multi_file_context_changes_indexes: str = "{}"
#     total_serving_time: Optional[int] = None
#     server_version_id: Optional[int] = None
#
#
# class MetaQuery(MetaQueryBase):
#     meta_query_id: UUID
#     user_id: Optional[UUID] = None
#     timestamp: datetime
#     query_type: str
#
#     class Config:
#         from_attributes = True
#
#
# class MetaQueryCreate(MetaQueryBase):
#     user_id: Optional[UUID] = None
#
#
# # CompletionQuery models
# class CompletionQuery(MetaQuery):
#     class Config:
#         from_attributes = True
#
#
# class CompletionQueryCreate(MetaQueryCreate):
#     pass
#
#
# # ChatQuery models
# class ChatQueryBase(BaseModel):
#     web_enabled: bool = False
#
#
# class ChatQuery(MetaQuery):
#     chat_id: UUID
#     web_enabled: bool
#
#     class Config:
#         from_attributes = True
#
#
# class ChatQueryCreate(MetaQueryCreate, ChatQueryBase):
#     chat_id: UUID
#
#
# # HadGeneration models
# class HadGenerationBase(BaseModel):
#     completion: str
#     generation_time: int
#     shown_at: List[datetime]
#     was_accepted: bool
#     confidence: float
#     logprobs: List[float]
#
#
# class HadGeneration(HadGenerationBase):
#     meta_query_id: UUID
#     model_id: int
#
#     class Config:
#         from_attributes = True
#
#
# class HadGenerationCreate(HadGenerationBase):
#     meta_query_id: UUID
#     model_id: int
#
#
# class HadGenerationUpdate(BaseModel):
#     shown_at: Optional[List[datetime]] = None
#     was_accepted: Optional[bool] = None
#
#
# # GroundTruth models
# class GroundTruthBase(BaseModel):
#     ground_truth: str
#
#
# class GroundTruth(GroundTruthBase):
#     completion_query_id: UUID
#     truth_timestamp: datetime
#
#     class Config:
#         from_attributes = True
#
#
# class GroundTruthCreate(GroundTruthBase):
#     completion_query_id: UUID
#
#
# # Trigger Type models
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
# # Programming Language models
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
# # Plugin Version models
# class PluginVersionBase(BaseModel):
#     version_name: str
#     ide_type: str
#     description: Optional[str] = None
#
#
# class PluginVersion(PluginVersionBase):
#     version_id: int
#
#     class Config:
#         from_attributes = True
#
#
# class PluginVersionCreate(PluginVersionBase):
#     pass
