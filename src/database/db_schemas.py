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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .db import Base

# Default user preferences for new accounts
# Controls data collection and storage behaviors
DEFAULT_USER_PREFERENCE = {
    "store_context": False,  # Whether to store code context
    "store_contextual_telemetry": True,  # Whether to collect contextual data
    "store_behavioral_telemetry": True,  # Whether to collect behavioral data
    # Whether to store agent task/event/edit *content* (message text, tool
    # arguments/results, diffs). Structural telemetry (tokens, latency, span
    # tree, tool names) is always stored and is not covered by this flag.
    #
    # Default ON: this is a research platform where study participants give
    # informed consent through a separate process before using it, so opt-out
    # is the right default for research data collection. The *mechanism* stays
    # strict — enforcement is server-side only (see
    # backend.routers.agent.consent.resolve_store_agent_content); a
    # client-supplied flag is never trusted.
    "store_agent_content": True,
}

# Preference key gating persistence of agent content columns.
STORE_AGENT_CONTENT_KEY = "store_agent_content"
# Server-side default applied when a user row predates this preference (i.e. the
# key is absent from their stored preference JSON).
STORE_AGENT_CONTENT_DEFAULT = True


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
        # Index on admin status for efficient filtering
        Index("idx_user_is_admin", "is_admin"),
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
    is_admin = Column(
        Boolean, server_default="false", default=False
    )  # Admin status for access control

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
    prompt_templates = Column(Text, nullable=False)
    model_parameters = Column(Text, nullable=False)
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


class Study(Base):
    """
    A/B testing and user experiment management.
    
    Manages user studies for configuration testing and analysis.
    Only one study can be active at a time per server instance.
    """
    
    __tablename__ = "study"
    __table_args__ = (
        Index("idx_study_is_active", "is_active"),
        Index("idx_study_created_by", "created_by"),
        Index("idx_study_starts_at", "starts_at"),
        Index("idx_study_ends_at", "ends_at"),
        {"schema": "public"},
    )
    
    study_id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(Text, nullable=False)
    description = Column(Text)
    created_by = Column(UUID(as_uuid=True), ForeignKey("public.user.user_id"), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, server_default="false", default=False)
    default_config_id = Column(BigInteger, ForeignKey("public.config.config_id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)
    
    # Relationships
    creator = relationship("User")
    default_config = relationship("Config")


class ConfigAssignmentHistory(Base):
    """
    Tracks user assignments to different configurations during studies.
    
    Maintains history of which users were assigned which configurations
    for later analysis and cross-referencing.
    """
    
    __tablename__ = "config_assignment_history"
    __table_args__ = (
        Index("idx_config_assignment_user_id", "user_id"),
        Index("idx_config_assignment_study_id", "study_id"),
        Index("idx_config_assignment_assigned_at", "assigned_at"),
        {"schema": "public"},
    )
    
    user_id = Column(UUID(as_uuid=True), ForeignKey("public.user.user_id"), primary_key=True)
    study_id = Column(UUID(as_uuid=True), ForeignKey("public.study.study_id"), primary_key=True)
    assigned_config_id = Column(BigInteger, ForeignKey("public.config.config_id"), nullable=False)
    assigned_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)
    
    # Relationships
    user = relationship("User")
    study = relationship("Study")
    assigned_config = relationship("Config")


# ── Agent tables ──────────────────────────────────────────────────────────────
#
# One schema serves both agent runtimes:
#
#   * the built-in ``code4me2-agent`` ReAct loop, which runs locally and
#     self-reports its steps to POST /api/agent/events/ingest, and
#   * third-party ACP agents (Goose, Codex), which are observed transparently
#     by the plugin's local proxy relaying through POST /api/agent/inference.
#
# Shape: "columnar core + JSON overflow". Fields both runtimes produce get
# first-class typed columns (span tree, token counts, latency, tool names,
# finish reason); runtime-specific or experimental extras go into the single
# ``extra_json`` overflow column so adding an adapter never needs a migration.
#
# Privacy split: structural columns (status, timings, token counts, decisions)
# are always stored. Content columns (task_description, first_system_message,
# last_user_message, response_text, tool_arguments, tool_result, payload_json,
# diff_text, edit_delta_json) are nullable and only ever written when the
# requesting user's ``store_agent_content`` preference resolves True — checked
# server-side, never taken from a client-supplied flag.


class AgentProfile(Base):
    """Named experiment variant — defines runtime, provider, tools and policy.

    ``framework_version`` selects which agent runtime the profile targets
    (``code4me2-agent``, ``goose``, ``codex``), and therefore which telemetry
    path a task created from it will use.

    The provider triple (``base_url``, ``api_key_ref``, ``model``) is
    deliberately generic: any endpoint speaking the OpenAI-compatible
    chat-completions wire format works, so a local Ollama install, Groq,
    OpenRouter and OpenAI itself are all a config change rather than a code
    change. ``base_url`` NULL falls back to the server's configured default
    (see ``agents.provider.resolve_upstream``).
    """

    __tablename__ = "agent_profile"
    __table_args__ = (
        Index("idx_agent_profile_is_active", "is_active"),
        {"schema": "public"},
    )

    profile_id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String, unique=True, nullable=False)
    model = Column(String, nullable=False)
    # Agent runtime this profile targets: code4me2-agent | goose | codex
    framework_version = Column(
        String, nullable=False, server_default="code4me2-agent"
    )
    # OpenAI-compatible base URL, e.g. http://localhost:11434/v1 (Ollama),
    # https://api.groq.com/openai/v1, https://api.openai.com/v1. NULL = server default.
    base_url = Column(String, nullable=True)
    # Name of the environment variable holding the upstream API key — never the
    # key itself, so profiles stay safe to dump from the admin UI or the DB.
    api_key_ref = Column(String, nullable=True)
    tools_json = Column(Text, nullable=False)  # JSON array of tool names
    approval_policy = Column(String, nullable=False)  # suggestion_only | per_step | …
    max_steps = Column(Integer, nullable=False)
    # Sampling temperature injected into the upstream request, server-side, the same
    # way `model` is overridden. NULL = don't inject; let the provider use its default.
    temperature = Column(Double, nullable=True)
    max_context_tokens = Column(
        Integer, nullable=True
    )  # per-turn rolling window; NULL = model max
    # Only active profiles are candidate arms for new A/B assignments. Inactive
    # profiles stay in the table (drafts, or arms retired mid-study) and existing
    # users keep any assignment already pinned to them.
    is_active = Column(Boolean, server_default="true", default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class AgentProfileAssignment(Base):
    """Sticky user→profile mapping for A/B testing.

    Resolution is server-authoritative: on a user's first agent task we draw a
    random active profile and persist it here, so the user keeps the same arm for
    the life of the experiment regardless of later changes to the active set.
    `source` distinguishes randomized assignments ("auto") from admin overrides
    ("manual"); manual rows are excluded from re-rolls and from A/B analysis.
    """

    __tablename__ = "agent_profile_assignment"
    __table_args__ = (
        Index("idx_agent_profile_assignment_profile_id", "profile_id"),
        {"schema": "public"},
    )

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.user.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.agent_profile.profile_id", ondelete="CASCADE"),
        nullable=False,
    )
    source = Column(String, nullable=False, server_default="auto")  # auto | manual
    assigned_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)

    # Relationships
    user = relationship("User")
    profile = relationship("AgentProfile")


class AgentTask(Base):
    """One row per agent session a developer starts.

    A "session" here is one run of whichever agent runtime the assigned profile
    selects — Goose, Codex, or the built-in ``code4me2-agent`` ReAct loop.

    The profile's config (model, temperature, approval_policy, tools_json) is
    *snapshotted* onto this row at creation time, so later edits to the profile
    never retroactively change the conditions a completed task ran under.
    """

    __tablename__ = "agent_task"
    __table_args__ = (
        Index("idx_agent_task_status", "status"),
        Index("idx_agent_task_session_id", "session_id"),
        Index("idx_agent_task_owner_user_id", "owner_user_id"),
        {"schema": "public"},
    )

    task_id = Column(UUID(as_uuid=True), primary_key=True)
    # nullable — populated when the task is minted via the authenticated plugin endpoint
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("public.session.session_id"), nullable=True
    )
    # Which path created this task: "plugin" (proxy relay, third-party agents),
    # "code4me2_agent" (self-reporting built-in runtime), or "benchmark".
    source = Column(String, nullable=False, server_default="plugin")
    # Owner attribution for tasks that arrive over the ACP bearer path, where
    # there is no session cookie to resolve the user from.
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("public.user.user_id"), nullable=True
    )
    owner_project_id = Column(UUID(as_uuid=True), nullable=True)
    # The runtime's *own* identifiers, which are not guaranteed to be UUIDs
    # (code4me2-agent uses uuid4().hex; ACP session ids are opaque strings).
    # Kept alongside the server-side UUID PK so self-reported batches can be
    # matched back to their task without forcing the runtime to adopt our ids.
    external_run_id = Column(String, unique=True, nullable=True)
    agent_session_id = Column(String, nullable=True)
    agent_profile = Column(String, nullable=False)
    model = Column(String, nullable=False)
    # Snapshotted from the assigned profile at task-creation time, like `model`, so
    # the value is stable for the life of the task. NULL = provider default.
    temperature = Column(Double, nullable=True)
    approval_policy = Column(String, nullable=False)
    tools_json = Column(Text, nullable=False)
    # Agent runtime (e.g. "goose", "codex", "code4me2-agent") — snapshotted from
    # the profile, but left nullable so the proxy can backfill the concrete
    # version string it observes ("goose 1.x") on the first inference call.
    framework_version = Column(String, nullable=True)
    # Complete, versioned managed-runtime policy. Nullable for legacy tasks.
    policy_snapshot = Column(JSONB, nullable=True)
    status = Column(String, nullable=False)  # pending | running | done | failed
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    total_steps = Column(Integer, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    # Points at the most recent event, so a dashboard can show "where is this
    # task now" without scanning agent_event.
    #
    # agent_task and agent_event reference each other, which is a deliberate
    # cycle. `use_alter=True` tells SQLAlchemy to emit this constraint as a
    # separate ALTER after both tables exist, so metadata.create_all() can
    # order the DDL instead of erroring on the cycle. The Alembic migration
    # does the same thing by hand.
    latest_event_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "public.agent_event.event_id",
            use_alter=True,
            name="agent_task_latest_event_id_fkey",
        ),
        nullable=True,
    )
    # Content — written only when store_agent_content resolves True
    task_description = Column(Text, nullable=True)


class AgentEvent(Base):
    """One row per agent step, from either telemetry path.

    ``event_type`` is the unified vocabulary both paths emit into:
    ``model_call`` and ``tool_call`` are the two that carry typed columns;
    ``thought`` / ``observation`` / ``run_started`` / ``run_completed`` /
    ``decision`` are structural markers.

    Rows form a span tree via ``span_id`` / ``parent_span_id`` (tool calls nest
    under the model_call whose response requested them). Anything a specific
    adapter reports that has no typed column lands in ``extra_json``.
    """

    __tablename__ = "agent_event"
    __table_args__ = (
        Index("idx_agent_event_task_id", "task_id"),
        Index("idx_agent_event_event_type", "event_type"),
        Index("idx_agent_event_created_at", "created_at"),
        {"schema": "public"},
    )

    event_id = Column(UUID(as_uuid=True), primary_key=True)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.agent_task.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_index = Column(Integer, nullable=False)
    # model_call | tool_call | thought | observation | test_run | decision |
    # run_started | run_completed | accepted | rejected
    event_type = Column(String, nullable=False)
    # Which telemetry path wrote this row: "proxy" (client-side relay observing
    # a third-party agent) or "code4me2_agent" (runtime self-report).
    source = Column(String, nullable=True)
    # Envelope schema version reported by a self-reporting runtime, so old
    # batches stay interpretable after the envelope evolves.
    schema_version = Column(String, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    # Client-side event timestamp, as reported by a self-reporting runtime.
    # Distinct from created_at, which is when the server persisted the row.
    occurred_at = Column(DateTime(timezone=True), nullable=True)

    # Shared span identifiers (model_call and tool_call).
    # Events are grouped by task via task_id; there is no separate trace_id column —
    # it was always a duplicate of task_id.
    span_id = Column(String, nullable=True)
    parent_span_id = Column(UUID(as_uuid=True), nullable=True)
    # Correlates every event emitted while serving one user prompt.
    request_id = Column(String, nullable=True)
    chat_session_index = Column(Integer, nullable=True)

    # ── model_call fields — null for tool_call rows ──
    model = Column(String, nullable=True)
    agent_profile = Column(String, nullable=True)
    streaming = Column(Boolean, nullable=True)
    message_count = Column(Integer, nullable=True)
    # Per-role message counts (the shape of the context window).
    role_system_count = Column(Integer, nullable=True)
    role_user_count = Column(Integer, nullable=True)
    role_assistant_count = Column(Integer, nullable=True)
    role_tool_count = Column(Integer, nullable=True)
    tools_kept = Column(Integer, nullable=True)
    tools_stripped = Column(Integer, nullable=True)
    tool_names_requested = Column(ARRAY(String), nullable=True)
    max_tokens = Column(Integer, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    finish_reason = Column(String, nullable=True)
    upstream_status = Column(Integer, nullable=True)
    step_index = Column(Integer, nullable=True)
    context_window_size_bytes = Column(Integer, nullable=True)
    active_file = Column(Text, nullable=True)
    first_message_hash = Column(String, nullable=True)
    chat_new_session_detected = Column(Boolean, nullable=True)
    experiment_tool_access_enabled = Column(Boolean, nullable=True)
    experiment_approval_policy = Column(String, nullable=True)
    # Content — written only when store_agent_content resolves True
    first_system_message = Column(Text, nullable=True)
    last_user_message = Column(Text, nullable=True)
    response_text = Column(Text, nullable=True)

    # ── tool_call fields — null for model_call rows ──
    tool_name = Column(String, nullable=True)
    tool_arguments_length = Column(Integer, nullable=True)
    tool_result_length = Column(Integer, nullable=True)
    # Content — written only when store_agent_content resolves True
    tool_arguments = Column(Text, nullable=True)
    tool_result = Column(Text, nullable=True)

    # ── JSON overflow ──
    # Adapter-specific / experimental structural metrics that don't warrant a
    # typed column (e.g. the ReAct adapter's per-iteration counters, an
    # adapter's own observability block). Never content — see payload_json.
    extra_json = Column(Text, nullable=True)
    # Free-form event payload from a self-reporting runtime. May quote user
    # prompts, model output, or file contents, so this is Content — written only
    # when store_agent_content resolves True.
    payload_json = Column(Text, nullable=True)


class AgentEdit(Base):
    """One row per file the agent proposed changing.

    The accept / reject / modified decision plus the diff is the
    human-in-the-loop signal: it records not just what the agent suggested but
    what the developer actually did with it.
    """

    __tablename__ = "agent_edit"
    __table_args__ = (
        Index("idx_agent_edit_task_id", "task_id"),
        {"schema": "public"},
    )

    edit_id = Column(UUID(as_uuid=True), primary_key=True)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.agent_task.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path = Column(Text, nullable=False)
    was_accepted = Column(Boolean, nullable=True)  # True/False after developer decides
    was_modified = Column(
        Boolean, nullable=True
    )  # True if developer edited before accepting
    decided_at = Column(DateTime, nullable=True)
    # Content — written only when store_agent_content resolves True
    diff_text = Column(Text, nullable=True)
    edit_delta_json = Column(
        Text, nullable=True
    )  # what developer changed before accepting


class AgentMemory(Base):
    """Durable per-session agent memory snapshot.

    Holds the latest serialized memory window for one agent chat session, so
    conversation state survives an IDE (or agent process) restart. Ownership is
    server-derived from the ACP token, matching agent telemetry authorization —
    the client cannot claim a session it doesn't own.
    """

    __tablename__ = "agent_memory"
    __table_args__ = (
        Index("idx_agent_memory_owner_user_id", "owner_user_id"),
        Index("idx_agent_memory_owner_project_id", "owner_project_id"),
        Index("idx_agent_memory_updated_at", "updated_at"),
        {"schema": "public"},
    )

    # ACP session ids are opaque strings, not guaranteed to be UUIDs.
    session_id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False)
    owner_project_id = Column(String, nullable=False)
    memory_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)


class StudyAgentProfile(Base):
    """Links a study to the agent profiles that serve as its experiment arms.

    A study with rows here drives agent-profile A/B assignment: its selected
    profiles become the candidate pool for new auto-draws (see
    ``registry.resolve_assignment``). Studies with no rows here are
    completion-only and behave exactly as before this table existed.

    ``is_baseline`` marks the arm that agent-evaluation uplift is measured
    against (analogous to ``study.default_config_id`` on the completion side).
    """

    __tablename__ = "study_agent_profile"
    __table_args__ = (
        Index("idx_study_agent_profile_profile_id", "profile_id"),
        {"schema": "public"},
    )

    study_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.study.study_id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("public.agent_profile.profile_id"),
        primary_key=True,
    )
    is_baseline = Column(Boolean, server_default="false", default=False, nullable=False)

    # Relationships
    study = relationship("Study")
    profile = relationship("AgentProfile")


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
