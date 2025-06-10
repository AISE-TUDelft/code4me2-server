from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .db import Base

DEFAULT_USER_PREFERENCE = {
    "store_context": False,
    "store_contextual_telemetry": True,
    "store_behavioral_telemetry": True,
}


class Config(Base):
    __tablename__ = "config"
    __table_args__ = {"schema": "public"}

    config_id = Column(BigInteger, primary_key=True)
    config_data = Column(Text, nullable=False)


class User(Base):
    __tablename__ = "user"
    __table_args__ = {"schema": "public"}

    user_id = Column(UUID(as_uuid=True), primary_key=True)
    joined_at = Column(DateTime(timezone=True), nullable=False)
    email = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    password = Column(String, nullable=False)
    is_oauth_signup = Column(Boolean, default=False)
    verified = Column(Boolean, default=False)
    config_id = Column(
        BigInteger, ForeignKey("public.config.config_id"), nullable=False
    )
    preference = Column(Text)
    auth_token = Column(UUID(as_uuid=True), nullable=True)

    config = relationship("Config")


class ModelName(Base):
    __tablename__ = "model_name"
    __table_args__ = {"schema": "public"}

    model_id = Column(BigInteger, primary_key=True)
    model_name = Column(Text, nullable=False)
    is_instruction_tuned = Column(Boolean, default=False, nullable=False)


class PluginVersion(Base):
    __tablename__ = "plugin_version"
    __table_args__ = {"schema": "public"}

    version_id = Column(BigInteger, primary_key=True)
    version_name = Column(Text, nullable=False)
    ide_type = Column(Text, nullable=False)
    description = Column(Text)


class TriggerType(Base):
    __tablename__ = "trigger_type"
    __table_args__ = {"schema": "public"}

    trigger_type_id = Column(BigInteger, primary_key=True)
    trigger_type_name = Column(Text, nullable=False)


class ProgrammingLanguage(Base):
    __tablename__ = "programming_language"
    __table_args__ = {"schema": "public"}

    language_id = Column(BigInteger, primary_key=True)
    language_name = Column(Text, nullable=False)


class Context(Base):
    __tablename__ = "context"
    __table_args__ = {"schema": "public"}

    context_id = Column(UUID(as_uuid=True), primary_key=True)
    prefix = Column(Text)
    suffix = Column(Text)
    file_name = Column(Text)
    selected_text = Column(Text)


class ContextualTelemetry(Base):
    __tablename__ = "contextual_telemetry"
    __table_args__ = {"schema": "public"}

    contextual_telemetry_id = Column(UUID(as_uuid=True), primary_key=True)
    version_id = Column(
        BigInteger, ForeignKey("public.plugin_version.version_id"), nullable=False
    )
    trigger_type_id = Column(
        BigInteger, ForeignKey("public.trigger_type.trigger_type_id"), nullable=False
    )
    language_id = Column(
        BigInteger,
        ForeignKey("public.programming_language.language_id"),
        nullable=False,
    )
    file_path = Column(Text)
    caret_line = Column(Integer)
    document_char_length = Column(Integer)
    relative_document_position = Column(Double)


class BehavioralTelemetry(Base):
    __tablename__ = "behavioral_telemetry"
    __table_args__ = {"schema": "public"}

    behavioral_telemetry_id = Column(UUID(as_uuid=True), primary_key=True)
    time_since_last_shown = Column(BigInteger)
    time_since_last_accepted = Column(BigInteger)
    typing_speed = Column(Double)


class Project(Base):
    __tablename__ = "project"
    __table_args__ = {"schema": "public"}

    project_id = Column(UUID(as_uuid=True), primary_key=True)
    project_name = Column(String, nullable=False)
    multi_file_contexts = Column(Text, default="{}")
    multi_file_context_changes = Column(Text, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False)

    sessions = relationship(
        "Session", secondary="public.session_projects", back_populates="projects"
    )


class ProjectUser(Base):
    __tablename__ = "project_users"
    __table_args__ = {"schema": "public"}

    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.project.project_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.user.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    joined_at = Column(DateTime(timezone=True), nullable=False)


class Session(Base):
    __tablename__ = "session"
    __table_args__ = {"schema": "public"}

    session_id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("public.user.user_id", ondelete="SET NULL")
    )
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True))

    projects = relationship(
        "Project", secondary="public.session_projects", back_populates="sessions"
    )


class SessionProject(Base):
    __tablename__ = "session_projects"
    __table_args__ = {"schema": "public"}

    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.session.session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.project.project_id", ondelete="CASCADE"),
        primary_key=True,
    )


class Chat(Base):
    __tablename__ = "chat"
    __table_args__ = {"schema": "public"}

    chat_id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.project.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.user.user_id", ondelete="SET NULL"),
    )
    title = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class MetaQuery(Base):
    __tablename__ = "meta_query"
    __table_args__ = (
        CheckConstraint("query_type IN ('chat', 'completion')"),
        {"schema": "public"},
    )

    meta_query_id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("public.user.user_id", ondelete="SET NULL")
    )
    contextual_telemetry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.contextual_telemetry.contextual_telemetry_id"),
    )
    behavioral_telemetry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.behavioral_telemetry.behavioral_telemetry_id"),
    )
    context_id = Column(UUID(as_uuid=True), ForeignKey("public.context.context_id"))
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("public.session.session_id"), nullable=False
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.project.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    multi_file_context_changes_indexes = Column(Text, default="{}")
    timestamp = Column(DateTime(timezone=True), nullable=False)
    total_serving_time = Column(Integer)
    server_version_id = Column(BigInteger)
    query_type = Column(String, nullable=False)


class CompletionQuery(Base):
    __tablename__ = "completion_query"
    __table_args__ = {"schema": "public"}

    meta_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.meta_query.meta_query_id", ondelete="CASCADE"),
        primary_key=True,
    )


class ChatQuery(Base):
    __tablename__ = "chat_query"
    __table_args__ = {"schema": "public"}

    meta_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.meta_query.meta_query_id", ondelete="CASCADE"),
        primary_key=True,
    )
    chat_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.chat.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    web_enabled = Column(Boolean, default=False, nullable=False)


class HadGeneration(Base):
    __tablename__ = "had_generation"
    __table_args__ = (
        PrimaryKeyConstraint("meta_query_id", "model_id"),
        {"schema": "public"},
    )

    meta_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.meta_query.meta_query_id", ondelete="CASCADE"),
    )
    model_id = Column(BigInteger, ForeignKey("public.model_name.model_id"))
    completion = Column(Text, nullable=False)
    generation_time = Column(Integer, nullable=False)
    shown_at = Column(ARRAY(DateTime(timezone=True)), nullable=False)
    was_accepted = Column(Boolean, nullable=False)
    confidence = Column(Double, nullable=False)
    logprobs = Column(ARRAY(Double), nullable=False)


class GroundTruth(Base):
    __tablename__ = "ground_truth"
    __table_args__ = (
        PrimaryKeyConstraint("completion_query_id", "truth_timestamp"),
        {"schema": "public"},
    )

    completion_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.completion_query.meta_query_id", ondelete="CASCADE"),
    )
    truth_timestamp = Column(DateTime(timezone=True), nullable=False)
    ground_truth = Column(Text, nullable=False)


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
