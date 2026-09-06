"""add unified agent tables

Creates the seven tables backing the agent subsystem in their final,
already-unified shape:

  agent_profile             experiment variant: runtime + provider + tools + policy
  agent_profile_assignment  sticky per-user A/B arm
  agent_task                one row per agent session, config snapshotted
  agent_event               one row per step, "columnar core + JSON overflow"
  agent_edit                per-file proposal + accept/reject/modified + diff
  agent_memory              session-scoped memory surviving IDE restarts
  study_agent_profile       links the existing Study A/B framework to agent arms

``agent_event`` gives typed columns to everything both telemetry paths produce
(the client-side proxy observing Goose/Codex, and the built-in code4me2-agent
runtime self-reporting), and routes adapter-specific extras into a single
``extra_json`` overflow column so a new adapter never requires a migration.

Content columns (task_description, first_system_message, last_user_message,
response_text, tool_arguments, tool_result, payload_json, diff_text,
edit_delta_json) are all nullable: they are only ever written when the user's
``store_agent_content`` preference resolves True, enforced server-side.

Revision ID: d1a2b3c4e5f7
Revises: c2dc3e9cc1bc
Create Date: 2026-09-06

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d1a2b3c4e5f7"
down_revision = "c2dc3e9cc1bc"
branch_labels = None
depends_on = None

_SCHEMA = "public"


def upgrade() -> None:
    op.create_table(
        "agent_profile",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        # Which agent runtime this profile targets: code4me2-agent | goose | codex
        sa.Column(
            "framework_version",
            sa.String(),
            nullable=False,
            server_default=sa.text("'code4me2-agent'"),
        ),
        # Generic OpenAI-compatible provider triple. NULL base_url = server default.
        sa.Column("base_url", sa.String(), nullable=True),
        # Name of the env var holding the key — never the key itself.
        sa.Column("api_key_ref", sa.String(), nullable=True),
        sa.Column("tools_json", sa.Text(), nullable=False),
        sa.Column("approval_policy", sa.String(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("temperature", sa.Double(), nullable=True),
        sa.Column("max_context_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("profile_id"),
        sa.UniqueConstraint("name"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_profile_is_active",
        "agent_profile",
        ["is_active"],
        unique=False,
        schema=_SCHEMA,
    )

    op.create_table(
        "agent_profile_assignment",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source", sa.String(), nullable=False, server_default=sa.text("'auto'")
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["public.user.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["public.agent_profile.profile_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_profile_assignment_profile_id",
        "agent_profile_assignment",
        ["profile_id"],
        unique=False,
        schema=_SCHEMA,
    )

    # agent_task and agent_event reference each other (agent_event.task_id →
    # agent_task, agent_task.latest_event_id → agent_event), so the cycle is
    # broken by adding latest_event_id's FK after both tables exist.
    op.create_table(
        "agent_task",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "source", sa.String(), nullable=False, server_default=sa.text("'plugin'")
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_project_id", postgresql.UUID(as_uuid=True), nullable=True),
        # The runtime's own ids, which are not guaranteed to be UUID-shaped.
        sa.Column("external_run_id", sa.String(), nullable=True),
        sa.Column("agent_session_id", sa.String(), nullable=True),
        sa.Column("agent_profile", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("temperature", sa.Double(), nullable=True),
        sa.Column("approval_policy", sa.String(), nullable=False),
        sa.Column("tools_json", sa.Text(), nullable=False),
        sa.Column("framework_version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("total_steps", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latest_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Content — gated by store_agent_content
        sa.Column("task_description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["public.session.session_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["public.user.user_id"]),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("external_run_id"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_task_status", "agent_task", ["status"], unique=False, schema=_SCHEMA
    )
    op.create_index(
        "idx_agent_task_session_id",
        "agent_task",
        ["session_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_task_owner_user_id",
        "agent_task",
        ["owner_user_id"],
        unique=False,
        schema=_SCHEMA,
    )

    op.create_table(
        "agent_event",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        # "proxy" | "code4me2_agent"
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("schema_version", sa.String(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        # Span tree
        sa.Column("span_id", sa.String(), nullable=True),
        sa.Column("parent_span_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("chat_session_index", sa.Integer(), nullable=True),
        # model_call fields
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("agent_profile", sa.String(), nullable=True),
        sa.Column("streaming", sa.Boolean(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=True),
        sa.Column("role_system_count", sa.Integer(), nullable=True),
        sa.Column("role_user_count", sa.Integer(), nullable=True),
        sa.Column("role_assistant_count", sa.Integer(), nullable=True),
        sa.Column("role_tool_count", sa.Integer(), nullable=True),
        sa.Column("tools_kept", sa.Integer(), nullable=True),
        sa.Column("tools_stripped", sa.Integer(), nullable=True),
        sa.Column("tool_names_requested", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.String(), nullable=True),
        sa.Column("upstream_status", sa.Integer(), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("context_window_size_bytes", sa.Integer(), nullable=True),
        sa.Column("active_file", sa.Text(), nullable=True),
        sa.Column("first_message_hash", sa.String(), nullable=True),
        sa.Column("chat_new_session_detected", sa.Boolean(), nullable=True),
        sa.Column("experiment_tool_access_enabled", sa.Boolean(), nullable=True),
        sa.Column("experiment_approval_policy", sa.String(), nullable=True),
        # model_call content — gated by store_agent_content
        sa.Column("first_system_message", sa.Text(), nullable=True),
        sa.Column("last_user_message", sa.Text(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        # tool_call fields
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("tool_arguments_length", sa.Integer(), nullable=True),
        sa.Column("tool_result_length", sa.Integer(), nullable=True),
        # tool_call content — gated by store_agent_content
        sa.Column("tool_arguments", sa.Text(), nullable=True),
        sa.Column("tool_result", sa.Text(), nullable=True),
        # JSON overflow: adapter-specific structural metrics (never content)
        sa.Column("extra_json", sa.Text(), nullable=True),
        # Free-form runtime payload — content, gated by store_agent_content
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"], ["public.agent_task.task_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_event_task_id",
        "agent_event",
        ["task_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_event_event_type",
        "agent_event",
        ["event_type"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_event_created_at",
        "agent_event",
        ["created_at"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_foreign_key(
        "agent_task_latest_event_id_fkey",
        "agent_task",
        "agent_event",
        ["latest_event_id"],
        ["event_id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
    )

    op.create_table(
        "agent_edit",
        sa.Column("edit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("was_accepted", sa.Boolean(), nullable=True),
        sa.Column("was_modified", sa.Boolean(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        # Content — gated by store_agent_content
        sa.Column("diff_text", sa.Text(), nullable=True),
        sa.Column("edit_delta_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"], ["public.agent_task.task_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("edit_id"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_edit_task_id",
        "agent_edit",
        ["task_id"],
        unique=False,
        schema=_SCHEMA,
    )

    op.create_table(
        "agent_memory",
        # ACP session ids are opaque strings, not guaranteed to be UUIDs.
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("owner_project_id", sa.String(), nullable=False),
        sa.Column("memory_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_memory_owner_user_id",
        "agent_memory",
        ["owner_user_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_memory_owner_project_id",
        "agent_memory",
        ["owner_project_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_memory_updated_at",
        "agent_memory",
        ["updated_at"],
        unique=False,
        schema=_SCHEMA,
    )

    op.create_table(
        "study_agent_profile",
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "is_baseline", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.ForeignKeyConstraint(
            ["study_id"], ["public.study.study_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["public.agent_profile.profile_id"]),
        sa.PrimaryKeyConstraint("study_id", "profile_id"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_study_agent_profile_profile_id",
        "study_agent_profile",
        ["profile_id"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_study_agent_profile_profile_id",
        table_name="study_agent_profile",
        schema=_SCHEMA,
    )
    op.drop_table("study_agent_profile", schema=_SCHEMA)

    for index in (
        "idx_agent_memory_updated_at",
        "idx_agent_memory_owner_project_id",
        "idx_agent_memory_owner_user_id",
    ):
        op.drop_index(index, table_name="agent_memory", schema=_SCHEMA)
    op.drop_table("agent_memory", schema=_SCHEMA)

    op.drop_index("idx_agent_edit_task_id", table_name="agent_edit", schema=_SCHEMA)
    op.drop_table("agent_edit", schema=_SCHEMA)

    # Drop the cyclic FK before agent_event, so agent_event is droppable.
    op.drop_constraint(
        "agent_task_latest_event_id_fkey",
        "agent_task",
        type_="foreignkey",
        schema=_SCHEMA,
    )
    for index in (
        "idx_agent_event_created_at",
        "idx_agent_event_event_type",
        "idx_agent_event_task_id",
    ):
        op.drop_index(index, table_name="agent_event", schema=_SCHEMA)
    op.drop_table("agent_event", schema=_SCHEMA)

    for index in (
        "idx_agent_task_owner_user_id",
        "idx_agent_task_session_id",
        "idx_agent_task_status",
    ):
        op.drop_index(index, table_name="agent_task", schema=_SCHEMA)
    op.drop_table("agent_task", schema=_SCHEMA)

    op.drop_index(
        "idx_agent_profile_assignment_profile_id",
        table_name="agent_profile_assignment",
        schema=_SCHEMA,
    )
    op.drop_table("agent_profile_assignment", schema=_SCHEMA)

    op.drop_index(
        "idx_agent_profile_is_active", table_name="agent_profile", schema=_SCHEMA
    )
    op.drop_table("agent_profile", schema=_SCHEMA)
