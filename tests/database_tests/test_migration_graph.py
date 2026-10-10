"""Alembic graph invariants for the study schema.

The history was collapsed to ONE consolidated revision (``8a0084080b46``) on
2026-09-18: the install baseline is ``init.sql`` and that revision creates every
ORM table ``init.sql`` does not. Since the production-readiness review (B-09)
that revision is the frozen study baseline and every schema change is one
further Alembic revision on a strictly linear chain, so a deployed database is
upgraded, never rebuilt.

These tests are pure (no database); they guard the graph shape so a second head,
a branch or a stray revision file cannot creep in, and they pin the current head
so a new revision is a deliberate, reviewed change (update ``HEAD_REVISION``).
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIR = PROJECT_ROOT / "src" / "database" / "migration"
VERSIONS_DIR = MIGRATION_DIR / "versions"

BASE_REVISION = "8a0084080b46"
HEAD_REVISION = "d41e7c9a2b6f"
# Oldest first. Adding a revision means appending here and moving HEAD_REVISION.
EXPECTED_CHAIN = [BASE_REVISION, "b7c1d2e3f4a5", "c5e8f1a2d3b4", HEAD_REVISION]


def _load_script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATION_DIR))
    return ScriptDirectory.from_config(config)


def _chain(script: ScriptDirectory) -> list[str]:
    """Revisions from base to head; fails on a branch (more than one parent)."""
    revisions = []
    for revision in script.walk_revisions(base="base", head=HEAD_REVISION):
        parents = revision.down_revision
        assert not isinstance(parents, tuple), f"{revision.revision} merges branches"
        revisions.append(revision.revision)
    return list(reversed(revisions))


def test_migration_graph_has_exactly_one_head():
    script = _load_script_directory()
    assert script.get_heads() == [HEAD_REVISION]


def test_consolidated_revision_is_the_chain_base():
    script = _load_script_directory()
    base = script.get_revision(BASE_REVISION)
    assert base is not None, "the consolidated baseline must be resolvable"
    assert base.down_revision is None, "the consolidated revision is the chain base"
    assert Path(base.path).name.startswith(BASE_REVISION)
    assert script.get_bases() == [BASE_REVISION]


def test_chain_is_linear_and_matches_the_expected_order():
    script = _load_script_directory()
    assert _chain(script) == EXPECTED_CHAIN


def test_no_orphan_revision_files():
    script = _load_script_directory()
    known = set(_chain(script))
    files = sorted(path.name for path in VERSIONS_DIR.glob("*.py"))
    assert files, "the consolidated revision must exist"
    orphans = [name for name in files if name.split("_", 1)[0] not in known]
    assert orphans == [], f"revision files outside the linear chain: {orphans}"
    assert len(files) == len(known), "every chain revision must have exactly one file"
