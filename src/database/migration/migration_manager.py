#!/usr/bin/env python3
"""
Migration Manager - Hybrid Approach

This manager:
1. Auto-initializes from init.sql on first run
2. Uses standard migrations afterward
3. Seamlessly handles the transition

Usage:
- First time: `python migration_manager.py init` or `python migration_manager.py migrate`
- Creates baseline from init.sql and sets up tracking
- Future changes: standard migration workflow
"""

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Set up paths
current_dir = Path(__file__).parent  # src/database/migration
project_root = current_dir.parent.parent.parent  # project root
src_dir = project_root / "src"
sys.path.insert(0, str(src_dir))
# Match src/main.py: operator-supplied environment variables win, while a
# repository/deployment .env fills values that Docker Compose does not export.
load_dotenv(project_root / ".env", override=False)


def _database_url_for_log(value: str) -> str:
    try:
        return make_url(value).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database URL>"


class MigrationManager:
    """Enhanced migration manager with hybrid init support."""

    def __init__(self, use_test_db=False):
        self.project_root = project_root
        self.alembic_cfg_path = self.project_root / "alembic.ini"
        self.use_test_db = use_test_db

        # Choose init SQL file based on database type
        if use_test_db:
            self.init_sql_path = (
                self.project_root / "src" / "database" / "init_test.sql"
            )
        else:
            self.init_sql_path = self.project_root / "src" / "database" / "init.sql"

        if not self.alembic_cfg_path.exists():
            print(f"alembic.ini not found at {self.alembic_cfg_path}")
            print("Create alembic.ini in your project root")
            sys.exit(1)

        self.alembic_cfg = Config(str(self.alembic_cfg_path))

    def get_database_url(self) -> str:
        """Get database URL from environment variables."""
        if self.use_test_db:
            # Use test database for testing
            return os.getenv(
                "TEST_DATABASE_URL",
                "postgresql://postgres:postgres@localhost:5433/test_db",
            )

        # Use main database from .env
        db_user = os.getenv("DB_USER", "postgres")
        db_password = os.getenv("DB_PASSWORD", "postgres")
        db_host = os.getenv("DB_HOST", "localhost")
        db_port = os.getenv("DB_PORT", "2345")
        db_name = os.getenv("DB_NAME", "code4meV2")

        return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

    def check_database_connection(self) -> bool:
        """Check if database connection works."""
        try:
            engine = create_engine(self.get_database_url())
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            print(f"Database connection failed: {e}")
            if self.use_test_db:
                print("Start test database: docker-compose up test_db")
            else:
                print("Start main database: docker-compose up db")
            return False

    def is_database_initialized(self) -> bool:
        """Check if database has been initialized (has tables)."""
        try:
            engine = create_engine(self.get_database_url())
            with engine.connect() as conn:
                # Check if any of our main tables exist
                result = conn.execute(
                    text(
                        """SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('user', 'config', 'model_name')"""
                    )
                )
                table_count = result.scalar()
                return table_count > 0
        except Exception:
            return False

    def is_migration_tracking_setup(self) -> bool:
        """Check if migration tracking is set up."""
        try:
            engine = create_engine(self.get_database_url())
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        """SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'alembic_version')"""
                    )
                )
                return result.scalar()
        except Exception:
            return False

    def initialize_from_sql(self) -> bool:
        """Initialize database from init.sql file."""
        init_file_name = "init_test.sql" if self.use_test_db else "init.sql"
        print(f"Initializing database from {init_file_name}...")

        if not self.init_sql_path.exists():
            print(f"{init_file_name} not found at {self.init_sql_path}")
            return False

        try:
            # Read and execute init.sql
            init_sql_content = self.init_sql_path.read_text()

            engine = create_engine(self.get_database_url())
            with engine.connect() as conn:
                # Execute the init.sql content
                conn.execute(text(init_sql_content))
                conn.commit()

            print(f"Database initialized from {init_file_name}")
            return True

        except Exception as e:
            print(f"Error initializing from {init_file_name}: {e}")
            return False

    def setup_migration_tracking(self) -> bool:
        """Set up Alembic tracking by stamping at the start of the existing chain."""
        print("Setting up migration tracking...")

        try:
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())

            # Stamp at "base" (no revision) so the subsequent `upgrade head` runs
            # every migration in order from the committed baseline
            # (c2dc3e9cc1bc_baseline_migration_from_init_sql).
            #
            # Do NOT create a new revision file here: the baseline is already
            # committed, so generating another one would put this database on a
            # parallel dead-end branch and silently skip all real migrations —
            # which is how the agent tables would go missing on a fresh install.
            command.stamp(self.alembic_cfg, "base")

            print("Migration tracking set up")
            return True

        except Exception as e:
            print(f"Error setting up migration tracking: {e}")
            return False

    def ensure_initialized(self) -> bool:
        """Ensure database is initialized and tracking is set up."""

        if not self.check_database_connection():
            return False

        # Check if database has tables
        if not self.is_database_initialized():
            init_file_name = "init_test.sql" if self.use_test_db else "init.sql"
            print(f"Database is empty - initializing from {init_file_name}...")
            if not self.initialize_from_sql():
                return False
        else:
            print("Database already has tables")

        # Check if migration tracking is set up
        if not self.is_migration_tracking_setup():
            print("Setting up migration tracking...")
            if not self.setup_migration_tracking():
                return False
        else:
            print("Migration tracking already set up")

        return True

    def init_migrations(self) -> bool:
        """Initialize migrations with hybrid approach."""
        db_type = "test database" if self.use_test_db else "main database"
        print(f"Initializing migration system for {db_type}...")
        print(f"Database URL: {_database_url_for_log(self.get_database_url())}")

        if self.ensure_initialized():
            print("Migration system ready!")
            print("Use 'create' to add new migrations, 'migrate' to apply them")
            return True
        else:
            print("Failed to initialize migration system")
            return False

    def create_migration(self, message: str) -> bool:
        """Create a new migration."""
        print(f"Creating migration: {message}")

        # Ensure system is initialized first
        if not self.ensure_initialized():
            return False

        try:
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())
            command.revision(self.alembic_cfg, message=message, autogenerate=True)
            print("Migration created")
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False

    def migrate(self) -> bool:
        """Apply all migrations."""
        print("Applying migrations...")

        # Ensure system is initialized first
        if not self.ensure_initialized():
            return False

        try:
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())
            command.upgrade(self.alembic_cfg, "head")
            if not self.is_at_expected_head():
                print("Migration command completed, but the database is not at the expected Alembic head")
                return False
            print("Migrations applied")
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False

    def current(self) -> bool:
        """Show current revision."""
        if not self.check_database_connection():
            return False

        try:
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())
            command.current(self.alembic_cfg)
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False

    def history(self) -> bool:
        """Show migration history."""
        try:
            command.history(self.alembic_cfg)
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False

    def status(self) -> bool:
        """Show detailed status of database and migrations."""
        db_type = "Test Database" if self.use_test_db else "Main Database"
        print(f"Migration System Status - {db_type}")
        print("=" * 50)
        print(f"Database URL: {_database_url_for_log(self.get_database_url())}")

        # Check database connection
        if not self.check_database_connection():
            print("Database: Not connected")
            return False

        print("Database: Connected")

        # Check if initialized
        if self.is_database_initialized():
            print("Database: Initialized")

            # Count tables
            try:
                engine = create_engine(self.get_database_url())
                with engine.connect() as conn:
                    result = conn.execute(
                        text(
                            """SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"""
                        )
                    )
                    table_count = result.scalar()
                    print(f"Tables: {table_count}")
            except Exception:
                print("Could not count tables")
        else:
            print("Database: Not initialized")

        # Check migration tracking
        if self.is_migration_tracking_setup():
            print("Migration tracking: Set up")

            # Show current version
            try:
                engine = create_engine(self.get_database_url())
                with engine.connect() as conn:
                    result = conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
                    version = result.scalar()
                    print(f"Current version: {version}")
                if not self.is_at_expected_head():
                    print("Migration readiness: Not at the expected head")
                    return False
                print("Migration readiness: At expected head")
            except Exception:
                print("Could not get current version")
                return False
        else:
            print("Migration tracking: Not set up")
            return False
        return True

    def is_at_expected_head(self) -> bool:
        """Require the deployed database revision set to equal the code's heads."""
        try:
            expected = set(ScriptDirectory.from_config(self.alembic_cfg).get_heads())
            engine = create_engine(self.get_database_url())
            with engine.connect() as conn:
                applied = {
                    str(row[0])
                    for row in conn.execute(text("SELECT version_num FROM alembic_version"))
                }
            return bool(expected) and applied == expected
        except Exception as error:
            print(f"Could not verify migration head: {error}")
            return False

    def reset(self) -> bool:
        """Reset database and reinitialize."""
        db_type = "test database" if self.use_test_db else "main database"
        print(f"Resetting {db_type}...")

        if not self.check_database_connection():
            return False

        try:
            # Drop and recreate public schema
            engine = create_engine(self.get_database_url())
            with engine.connect() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
                conn.commit()

            print("Database reset")

            # Reinitialize
            return self.init_migrations()

        except Exception as e:
            print(f"Error during reset: {e}")
            return False


def main() -> int:
    """CLI interface."""
    parser = argparse.ArgumentParser(description="Hybrid Migration Manager")
    parser.add_argument(
        "--test", action="store_true", help="Use test database instead of main database"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    subparsers.add_parser(
        "init", help="Initialize migration system (auto-detects first run)"
    )
    subparsers.add_parser("status", help="Show system status")

    create_parser = subparsers.add_parser("create", help="Create migration")
    create_parser.add_argument("message", help="Migration message")

    subparsers.add_parser(
        "migrate", help="Apply migrations (auto-initializes if needed)"
    )
    subparsers.add_parser("current", help="Show current revision")
    subparsers.add_parser("history", help="Show migration history")
    subparsers.add_parser("reset", help="Reset database and reinitialize")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    manager = MigrationManager(use_test_db=args.test)

    if args.command == "init":
        succeeded = manager.init_migrations()
    elif args.command == "status":
        succeeded = manager.status()
    elif args.command == "create":
        succeeded = manager.create_migration(args.message)
    elif args.command == "migrate":
        succeeded = manager.migrate()
    elif args.command == "current":
        succeeded = manager.current()
    elif args.command == "history":
        succeeded = manager.history()
    elif args.command == "reset":
        succeeded = manager.reset()
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
