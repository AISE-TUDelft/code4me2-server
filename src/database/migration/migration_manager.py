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
from sqlalchemy import create_engine, text

# Set up paths
current_dir = Path(__file__).parent  # src/database/migration
project_root = current_dir.parent.parent.parent  # project root
src_dir = project_root / "src"
sys.path.insert(0, str(src_dir))


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
        """Set up Alembic migration tracking."""
        print("Setting up migration tracking...")

        try:
            # Set database URL
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())

            # Create EMPTY baseline migration (not autogenerated)
            # This represents the current state from init.sql
            init_file_name = "init_test.sql" if self.use_test_db else "init.sql"
            command.revision(
                self.alembic_cfg,
                message=f"Baseline migration from {init_file_name}",
                autogenerate=False,  # Create empty migration
            )

            # Mark as current state
            command.stamp(self.alembic_cfg, "head")

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

    def init_migrations(self) -> None:
        """Initialize migrations with hybrid approach."""
        db_type = "test database" if self.use_test_db else "main database"
        print(f"Initializing migration system for {db_type}...")
        print(f"Database URL: {self.get_database_url()}")

        if self.ensure_initialized():
            print("Migration system ready!")
            print("Use 'create' to add new migrations, 'migrate' to apply them")
        else:
            print("Failed to initialize migration system")

    def create_migration(self, message: str) -> None:
        """Create a new migration."""
        print(f"Creating migration: {message}")

        # Ensure system is initialized first
        if not self.ensure_initialized():
            return

        try:
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())
            command.revision(self.alembic_cfg, message=message, autogenerate=True)
            print("Migration created")
        except Exception as e:
            print(f"Error: {e}")

    def migrate(self) -> None:
        """Apply all migrations."""
        print("Applying migrations...")

        # Ensure system is initialized first
        if not self.ensure_initialized():
            return

        try:
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())
            command.upgrade(self.alembic_cfg, "head")
            print("Migrations applied")
        except Exception as e:
            print(f"Error: {e}")

    def current(self) -> None:
        """Show current revision."""
        if not self.check_database_connection():
            return

        try:
            self.alembic_cfg.set_main_option("sqlalchemy.url", self.get_database_url())
            command.current(self.alembic_cfg)
        except Exception as e:
            print(f"Error: {e}")

    def history(self) -> None:
        """Show migration history."""
        try:
            command.history(self.alembic_cfg)
        except Exception as e:
            print(f"Error: {e}")

    def status(self) -> None:
        """Show detailed status of database and migrations."""
        db_type = "Test Database" if self.use_test_db else "Main Database"
        print(f"Migration System Status - {db_type}")
        print("=" * 50)
        print(f"Database URL: {self.get_database_url()}")

        # Check database connection
        if not self.check_database_connection():
            print("Database: Not connected")
            return

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
            except Exception:
                print("Could not get current version")
        else:
            print("Migration tracking: Not set up")

    def reset(self) -> None:
        """Reset database and reinitialize."""
        db_type = "test database" if self.use_test_db else "main database"
        print(f"Resetting {db_type}...")

        if not self.check_database_connection():
            return

        try:
            # Drop and recreate public schema
            engine = create_engine(self.get_database_url())
            with engine.connect() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
                conn.commit()

            print("Database reset")

            # Reinitialize
            self.init_migrations()

        except Exception as e:
            print(f"Error during reset: {e}")


def main():
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
        return

    manager = MigrationManager(use_test_db=args.test)

    if args.command == "init":
        manager.init_migrations()
    elif args.command == "status":
        manager.status()
    elif args.command == "create":
        manager.create_migration(args.message)
    elif args.command == "migrate":
        manager.migrate()
    elif args.command == "current":
        manager.current()
    elif args.command == "history":
        manager.history()
    elif args.command == "reset":
        manager.reset()


if __name__ == "__main__":
    main()
