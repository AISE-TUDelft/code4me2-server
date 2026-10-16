from unittest.mock import MagicMock, patch

from database.migration import migration_manager


def test_migration_cli_returns_nonzero_when_upgrade_fails():
    with patch.object(migration_manager.sys, "argv", ["migration_manager.py", "migrate"]), patch.object(
        migration_manager.MigrationManager, "migrate", return_value=False
    ):
        assert migration_manager.main() == 1


def test_migration_cli_returns_zero_when_upgrade_succeeds():
    with patch.object(migration_manager.sys, "argv", ["migration_manager.py", "migrate"]), patch.object(
        migration_manager.MigrationManager, "migrate", return_value=True
    ):
        assert migration_manager.main() == 0


def test_migrate_fails_when_database_did_not_reach_expected_head():
    manager = object.__new__(migration_manager.MigrationManager)
    manager.alembic_cfg = MagicMock()
    manager.ensure_initialized = lambda: True
    manager.get_database_url = lambda: "postgresql://unused"
    manager.is_at_expected_head = lambda: False

    with patch.object(migration_manager.command, "upgrade") as upgrade:
        assert manager.migrate() is False
    upgrade.assert_called_once()
