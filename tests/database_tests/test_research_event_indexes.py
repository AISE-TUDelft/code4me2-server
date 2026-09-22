"""TSCH-06: the research_event supporting indexes exist in the model and the
single consolidated migration (no database required)."""

from __future__ import annotations

from pathlib import Path

from database.research_schemas import ResearchEvent

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "src"
    / "database"
    / "migration"
    / "versions"
    / "8a0084080b46_consolidated_schema.py"
)

SUPPORTING_INDEXES = (
    ("idx_research_event_study_id", "study_id"),
    ("idx_research_event_agent_run_id", "agent_run_id"),
    ("idx_research_event_retention_state", "retention_state"),
)


def test_research_event_supporting_indexes_are_declared_on_the_model():
    names = {index.name for index in ResearchEvent.__table__.indexes}
    missing = sorted(name for name, _ in SUPPORTING_INDEXES if name not in names)
    assert missing == [], f"missing research_event indexes: {missing}"


def test_consolidated_migration_creates_and_drops_the_supporting_indexes():
    migration = MIGRATION_PATH.read_text()
    for name, column in SUPPORTING_INDEXES:
        assert (
            f"op.create_index('{name}', 'research_event', ['{column}']" in migration
        ), f"{name} is not created by the consolidated migration"
        assert (
            f"op.drop_index('{name}', table_name='research_event'" in migration
        ), f"{name} is not dropped by the consolidated migration"
