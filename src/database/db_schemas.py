from sqlalchemy import (
    ARRAY,
    BIGINT,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .db import Base


class Config(Base):
    __tablename__ = "config"
    config_id = Column(BIGINT, primary_key=True, nullable=False, autoincrement=True)
    config_data = Column(Text, nullable=False)  # JSON string

    # Relationships
    users = relationship("User", back_populates="config")


class User(Base):
    __tablename__ = "user"
    __table_args__ = {"extend_existing": True}
    user_id = Column(UUID(as_uuid=True), unique=True, primary_key=True, nullable=False)
    joined_at = Column(DateTime(timezone=True), nullable=False)
    email = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    password = Column(String, nullable=False)
    is_oauth_signup = Column(Boolean, default=False)
    verified = Column(Boolean, default=False)
    config_id = Column(BIGINT, ForeignKey("config.config_id"), nullable=False)
    preference = Column(Text, nullable=True)  # JSON string

    # Relationships
    config = relationship("Config", back_populates="users")
    metaqueries = relationship("MetaQuery", back_populates="user")
    project_users = relationship("ProjectUser", back_populates="user")
    sessions = relationship("Session", back_populates="user")
    chats = relationship("Chat", back_populates="user")


class ModelName(Base):
    __tablename__ = "model_name"
    model_id = Column(BIGINT, primary_key=True, nullable=False, autoincrement=True)
    model_name = Column(Text, nullable=False)
    is_instructionTuned = Column(Boolean, nullable=False, default=False)

    had_generations = relationship("HadGeneration", back_populates="model")


class PluginVersion(Base):
    __tablename__ = "plugin_version"
    version_id = Column(BIGINT, primary_key=True, nullable=False, autoincrement=True)
    version_name = Column(Text, nullable=False)
    ide_type = Column(Text, nullable=False)
    description = Column(Text)

    contextual_telemetries = relationship(
        "ContextualTelemetry", back_populates="version"
    )


class TriggerType(Base):
    __tablename__ = "trigger_type"
    trigger_type_id = Column(
        BIGINT, primary_key=True, nullable=False, autoincrement=True
    )
    trigger_type_name = Column(Text, nullable=False)

    contextual_telemetries = relationship(
        "ContextualTelemetry", back_populates="trigger_type"
    )


class ProgrammingLanguage(Base):
    __tablename__ = "programming_language"
    language_id = Column(BIGINT, primary_key=True, nullable=False, autoincrement=True)
    language_name = Column(Text, nullable=False)

    contextual_telemetries = relationship(
        "ContextualTelemetry", back_populates="language"
    )


class Context(Base):
    __tablename__ = "context"
    context_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    prefix = Column(Text)
    suffix = Column(Text)
    file_name = Column(Text)
    selected_text = Column(Text)

    metaqueries = relationship("MetaQuery", back_populates="context")


class ContextualTelemetry(Base):
    __tablename__ = "contextual_telemetry"
    contextual_telemetry_id = Column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    version_id = Column(BIGINT, ForeignKey("plugin_version.version_id"), nullable=False)
    trigger_type_id = Column(
        BIGINT, ForeignKey("trigger_type.trigger_type_id"), nullable=False
    )
    language_id = Column(
        BIGINT, ForeignKey("programming_language.language_id"), nullable=False
    )
    file_path = Column(Text, nullable=True)
    caret_line = Column(Integer, nullable=True)
    document_char_length = Column(Integer, nullable=True)
    relative_document_position = Column(Float, nullable=True)

    # Relationships
    version = relationship("PluginVersion", back_populates="contextual_telemetries")
    trigger_type = relationship("TriggerType", back_populates="contextual_telemetries")
    language = relationship(
        "ProgrammingLanguage", back_populates="contextual_telemetries"
    )
    metaqueries = relationship("MetaQuery", back_populates="contextual_telemetry")


class BehavioralTelemetry(Base):
    __tablename__ = "behavioral_telemetry"
    behavioral_telemetry_id = Column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    time_since_last_shown = Column(Integer, nullable=True)  # milliseconds
    time_since_last_accepted = Column(Integer, nullable=True)  # milliseconds
    typing_speed = Column(Integer, nullable=True)

    # Relationships
    metaqueries = relationship("MetaQuery", back_populates="behavioral_telemetry")


class Project(Base):
    __tablename__ = "project"
    project_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    project_name = Column(String, nullable=False)
    multi_file_contexts = Column(Text, default="{}")  # JSON string
    multi_file_context_changes = Column(Text, default="{}")  # JSON string
    created_at = Column(DateTime(timezone=True), nullable=False)

    # Relationships
    project_users = relationship("ProjectUser", back_populates="project")
    sessions = relationship("Session", back_populates="project")
    chats = relationship("Chat", back_populates="project")
    metaqueries = relationship("MetaQuery", back_populates="project")


class ProjectUser(Base):
    __tablename__ = "project_users"
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("project.project_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.user_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    # role = Column(String, nullable=True, default="member")  # owner, member, viewer, etc.
    joined_at = Column(DateTime(timezone=True), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="project_users")
    user = relationship("User", back_populates="project_users")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="unique_project_user"),
    )


class Session(Base):
    __tablename__ = "session"
    session_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("project.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="sessions")
    project = relationship("Project", back_populates="sessions")
    metaqueries = relationship("MetaQuery", back_populates="session")

    __table_args__ = (
        Index("idx_session_user_id", "user_id"),
        Index("idx_session_project_id", "project_id"),
    )


class Chat(Base):
    __tablename__ = "chat"
    chat_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("project.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="chats")
    user = relationship("User", back_populates="chats")
    chat_queries = relationship("ChatQuery", back_populates="chat")

    __table_args__ = (
        Index("idx_chat_project_id", "project_id"),
        Index("idx_chat_user_id", "user_id"),
    )


class MetaQuery(Base):
    __tablename__ = "metaquery"
    metaquery_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    contextual_telemetry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("contextual_telemetry.contextual_telemetry_id"),
        nullable=False,
    )
    behavioral_telemetry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("behavioral_telemetry.behavioral_telemetry_id"),
        nullable=False,
    )
    context_id = Column(
        UUID(as_uuid=True), ForeignKey("context.context_id"), nullable=False
    )
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("session.session_id"), nullable=False
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("project.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    multifile_context_changes_indexes = Column(Text, default="{}")  # JSON string
    timestamp = Column(DateTime(timezone=True), nullable=False)
    total_serving_time = Column(Integer, nullable=True)
    server_version_id = Column(BIGINT, nullable=True)
    query_type = Column(String, nullable=False)  # 'chat' or 'completion'

    # Relationships
    user = relationship("User", back_populates="metaqueries")
    contextual_telemetry = relationship(
        "ContextualTelemetry", back_populates="metaqueries"
    )
    behavioral_telemetry = relationship(
        "BehavioralTelemetry", back_populates="metaqueries"
    )
    context = relationship("Context", back_populates="metaqueries")
    project = relationship("Project", back_populates="metaqueries")
    session = relationship("Session", back_populates="metaqueries")
    had_generations = relationship("HadGeneration", back_populates="metaquery")

    __table_args__ = (
        Index("idx_metaquery_user_id", "user_id"),
        Index("idx_metaquery_project_id", "project_id"),
        Index("idx_metaquery_session_id", "session_id"),
        Index("idx_metaquery_type", "query_type"),
    )

    __mapper_args__ = {
        "polymorphic_identity": "base",
        "polymorphic_on": query_type,
    }


class CompletionQuery(MetaQuery):
    __tablename__ = "completionquery"
    metaquery_id = Column(
        UUID(as_uuid=True),
        ForeignKey("metaquery.metaquery_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    # Relationships
    ground_truths = relationship("GroundTruth", back_populates="completion_query")

    __mapper_args__ = {
        "polymorphic_identity": "completion",
    }


class ChatQuery(MetaQuery):
    __tablename__ = "chatquery"
    metaquery_id = Column(
        UUID(as_uuid=True),
        ForeignKey("metaquery.metaquery_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    chat_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    web_enabled = Column(Boolean, nullable=False, default=False)

    # Relationships
    chat = relationship("Chat", back_populates="chat_queries")

    __table_args__ = (Index("idx_chatquery_chat_id", "chat_id"),)

    __mapper_args__ = {
        "polymorphic_identity": "chat",
    }


class HadGeneration(Base):
    __tablename__ = "had_generation"
    metaquery_id = Column(
        UUID(as_uuid=True),
        ForeignKey("metaquery.metaquery_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    model_id = Column(
        BIGINT, ForeignKey("model_name.model_id"), primary_key=True, nullable=False
    )
    completion = Column(Text, nullable=False)
    generation_time = Column(Integer, nullable=False)
    shown_at = Column(ARRAY(DateTime(timezone=True)), nullable=False)
    was_accepted = Column(Boolean, nullable=False)
    confidence = Column(Float, nullable=False)
    logprobs = Column(ARRAY(Float), nullable=False)

    metaquery = relationship("MetaQuery", back_populates="had_generations")
    model = relationship("ModelName", back_populates="had_generations")

    __table_args__ = (Index("idx_metaquery_id_model_id", "metaquery_id", "model_id"),)


class GroundTruth(Base):
    __tablename__ = "ground_truth"
    completionquery_id = Column(
        UUID(as_uuid=True),
        ForeignKey("completionquery.metaquery_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    truth_timestamp = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    ground_truth = Column(Text, nullable=False)

    completion_query = relationship("CompletionQuery", back_populates="ground_truths")

    __table_args__ = (
        Index(
            "idx_completionquery_id_truth_timestamp",
            "completionquery_id",
            "truth_timestamp",
        ),
    )


#
# class SessionQuery(Base):
#     __tablename__ = "session_queries"
#     session_id = Column(
#         UUID(as_uuid=True),
#         ForeignKey("session.session_id", ondelete="CASCADE"),
#         primary_key=True,
#         nullable=False,
#     )
#     query_id = Column(
#         UUID(as_uuid=True),
#         ForeignKey("query.query_id", ondelete="CASCADE"),
#         primary_key=True,
#         nullable=False,
#     )
#     multi_file_context_changes_indexes = Column(
#         Text, default="{}"
#     )  # JSON string of the upper limit indexes of context changes used for the query in the session
#
#     # Relationships
#     session = relationship("Session", back_populates="session_queries")
#     query = relationship("Query", back_populates="session_queries")
#
#     __table_args__ = (
#         UniqueConstraint("session_id", "query_id", name="unique_session_query"),
#         Index("idx_session_queries_query_id", "query_id"),
#     )
