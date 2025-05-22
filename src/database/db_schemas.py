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


class User(Base):
    __tablename__ = "user"
    __table_args__ = {"extend_existing": True}
    user_id = Column(UUID, unique=True, primary_key=True, nullable=False)
    joined_at = Column(DateTime(timezone=True), nullable=False)
    email = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    password = Column(String, nullable=False)
    is_oauth_signup = Column(Boolean, default=False)
    verified = Column(Boolean, default=False)

    # New field for future designs
    queries = relationship("Query", back_populates="user")


class Query(Base):
    __tablename__ = "query"
    query_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.user_id"), index=True)
    telemetry_id = Column(UUID(as_uuid=True), ForeignKey("telemetry.telemetry_id"))
    context_id = Column(UUID(as_uuid=True), ForeignKey("context.context_id"))
    total_serving_time = Column(Integer)
    timestamp = Column(DateTime(timezone=True))
    server_version_id = Column(Integer)

    user = relationship("User", back_populates="queries")
    telemetry = relationship("Telemetry", back_populates="queries")
    context = relationship("Context", back_populates="queries")
    had_generations = relationship("HadGeneration", back_populates="query")
    ground_truths = relationship("GroundTruth", back_populates="query")

    __table_args__ = (
        UniqueConstraint("user_id", "query_id", name="unique_user_query"),
        Index("idx_query_user_id", "user_id"),
        Index("idx_query_query_id", "query_id"),
    )


class ModelName(Base):
    __tablename__ = "model_name"
    model_id = Column(BIGINT, primary_key=True, nullable=False, autoincrement=True)
    model_name = Column(Text, nullable=False)

    had_generations = relationship("HadGeneration", back_populates="model")


class PluginVersion(Base):
    __tablename__ = "plugin_version"
    version_id = Column(BIGINT, primary_key=True, nullable=False, autoincrement=True)
    version_name = Column(Text, nullable=False)
    ide_type = Column(Text, nullable=False)
    description = Column(Text)

    contexts = relationship("Context", back_populates="version")


class TriggerType(Base):
    __tablename__ = "trigger_type"
    trigger_type_id = Column(
        BIGINT, primary_key=True, nullable=False, autoincrement=True
    )
    trigger_type_name = Column(Text, nullable=False)

    contexts = relationship("Context", back_populates="trigger_type")


class ProgrammingLanguage(Base):
    __tablename__ = "programming_language"
    language_id = Column(BIGINT, primary_key=True, nullable=False, autoincrement=True)
    language_name = Column(Text, nullable=False)

    contexts = relationship("Context", back_populates="language")


class HadGeneration(Base):
    __tablename__ = "had_generation"
    query_id = Column(
        UUID(as_uuid=True),
        ForeignKey("query.query_id", ondelete="CASCADE"),
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

    query = relationship("Query", back_populates="had_generations")
    model = relationship("ModelName", back_populates="had_generations")

    __table_args__ = (Index("idx_query_id_model_id", "query_id", "model_id"),)


class GroundTruth(Base):
    __tablename__ = "ground_truth"
    query_id = Column(
        UUID(as_uuid=True),
        ForeignKey("query.query_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    truth_timestamp = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    ground_truth = Column(Text, nullable=False)

    query = relationship("Query", back_populates="ground_truths")

    __table_args__ = (
        Index("idx_query_id_truth_timestamp", "query_id", "truth_timestamp"),
    )


class Context(Base):
    __tablename__ = "context"
    context_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    prefix = Column(Text)
    suffix = Column(Text)
    file_name = Column(Text)
    language_id = Column(
        BIGINT, ForeignKey("programming_language.language_id"), index=True
    )
    trigger_type_id = Column(
        BIGINT, ForeignKey("trigger_type.trigger_type_id"), index=True
    )
    version_id = Column(BIGINT, ForeignKey("plugin_version.version_id"), index=True)

    language = relationship("ProgrammingLanguage", back_populates="contexts")
    trigger_type = relationship("TriggerType", back_populates="contexts")
    version = relationship("PluginVersion", back_populates="contexts")
    queries = relationship("Query", back_populates="context")


class Telemetry(Base):
    __tablename__ = "telemetry"
    telemetry_id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    time_since_last_completion = Column(Integer)
    typing_speed = Column(Integer)
    document_char_length = Column(Integer)
    relative_document_position = Column(Float)

    queries = relationship("Query", back_populates="telemetry")

    __table_args__ = (Index("telemetry_id_index", "telemetry_id"),)
