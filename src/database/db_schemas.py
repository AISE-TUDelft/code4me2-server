"""
SQLAlchemy ORM models for Code4meV2 database schema.

This module defines all database tables and relationships for the Code4meV2 application,
including user management, project collaboration, AI completions, telemetry tracking,
and chat functionality.

The schema supports:
- User authentication and preferences
- Project-based collaboration with multi-file contexts
- AI model completions and chat interactions
- Comprehensive telemetry collection
- Session management and tracking
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .db import Base

# Default user preferences for new accounts
# Controls data collection and storage behaviors
DEFAULT_USER_PREFERENCE = {
    "store_context": False,  # Whether to store code context
    "store_contextual_telemetry": True,  # Whether to collect contextual data
    "store_behavioral_telemetry": True,  # Whether to collect behavioral data
}


class Config(Base):
    """
    Configuration settings storage table.

    Stores JSON configuration data that can be referenced by users or system settings.
    Used for flexible configuration management without schema changes.
    """

    __tablename__ = "config"
    __table_args__ = {"schema": "public"}

    config_id = Column(
        BigInteger, primary_key=True
    )  # Auto-incrementing config identifier
    config_data = Column(Text, nullable=False)  # JSON configuration data


class User(Base):
    """
    User account information and authentication data.

    Central table for user management, storing authentication credentials,
    preferences, and linking to user-specific configurations.
    Supports both standard registration and OAuth signup flows.
    """

    __tablename__ = "user"
    __table_args__ = (
        # Index on email for fast login lookups
        Index("idx_user_email", "email"),
        # Index on config_id for efficient joins
        Index("idx_user_config_id", "config_id"),
        {"schema": "public"},
    )

    user_id = Column(UUID(as_uuid=True), primary_key=True)  # Unique user identifier
    joined_at = Column(
        DateTime(timezone=True), nullable=False
    )  # Account creation timestamp
    email = Column(String, unique=True, nullable=False)  # User email (unique login)
    name = Column(String, nullable=False)  # Display name
    password = Column(String, nullable=False)  # Hashed password
    is_oauth_signup = Column(
        Boolean, server_default="false", default=False
    )  # OAuth vs standard signup
    verified = Column(
        Boolean, server_default="false", default=False
    )  # Email verification status
    config_id = Column(
        BigInteger, ForeignKey("public.config.config_id"), nullable=False
    )  # Reference to user configuration
    preference = Column(Text)  # JSON user preferences
    auth_token = Column(
        UUID(as_uuid=True), nullable=True
    )  # Current authentication token

    # Relationship to configuration data
    config = relationship("Config")


class ModelName(Base):
    """
    Available AI models for code completion and chat.

    Registry of all AI models that can be used for generating completions,
    including metadata about their capabilities and training.
    """

    __tablename__ = "model_name"
    __table_args__ = {"schema": "public"}

    model_id = Column(BigInteger, primary_key=True)  # Unique model identifier
    model_name = Column(Text, nullable=False)  # Human-readable model name
    meta_data = Column(Text, nullable=False)
    is_instruction_tuned = Column(
        Boolean, server_default="false", nullable=False, default=False
    )  # Whether model is fine-tuned for following instructions


class PluginVersion(Base):
    """
    IDE plugin version tracking.

    Tracks different versions of the Code4me plugin across various IDEs,
    enabling version-specific behavior and compatibility management.
    """

    __tablename__ = "plugin_version"
    __table_args__ = {"schema": "public"}

    version_id = Column(BigInteger, primary_key=True)  # Unique version identifier
    version_name = Column(Text, nullable=False)  # Version string (e.g., "1.2.3")
    ide_type = Column(Text, nullable=False)  # IDE name (VSCode, IntelliJ, etc.)
    description = Column(Text)  # Version description/changelog


class TriggerType(Base):
    """
    Code completion trigger types.

    Defines the different ways code completion can be triggered
    (manual, automatic, on keystroke, etc.) for telemetry analysis.
    """

    __tablename__ = "trigger_type"
    __table_args__ = {"schema": "public"}

    trigger_type_id = Column(
        BigInteger, primary_key=True
    )  # Unique trigger type identifier
    trigger_type_name = Column(Text, nullable=False)  # Trigger type name


class ProgrammingLanguage(Base):
    """
    Supported programming languages.

    Registry of programming languages supported by the system,
    used for language-specific completion and analysis.
    """

    __tablename__ = "programming_language"
    __table_args__ = {"schema": "public"}

    language_id = Column(BigInteger, primary_key=True)  # Unique language identifier
    language_name = Column(
        Text, nullable=False
    )  # Language name (Python, JavaScript, etc.)


class Context(Base):
    """
    Code context information for completions.

    Stores the surrounding code context when a completion is requested,
    including prefix/suffix code, file information, and selected text.
    Essential for providing relevant AI completions.
    """

    __tablename__ = "context"
    __table_args__ = {"schema": "public"}

    context_id = Column(
        UUID(as_uuid=True), primary_key=True
    )  # Unique context identifier
    prefix = Column(Text)  # Code before cursor position
    suffix = Column(Text)  # Code after cursor position
    file_name = Column(Text)  # Name of the file being edited
    selected_text = Column(Text)  # Currently selected text (if any)


class ContextualTelemetry(Base):
    """
    Contextual telemetry data for completion requests.

    Captures environmental context when completions are requested,
    including plugin version, trigger type, programming language,
    and cursor position information for analysis and improvement.
    """

    __tablename__ = "contextual_telemetry"
    __table_args__ = (
        # Indexes for efficient filtering and analysis
        Index("idx_ctxt_telemetry_version_id", "version_id"),
        Index("idx_ctxt_telemetry_trigger_type_id", "trigger_type_id"),
        Index("idx_ctxt_telemetry_language_id", "language_id"),
        {"schema": "public"},
    )

    contextual_telemetry_id = Column(
        UUID(as_uuid=True), primary_key=True
    )  # Unique telemetry record
    version_id = Column(
        BigInteger, ForeignKey("public.plugin_version.version_id"), nullable=False
    )  # Plugin version used
    trigger_type_id = Column(
        BigInteger, ForeignKey("public.trigger_type.trigger_type_id"), nullable=False
    )  # How completion was triggered
    language_id = Column(
        BigInteger,
        ForeignKey("public.programming_language.language_id"),
        nullable=False,
    )  # Programming language being used
    file_path = Column(Text)  # Full path to the file
    caret_line = Column(Integer)  # Line number of cursor
    document_char_length = Column(Integer)  # Total characters in document
    relative_document_position = Column(
        Double
    )  # Cursor position as percentage of document


class BehavioralTelemetry(Base):
    """
    User behavioral telemetry data.

    Tracks user interaction patterns and typing behavior to improve
    completion timing and relevance. Used for personalization and
    system optimization.
    """

    __tablename__ = "behavioral_telemetry"
    __table_args__ = {"schema": "public"}

    behavioral_telemetry_id = Column(
        UUID(as_uuid=True), primary_key=True
    )  # Unique behavioral record
    time_since_last_shown = Column(
        BigInteger
    )  # Milliseconds since last completion shown
    time_since_last_accepted = Column(
        BigInteger
    )  # Milliseconds since last completion accepted
    typing_speed = Column(Double)  # Characters per minute typing speed


class Project(Base):
    """
    Project workspace information.

    Represents a coding project that can contain multiple files and contexts.
    Supports multi-file context storage for better AI completions across
    related files in the same project.
    """

    __tablename__ = "project"
    __table_args__ = {"schema": "public"}

    project_id = Column(
        UUID(as_uuid=True), primary_key=True
    )  # Unique project identifier
    project_name = Column(String, nullable=False)  # Human-readable project name
    multi_file_contexts = Column(
        Text, server_default="{}", default="{}"
    )  # JSON: related file contexts
    multi_file_context_changes = Column(
        Text, server_default="{}", default="{}"
    )  # JSON: context change history
    created_at = Column(
        DateTime(timezone=True), nullable=False
    )  # Project creation timestamp

    # Many-to-many relationship with sessions through junction table
    sessions = relationship(
        "Session", secondary="public.session_projects", back_populates="projects"
    )


class ProjectUser(Base):
    """
    Project membership and access control.

    Junction table managing which users have access to which projects,
    supporting collaborative development with timestamp tracking.
    """

    __tablename__ = "project_users"
    __table_args__ = (
        # Indexes for efficient membership queries
        Index("idx_project_users_project_id", "project_id"),
        Index("idx_project_users_user_id", "user_id"),
        {"schema": "public"},
    )

    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.project.project_id", ondelete="CASCADE"
        ),  # Cascade delete when project removed
        primary_key=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.user.user_id", ondelete="CASCADE"
        ),  # Cascade delete when user removed
        primary_key=True,
    )
    joined_at = Column(
        DateTime(timezone=True), nullable=False
    )  # When user joined the project


class Session(Base):
    """
    User coding sessions.

    Tracks individual coding sessions, which can span multiple projects.
    Used for analytics, billing, and understanding usage patterns.
    Sessions are automatically created and managed by the system.
    """

    __tablename__ = "session"
    __table_args__ = (
        Index("idx_session_user_id", "user_id"),  # Index for user session queries
        {"schema": "public"},
    )

    session_id = Column(
        UUID(as_uuid=True), primary_key=True
    )  # Unique session identifier
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("public.user.user_id", ondelete="SET NULL")
    )  # Session owner (nullable for anonymous sessions)
    start_time = Column(
        DateTime(timezone=True), nullable=False
    )  # Session start timestamp
    end_time = Column(DateTime(timezone=True))  # Session end timestamp (null if active)

    # Many-to-many relationship with projects through junction table
    projects = relationship(
        "Project", secondary="public.session_projects", back_populates="sessions"
    )


class SessionProject(Base):
    """
    Session-Project association table.

    Junction table linking sessions to projects, allowing a single session
    to work across multiple projects and tracking project usage patterns.
    """

    __tablename__ = "session_projects"
    __table_args__ = (
        # Indexes for efficient session-project queries
        Index("idx_session_projects_session_id", "session_id"),
        Index("idx_session_projects_project_id", "project_id"),
        {"schema": "public"},
    )

    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.session.session_id", ondelete="CASCADE"
        ),  # Cascade when session deleted
        primary_key=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.project.project_id", ondelete="CASCADE"
        ),  # Cascade when project deleted
        primary_key=True,
    )


class Chat(Base):
    """
    Chat conversations within projects.

    Represents chat conversations between users and AI within the context
    of a specific project. Each chat has a title and belongs to both
    a project and a user.
    """

    __tablename__ = "chat"
    __table_args__ = (
        # Indexes for efficient chat queries
        Index("idx_chat_project_id", "project_id"),
        Index("idx_chat_user_id", "user_id"),
        {"schema": "public"},
    )

    chat_id = Column(UUID(as_uuid=True), primary_key=True)  # Unique chat identifier
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.project.project_id", ondelete="CASCADE"
        ),  # Chat deleted with project
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.user.user_id", ondelete="SET NULL"
        ),  # Chat preserved if user deleted
    )
    title = Column(String, nullable=False)  # Chat conversation title
    created_at = Column(
        DateTime(timezone=True), nullable=False
    )  # Chat creation timestamp


class MetaQuery(Base):
    """
    Base query information for both completions and chats.

    Central table that captures common information for all AI queries,
    whether they are code completions or chat messages. Links to
    telemetry data and provides query type discrimination.
    """

    __tablename__ = "meta_query"
    __table_args__ = (
        # Constraint ensuring query_type is valid
        CheckConstraint("query_type IN ('chat', 'completion')"),
        # Indexes for efficient query filtering and analysis
        Index("idx_meta_query_user_id", "user_id"),
        Index("idx_meta_query_project_id", "project_id"),
        Index("idx_meta_query_session_id", "session_id"),
        Index("idx_meta_query_type", "query_type"),
        {"schema": "public"},
    )

    meta_query_id = Column(
        UUID(as_uuid=True), primary_key=True
    )  # Unique query identifier
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("public.user.user_id", ondelete="SET NULL")
    )  # Query originator (nullable for anonymous)
    contextual_telemetry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.contextual_telemetry.contextual_telemetry_id"),
    )  # Associated contextual telemetry
    behavioral_telemetry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.behavioral_telemetry.behavioral_telemetry_id"),
    )  # Associated behavioral telemetry
    context_id = Column(
        UUID(as_uuid=True), ForeignKey("public.context.context_id")
    )  # Code context
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("public.session.session_id"), nullable=False
    )  # Session containing this query
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.project.project_id", ondelete="CASCADE"),
        nullable=False,
    )  # Project context for the query
    multi_file_context_changes_indexes = Column(
        Text, server_default="{}", default="{}"
    )  # JSON: context changes
    timestamp = Column(DateTime(timezone=True), nullable=False)  # Query timestamp
    total_serving_time = Column(Integer)  # Total time to serve response (ms)
    server_version_id = Column(BigInteger)  # Server version that processed query
    query_type = Column(String, nullable=False)  # 'chat' or 'completion'


class CompletionQuery(Base):
    """
    Code completion specific query data.

    Extends MetaQuery for code completion requests. Uses the same ID
    as the parent MetaQuery record for 1:1 relationship.
    """

    __tablename__ = "completion_query"
    __table_args__ = {"schema": "public"}

    meta_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.meta_query.meta_query_id", ondelete="CASCADE"
        ),  # Cascade delete with meta query
        primary_key=True,
    )


class ChatQuery(Base):
    """
    Chat message specific query data.

    Extends MetaQuery for chat messages, linking to the specific
    chat conversation and including chat-specific settings.
    """

    __tablename__ = "chat_query"
    __table_args__ = (
        Index("idx_chat_query_chat_id", "chat_id"),  # Index for chat message queries
        {"schema": "public"},
    )

    meta_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.meta_query.meta_query_id", ondelete="CASCADE"
        ),  # Cascade delete with meta query
        primary_key=True,
    )
    chat_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.chat.chat_id", ondelete="CASCADE"
        ),  # Chat containing this message
        nullable=False,
    )
    web_enabled = Column(
        Boolean, server_default="false", nullable=False, default=False
    )  # Whether web search is enabled


class HadGeneration(Base):
    """
    AI model generations for queries.

    Stores the actual AI-generated responses for queries, including
    completion text, performance metrics, user interaction data,
    and model confidence scores. Supports multiple generations per query.
    """

    __tablename__ = "had_generation"
    __table_args__ = (
        # Composite primary key for query-model combinations
        PrimaryKeyConstraint("meta_query_id", "model_id"),
        # Index for efficient query-model lookups
        Index("idx_had_generation_meta_query_model", "meta_query_id", "model_id"),
        {"schema": "public"},
    )

    meta_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.meta_query.meta_query_id", ondelete="CASCADE"
        ),  # Cascade delete with query
    )
    model_id = Column(
        BigInteger, ForeignKey("public.model_name.model_id")
    )  # Model that generated response
    completion = Column(Text, nullable=False)  # Generated completion text
    generation_time = Column(Integer, nullable=False)  # Time to generate (milliseconds)
    shown_at = Column(
        ARRAY(DateTime(timezone=True)), nullable=False
    )  # Array of timestamps when shown to user
    was_accepted = Column(
        Boolean, nullable=False
    )  # Whether user accepted the completion
    confidence = Column(Double, nullable=False)  # Model confidence score
    logprobs = Column(ARRAY(Double), nullable=False)  # Log probabilities for tokens


class GroundTruth(Base):
    """
    Ground truth data for completion evaluation.

    Stores the actual code that users wrote after completion requests,
    used for evaluating and improving model performance. Multiple
    ground truth records can exist per completion with timestamps.
    """

    __tablename__ = "ground_truth"
    __table_args__ = (
        # Composite primary key for completion-timestamp combinations
        PrimaryKeyConstraint("completion_query_id", "truth_timestamp"),
        # Index for efficient completion-timestamp queries
        Index(
            "idx_ground_truth_completion_query_timestamp",
            "completion_query_id",
            "truth_timestamp",
        ),
        {"schema": "public"},
    )

    completion_query_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.completion_query.meta_query_id", ondelete="CASCADE"
        ),  # Cascade delete with completion
    )
    truth_timestamp = Column(
        DateTime(timezone=True), nullable=False
    )  # When ground truth was captured
    ground_truth = Column(Text, nullable=False)  # Actual code written by user
    truth_timestamp = Column(DateTime(timezone=True), nullable=False)
    ground_truth = Column(Text, nullable=False)


class Documentation(Base):
    __tablename__ = "documentation"
    __table_args__ = (
        Index("idx_documentation_language", "language"),
        Index("idx_documentation_embedding", "embedding", postgresql_using="ivfflat"),
        {"schema": "public"},
    )

    documentation_id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    language = Column(String(50), nullable=False)
    embedding = Column(
        Vector(384), nullable=True
    )  # 384 dimensions for all-MiniLM-L6-v2
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)


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
