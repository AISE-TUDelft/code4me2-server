"""Research-schema consolidation invariants (no database required).

The DB consolidation removed derivation/absorption tables and keeps the generic
``research_record`` table. These tests guard the ORM metadata and the Alembic
graph directly so a removed table cannot silently creep back in (and the chain
keeps one head).
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from database.db_schemas import (  # imports research_schemas too (one shared Base)
    AgentTask,
    Base,
    ResearchStudyStatus,
    Study,
)
from database.research_schemas import (
    ResearchEnrollment,
    ResearchEvent,
    ResearchSessionV1,
    StudyAgentProfile,
    StudyAssignment,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIR = PROJECT_ROOT / "src" / "database" / "migration"

# Tables dropped by the consolidation: pure projections of an existing JSON
# column, or records absorbed by research_record.
REMOVED_TABLES = (
    "research_consent_receipt",
    "acp_capability_evidence",
    "agent_distribution_artifact",
    "agent_adapter",
    "study_condition",
    "research_study_draft",
    "emitter_cursor",
    "telemetry_rejection",
    "research_session_transition",
    "research_retention_verification",
    "research_deletion_ledger",
    "runtime_package_component",
    "study_publication_audit",
    "research_export_audit",
    "telemetry_coverage",
    "telemetry_metric",
    "research_operational_health",
    "research_release_evidence",
    "research_pilot_run",
    # Stronger consolidation: folded into a JSON column / generic table.
    "research_study",
    "agent_capability_snapshot",
    "research_session_capability",
    "research_withdrawal",
    "runtime_package",
    "runtime_conformance_receipt",
    "research_audit",
    "research_ops_record",
    "research_kill_switch",
    "study_revision",
    "condition_exposure",
    "provider_connection_grant",
    # Wave 1: parallel assignment authorities replaced by research study_assignment.
    "agent_profile_assignment",
    "agent_study_assignment",
    "research_researcher_role",
)

ADDED_TABLES = ("research_record",)

# Wave 1: new persisted owners introduced by the ownership/provider work.
WAVE1_TABLES = (
    "provider_connection",
    "study_agent_profile",
    "study_assignment",
)


def _table_names() -> set[str]:
    # Metadata tables are schema-qualified ("public.<name>").
    return {table.split(".")[-1] for table in Base.metadata.tables}


def test_removed_tables_are_absent_from_metadata():
    present = sorted(name for name in REMOVED_TABLES if name in _table_names())
    assert present == [], f"removed tables still declared: {present}"


def test_generic_tables_are_present_in_metadata():
    names = _table_names()
    missing = sorted(name for name in ADDED_TABLES if name not in names)
    assert missing == [], f"consolidated tables missing: {missing}"


def test_wave1_tables_are_present_in_metadata():
    names = _table_names()
    missing = sorted(name for name in WAVE1_TABLES if name not in names)
    assert missing == [], f"wave-1 tables missing: {missing}"


def test_research_study_lifecycle_contract_is_present():
    columns = set(Study.__table__.c.keys())
    expected_columns = {
        "research_status",
        "research_config_json",
        "research_config_digest",
        "join_code",
        "consent_locked_at",
        "stopped_at",
        "stopped_by",
    }
    assert expected_columns <= columns
    assert [status.value for status in ResearchStudyStatus] == [
        "DRAFT",
        "ACTIVE",
        "STUDY_STOPPED",
    ]
    index_names = {index.name for index in Study.__table__.indexes}
    assert {
        "idx_study_research_status",
        "uq_study_research_join_code",
    } <= index_names


def test_study_agent_profile_contract_is_present():
    columns = set(StudyAgentProfile.__table__.c.keys())
    assert {
        "study_id",
        "profile_id",
        "profile_digest",
        "profile_snapshot_json",
        "selection_order",
        "created_at",
    } <= columns
    assert {
        column.name
        for column in StudyAgentProfile.__table__.primary_key.columns
    } == {"study_id", "profile_id"}
    unique_constraints = {
        constraint.name
        for constraint in StudyAgentProfile.__table__.constraints
        if constraint.name
    }
    assert "uq_study_agent_profile_selection" in unique_constraints


def test_revision_and_condition_authorities_are_absent_from_research_models():
    model_names = _table_names()
    assert "study_revision" not in model_names
    assert "condition_exposure" not in model_names
    assert "provider_connection_grant" not in model_names

    for model in (
        ResearchEnrollment,
        StudyAssignment,
        ResearchSessionV1,
        ResearchEvent,
        AgentTask,
    ):
        columns = set(model.__table__.c.keys())
        assert "study_revision_id" not in columns, model.__tablename__
        assert "revision_id" not in columns, model.__tablename__
        assert "condition_id" not in columns, model.__tablename__


def test_fresh_migration_does_not_create_removed_research_authorities():
    migration = (
        PROJECT_ROOT
        / "src"
        / "database"
        / "migration"
        / "versions"
        / "8a0084080b46_consolidated_schema.py"
    ).read_text()
    for removed_name in (
        "study_revision",
        "condition_exposure",
        "provider_connection_grant",
        "study_revision_id",
        "condition_id",
        "revision_id",
    ):
        assert removed_name not in migration, removed_name


def test_migration_graph_has_exactly_one_head():
    config = Config()
    config.set_main_option("script_location", str(MIGRATION_DIR))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1, script.get_heads()
