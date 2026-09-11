"""add experiment execution integrity

Adds study-scoped agent assignments and links new agent tasks to the exact
study arm, profile, and consent decision used at creation time. Task rows keep
their existing copied configuration as the immutable execution snapshot. Event
writes gain a task-local atomic index allocator and database-enforced
identity/order constraints.

Legacy assignments and tasks are intentionally retained without inferred study
attribution. Existing events retain a NULL source_event_id because their source
identity cannot be reconstructed safely.

Revision ID: f3c4d5e6f7a9
Revises: f3c4d5e6a7b9
Create Date: 2026-09-09

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "f3c4d5e6f7a9"
down_revision = "f3c4d5e6a7b9"
branch_labels = None
depends_on = None

_SCHEMA = "public"


def upgrade() -> None:
    op.create_table(
        "agent_study_assignment",
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("arm_name", sa.String(), nullable=False),
        sa.Column(
            "is_baseline", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
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
            ["study_id"], ["public.study.study_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["public.user.user_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["public.agent_profile.profile_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("study_id", "user_id", name="uq_agent_study_assignment"),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_study_assignment_study_id",
        "agent_study_assignment",
        ["study_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_study_assignment_profile_id",
        "agent_study_assignment",
        ["profile_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("study_assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("study_arm_name", sa.String(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("study_arm_is_baseline", sa.Boolean(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("consent_content_storage", sa.Boolean(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("observed_framework_version", sa.String(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column("observed_tools_json", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "agent_task",
        sa.Column(
            "next_event_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=_SCHEMA,
    )
    op.create_foreign_key(
        "agent_task_study_id_fkey",
        "agent_task",
        "study",
        ["study_id"],
        ["study_id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
    )
    op.create_foreign_key(
        "agent_task_study_assignment_id_fkey",
        "agent_task",
        "agent_study_assignment",
        ["study_assignment_id"],
        ["assignment_id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "agent_task_profile_id_fkey",
        "agent_task",
        "agent_profile",
        ["profile_id"],
        ["profile_id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_task_study_id",
        "agent_task",
        ["study_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_task_study_assignment_id",
        "agent_task",
        ["study_assignment_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_agent_task_profile_id",
        "agent_task",
        ["profile_id"],
        unique=False,
        schema=_SCHEMA,
    )
    op.execute(
        """
        CREATE FUNCTION public.prevent_agent_task_snapshot_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.profile_id IS NOT NULL AND (
                NEW.study_id IS DISTINCT FROM OLD.study_id OR
                NEW.study_assignment_id IS DISTINCT FROM OLD.study_assignment_id OR
                NEW.profile_id IS DISTINCT FROM OLD.profile_id OR
                NEW.study_arm_name IS DISTINCT FROM OLD.study_arm_name OR
                NEW.study_arm_is_baseline IS DISTINCT FROM OLD.study_arm_is_baseline OR
                NEW.consent_content_storage IS DISTINCT FROM OLD.consent_content_storage OR
                NEW.agent_profile IS DISTINCT FROM OLD.agent_profile OR
                NEW.model IS DISTINCT FROM OLD.model OR
                NEW.temperature IS DISTINCT FROM OLD.temperature OR
                NEW.approval_policy IS DISTINCT FROM OLD.approval_policy OR
                NEW.tools_json IS DISTINCT FROM OLD.tools_json OR
                NEW.framework_version IS DISTINCT FROM OLD.framework_version OR
                NEW.policy_snapshot IS DISTINCT FROM OLD.policy_snapshot
            ) THEN
                RAISE EXCEPTION 'Agent task execution snapshots are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER agent_task_snapshot_immutable
        BEFORE UPDATE ON public.agent_task
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_agent_task_snapshot_mutation();
        """
    )

    op.execute(
        """
        UPDATE public.agent_task task
        SET next_event_index = COALESCE(
            (
                SELECT MAX(event.event_index) + 1
                FROM public.agent_event event
                WHERE event.task_id = task.task_id
            ),
            0
        )
        """
    )

    op.add_column(
        "agent_event",
        sa.Column("source_event_id", sa.String(), nullable=True),
        schema=_SCHEMA,
    )

    # Historical duplicate positions have no defensible automatic repair. Stop
    # rather than silently selecting an arbitrary event as the canonical one.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.agent_event
                GROUP BY task_id, event_index
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot add agent event ordering constraint: duplicate task/event indexes exist';
            END IF;
        END $$;
        """
    )
    op.create_unique_constraint(
        "uq_agent_event_task_event_index",
        "agent_event",
        ["task_id", "event_index"],
        schema=_SCHEMA,
    )
    op.create_unique_constraint(
        "uq_agent_event_source_identity",
        "agent_event",
        ["task_id", "source", "source_event_id"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER agent_task_snapshot_immutable ON public.agent_task")
    op.execute("DROP FUNCTION public.prevent_agent_task_snapshot_mutation()")
    op.drop_constraint(
        "uq_agent_event_source_identity",
        "agent_event",
        type_="unique",
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "uq_agent_event_task_event_index",
        "agent_event",
        type_="unique",
        schema=_SCHEMA,
    )
    op.drop_column("agent_event", "source_event_id", schema=_SCHEMA)

    op.drop_index(
        "idx_agent_task_study_assignment_id", table_name="agent_task", schema=_SCHEMA
    )
    op.drop_index("idx_agent_task_profile_id", table_name="agent_task", schema=_SCHEMA)
    op.drop_index("idx_agent_task_study_id", table_name="agent_task", schema=_SCHEMA)
    op.drop_constraint(
        "agent_task_profile_id_fkey",
        "agent_task",
        type_="foreignkey",
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "agent_task_study_assignment_id_fkey",
        "agent_task",
        type_="foreignkey",
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "agent_task_study_id_fkey",
        "agent_task",
        type_="foreignkey",
        schema=_SCHEMA,
    )
    for column_name in (
        "next_event_index",
        "consent_content_storage",
        "study_arm_is_baseline",
        "study_arm_name",
        "profile_id",
        "study_assignment_id",
        "study_id",
        "observed_tools_json",
        "observed_framework_version",
    ):
        op.drop_column("agent_task", column_name, schema=_SCHEMA)

    op.drop_index(
        "idx_agent_study_assignment_profile_id",
        table_name="agent_study_assignment",
        schema=_SCHEMA,
    )
    op.drop_index(
        "idx_agent_study_assignment_study_id",
        table_name="agent_study_assignment",
        schema=_SCHEMA,
    )
    op.drop_table("agent_study_assignment", schema=_SCHEMA)
