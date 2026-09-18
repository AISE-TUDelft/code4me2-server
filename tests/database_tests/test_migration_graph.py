"""Alembic graph invariants for the consolidated single-migration schema.

The migration history was deliberately collapsed to ONE revision: the install
baseline is ``init.sql`` (the documented clean-install path) and a single
consolidated revision creates every ORM table that ``init.sql`` does not, plus
the column/index changes on the core tables. There is no older chain to keep.

These tests are pure (no database); they guard the graph shape so a stray
revision file or a second head cannot creep back in.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIR = PROJECT_ROOT / "src" / "database" / "migration"
VERSIONS_DIR = MIGRATION_DIR / "versions"

HEAD_REVISION = "8a0084080b46"


def _load_script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATION_DIR))
    return ScriptDirectory.from_config(config)


def test_migration_graph_has_exactly_one_head():
    script = _load_script_directory()
    assert script.get_heads() == [HEAD_REVISION]


def test_head_is_the_only_revision_and_starts_the_chain():
    script = _load_script_directory()
    head = script.get_revision(HEAD_REVISION)
    assert head is not None, "the consolidated head must be resolvable"
    assert head.down_revision is None, "the consolidated revision is the chain base"
    assert Path(head.path).name.startswith(HEAD_REVISION)


def test_no_orphan_revision_files():
    files = sorted(path.name for path in VERSIONS_DIR.glob("*.py"))
    assert files, "the consolidated revision must exist"
    orphans = [name for name in files if not name.startswith(HEAD_REVISION)]
    assert orphans == [], f"unexpected extra migration revisions: {orphans}"
