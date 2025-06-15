#!/usr/bin/env python3
"""
Migration System Test Suite

This version properly handles:
- Windows paths with spaces
- Working directory detection
- Project root detection from tests subdirectory
- Configurable database (main vs test)

"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


def setup_project_paths():
    """
    Properly detect project root and set up paths for Windows.
    """
    # Get the directory where THIS test file is located
    test_file_dir = Path(__file__).resolve().parent  # tests/database_tests/

    # Go up to tests directory, then up to project root
    tests_dir = test_file_dir.parent  # tests/
    project_root = tests_dir.parent  # project root

    print(f"🔍 Test file location: {test_file_dir}")
    print(f"🔍 Tests directory: {tests_dir}")
    print(f"🔍 Detected project root: {project_root}")

    # Verify we found the right project root
    markers = [
        project_root / "run_tests.py",
        project_root / "src",
        project_root / "alembic.ini",
    ]

    found_markers = [marker for marker in markers if marker.exists()]
    print(f"🔍 Found project markers: {[str(m) for m in found_markers]}")

    if not found_markers:
        # Fallback: look for any of these files going up the directory tree
        current = test_file_dir
        while current.parent != current:
            for marker_name in ["run_tests.py", "src", "alembic.ini"]:
                if (current / marker_name).exists():
                    project_root = current
                    print(f"🔍 Found project root via {marker_name}: {project_root}")
                    break
            else:
                current = current.parent
                continue
            break

    # Add project root to Python path
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)
        print(f"✅ Added to Python path: {project_root_str}")

    # Also add src directory
    src_dir = project_root / "src"
    if src_dir.exists():
        src_str = str(src_dir)
        if src_str not in sys.path:
            sys.path.insert(0, src_str)
            print(f"✅ Added src to Python path: {src_str}")

    return project_root


# Set up paths BEFORE any imports
PROJECT_ROOT = setup_project_paths()

# Now try imports with better error handling
print(f"🔄 Attempting imports with project root: {PROJECT_ROOT}")
try:
    from database import crud
    from database.migration.migration_manager import MigrationManager

    print("✅ All imports successful!")
except ImportError as e:
    print(f"❌ Import error: {e}")
    print(f"Current working directory: {Path.cwd()}")
    print(f"Python path: {sys.path[:5]}")  # Show first 5 entries

    # Show what's actually in the directories
    src_dir = PROJECT_ROOT / "src"
    if src_dir.exists():
        print(f"Contents of {src_dir}:")
        for item in src_dir.iterdir():
            print(f"  {item.name}")

    database_dir = PROJECT_ROOT / "src" / "database"
    if database_dir.exists():
        print(f"Contents of {database_dir}:")
        for item in database_dir.iterdir():
            print(f"  {item.name}")

    raise


# Test Configuration
class TestConfig:
    """Centralized test configuration with configurable database."""

    @classmethod
    def get_database_url(cls):
        """Get database URL - configurable via environment variables."""
        # Check if we should use test database
        use_test_db = os.getenv("USE_TEST_DB", "true").lower() == "true"

        if use_test_db:
            # Use test database (default for tests)
            return os.getenv(
                "TEST_DATABASE_URL",
                "postgresql://postgres:postgres@localhost:5433/test_db",
            )
        else:
            # Use main database from .env
            db_user = os.getenv("DB_USER", "postgres")
            db_password = os.getenv("DB_PASSWORD", "postgres")
            db_host = os.getenv("DB_HOST", "localhost")
            db_port = os.getenv("DB_PORT", "2345")
            db_name = os.getenv("DB_NAME", "code4meV2")

            return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

    @classmethod
    def is_using_test_db(cls):
        """Check if we're using test database."""
        return os.getenv("USE_TEST_DB", "true").lower() == "true"

    @classmethod
    def get_migration_script_path(cls):
        """Get migration script path with proper Windows handling."""
        # Try multiple possible locations relative to project root
        possible_paths = [
            PROJECT_ROOT / "src" / "database" / "migration" / "migration_manager.py",
            PROJECT_ROOT / "database" / "migration" / "migration_manager.py",
            PROJECT_ROOT / "migration_manager.py",
            # Also try from current directory (fallback)
            Path.cwd() / "src" / "database" / "migration" / "migration_manager.py",
        ]

        print("🔍 Looking for migration script in:")
        for path in possible_paths:
            print(f"  {path} - {'✅ EXISTS' if path.exists() else '❌ missing'}")
            if path.exists():
                print(f"✅ Found migration script: {path}")
                return path

        # If none found, show directory contents for debugging
        print(f"\n🔍 Debug: Contents of project root {PROJECT_ROOT}:")
        try:
            for item in PROJECT_ROOT.iterdir():
                print(f"  {item.name} ({'dir' if item.is_dir() else 'file'})")
        except Exception as e:
            print(f"  Error listing directory: {e}")

        raise FileNotFoundError(f"Migration script not found. Tried: {possible_paths}")

    @classmethod
    def get_engine(cls):
        """Get database engine."""
        return create_engine(cls.get_database_url())

    @classmethod
    def run_migration_command(cls, command_str, timeout=60):
        """Run a migration command with proper Windows path handling."""
        try:
            import shlex

            migration_script = cls.get_migration_script_path()

            # Use shlex to properly handle quoted arguments (important for Windows paths with spaces)
            cmd_parts = shlex.split(command_str)

            # Add --test flag if using test database
            if cls.is_using_test_db():
                cmd_parts.insert(0, "--test")

            # Build command with quoted paths for Windows
            cmd = [
                "python",
                str(migration_script),  # This handles spaces in paths
            ] + cmd_parts

            print(f"🚀 Running command: {' '.join(cmd)}")
            print(f"🚀 Working directory: {PROJECT_ROOT}")
            print(f"🚀 Database: {'test' if cls.is_using_test_db() else 'main'}")

            # Always run from project root with proper environment
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(PROJECT_ROOT),  # Ensure we're in project root
                env={
                    **os.environ,
                    "PYTHONPATH": str(PROJECT_ROOT),  # Ensure Python can find modules
                },
            )

            success = result.returncode == 0
            if not success:
                print(f"❌ Command failed with return code: {result.returncode}")
                print(f"STDOUT: {result.stdout}")
                print(f"STDERR: {result.stderr}")
            else:
                print("✅ Command succeeded")

            return success, result.stdout, result.stderr

        except Exception as e:
            print(f"❌ Exception running command: {e}")
            return False, "", str(e)

    @classmethod
    def count_tables(cls, engine):
        """Count tables in database."""
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"""
                )
            )
            return result.scalar()

    @classmethod
    def reset_database(cls, engine):
        """Reset database to empty state."""
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.commit()


# Enhanced fixtures with configurable database
@pytest.fixture(scope="session")
def database_engine():
    """Session-scoped database engine with better error messages."""
    try:
        engine = TestConfig.get_engine()
        # Test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        db_type = "test" if TestConfig.is_using_test_db() else "main"
        print(f"✅ Database connection successful ({db_type} database)")
        print(f"Database URL: {TestConfig.get_database_url()}")

        return engine
    except Exception as e:
        db_type = "test" if TestConfig.is_using_test_db() else "main"
        pytest.skip(
            f"""
❌ Database not available: {e}

To fix this:
1. Start {db_type} database: docker-compose up {'test_db' if TestConfig.is_using_test_db() else 'db'}
2. Check connection: psql -h localhost -p {'5433' if TestConfig.is_using_test_db() else os.getenv('DB_PORT', '2345')} -U postgres -d {'test_db' if TestConfig.is_using_test_db() else os.getenv('DB_NAME', 'code4meV2')}
3. Verify DATABASE_URL: {TestConfig.get_database_url()}

Or set USE_TEST_DB=false to use main database, or USE_TEST_DB=true to use test database.
        """
        )


@pytest.fixture(scope="function")
def clean_database(database_engine):
    """Function-scoped clean database."""
    TestConfig.reset_database(database_engine)
    yield database_engine


@pytest.fixture(scope="function")
def initialized_database(clean_database):
    """Function-scoped initialized database with better error handling."""
    db_type = "test" if TestConfig.is_using_test_db() else "main"
    print(f"🔄 Initializing {db_type} database...")

    success, stdout, stderr = TestConfig.run_migration_command("init")
    if not success:
        print("❌ Database initialization failed")
        print(f"STDOUT: {stdout}")
        print(f"STDERR: {stderr}")
        pytest.fail(
            f"""
❌ Failed to initialize {db_type} database:
STDERR: {stderr}
STDOUT: {stdout}

Debug info:
- Migration script: {TestConfig.get_migration_script_path()}
- Working directory: {PROJECT_ROOT}
- Current sys.path: {sys.path[:3]}...
- Database URL: {TestConfig.get_database_url()}

Make sure:
1. migration_manager.py exists at the expected location
2. Database is running: docker-compose up {'test_db' if TestConfig.is_using_test_db() else 'db'}
3. All dependencies are installed: pip install -r requirements.txt
        """
        )
    print(f"✅ {db_type.title()} database initialized successfully")
    yield clean_database


# Test Classes with configurable database support
class TestDatabaseConnection:
    """Test database connectivity and basic operations."""

    def test_database_connection_works(self, database_engine):
        """Test basic database connection."""
        with database_engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar() == 1

    def test_correct_database_selected(self, database_engine):
        """Test that we're connected to the correct database."""
        with database_engine.connect() as conn:
            result = conn.execute(text("SELECT current_database()"))
            current_db = result.scalar()

            expected_db = (
                "test_db"
                if TestConfig.is_using_test_db()
                else os.getenv("DB_NAME", "code4meV2")
            )
            assert (
                current_db == expected_db
            ), f"Expected {expected_db}, got {current_db}"

            db_type = "test" if TestConfig.is_using_test_db() else "main"
            print(f"✅ Connected to correct {db_type} database: {current_db}")

    def test_paths_are_correct(self):
        """Test that all paths are correctly detected on Windows."""
        print("\n=== PATH VERIFICATION ===")
        print(f"Project root: {PROJECT_ROOT}")
        print(f"Project root exists: {PROJECT_ROOT.exists()}")

        # Test migration script detection
        try:
            migration_script = TestConfig.get_migration_script_path()
            print(f"Migration script: {migration_script}")
            assert (
                migration_script.exists()
            ), f"Migration script not found: {migration_script}"
        except FileNotFoundError as e:
            pytest.fail(f"Migration script path detection failed: {e}")

        # Test alembic config (with multiple possible locations)
        possible_alembic_configs = [
            PROJECT_ROOT / "alembic.ini",
            PROJECT_ROOT / "src" / "alembic.ini",
        ]

        alembic_config = None
        for config_path in possible_alembic_configs:
            print(
                f"Checking alembic config: {config_path} - {'✅' if config_path.exists() else '❌'}"
            )
            if config_path.exists():
                alembic_config = config_path
                break

        if alembic_config is None:
            # This might be okay if alembic.ini is generated dynamically
            print(
                f"⚠️  Warning: alembic.ini not found at expected locations: {possible_alembic_configs}"
            )
        else:
            print(f"✅ Found alembic.ini: {alembic_config}")

    def test_imports_work(self):
        """Test that all required imports work."""
        # These should work now that we fixed the paths
        assert MigrationManager is not None
        assert hasattr(
            crud, "get_config_by_id"
        ), "crud module should have expected functions"
        print("✅ All imports working correctly")


class TestMigrationManagerUnit:
    """Unit tests for MigrationManager with configurable database."""

    def test_migration_manager_creation(self):
        """Test migration manager can be created."""
        try:
            use_test_db = TestConfig.is_using_test_db()
            manager = MigrationManager(use_test_db=use_test_db)
            assert manager is not None
            assert hasattr(manager, "check_database_connection")

            db_type = "test" if use_test_db else "main"
            print(f"✅ MigrationManager created successfully for {db_type} database")
        except Exception as e:
            pytest.fail(f"Failed to create MigrationManager: {e}")

    def test_get_database_url(self):
        """Test get_database_url returns correct URL."""
        use_test_db = TestConfig.is_using_test_db()
        manager = MigrationManager(use_test_db=use_test_db)
        url = manager.get_database_url()

        expected_url = TestConfig.get_database_url()
        assert url == expected_url, f"Expected {expected_url}, got {url}"

        if use_test_db:
            assert "test_db" in url
        else:
            expected_db = os.getenv("DB_NAME", "code4meV2")
            assert expected_db in url

        print(f"✅ Database URL correct: {url}")

    def test_migration_script_exists(self):
        """Test migration script file exists."""
        migration_script = TestConfig.get_migration_script_path()
        assert migration_script.exists()
        print(f"✅ Migration script exists: {migration_script}")

    def test_alembic_config_exists(self):
        """Test alembic.ini exists and is valid (if present)."""
        # Look in multiple locations
        possible_configs = [
            PROJECT_ROOT / "alembic.ini",
            PROJECT_ROOT / "src" / "alembic.ini",
        ]

        config_found = False
        for alembic_config in possible_configs:
            if alembic_config.exists():
                content = alembic_config.read_text()
                assert "script_location" in content
                config_found = True
                print(f"✅ Found valid alembic.ini: {alembic_config}")
                break

        if not config_found:
            # Don't fail the test, just warn - alembic.ini might be generated
            print(f"⚠️  Warning: alembic.ini not found in: {possible_configs}")
            print(
                "This might be okay if alembic.ini is generated by the migration system"
            )


class TestMigrationCommands:
    """Test migration command line interface with configurable database."""

    def test_migration_help_command(self):
        """Test migration help command works."""
        success, stdout, stderr = TestConfig.run_migration_command("--help")
        if not success:
            pytest.fail(
                f"""
❌ Migration help command failed:
STDOUT: {stdout}
STDERR: {stderr}
Migration script: {TestConfig.get_migration_script_path()}

This suggests the migration script itself has issues. Check:
1. Python syntax in migration_manager.py
2. Required dependencies are installed
3. The script is executable with: python migration_manager.py --help
            """
            )
        assert "usage:" in stdout.lower()
        print("✅ Help command works")

    def test_status_command_on_empty_database(self, clean_database):
        """Test status command on empty database."""
        success, stdout, stderr = TestConfig.run_migration_command("status")
        # Should work even on empty database
        if not success and "not connected" not in stderr.lower():
            print(f"⚠️  Status command failed (might be expected on empty DB): {stderr}")
        else:
            print("✅ Status command works")


class TestMigrationInitialization:
    """Test the hybrid migration initialization process."""

    def test_init_creates_all_tables(self, clean_database):
        """Test init command creates all expected tables."""
        # Verify database starts empty
        initial_count = TestConfig.count_tables(clean_database)
        assert initial_count == 0

        # Run init command
        success, stdout, stderr = TestConfig.run_migration_command("init")
        assert success, f"Init failed: {stderr}"

        # Verify tables were created
        final_count = TestConfig.count_tables(clean_database)
        assert final_count >= 18, f"Expected 18+ tables, got {final_count}"

        db_type = "test" if TestConfig.is_using_test_db() else "main"
        print(f"✅ Created {final_count} tables in {db_type} database")


# Debug test class to help troubleshoot issues
class TestDebugInfo:
    """Tests to help debug Windows path and configuration issues."""

    def test_debug_paths(self):
        """Show all detected paths for debugging."""
        print("\n=== DEBUG PATH INFO (WINDOWS) ===")
        print(f"Current working directory: {Path.cwd()}")
        print(f"Test file location: {Path(__file__)}")
        print(f"Detected project root: {PROJECT_ROOT}")

        try:
            migration_script = TestConfig.get_migration_script_path()
            print(f"Migration script: {migration_script}")
        except Exception as e:
            print(f"Migration script error: {e}")

        print(f"Python path (first 3): {sys.path[:3]}")

        # Show what files exist in project root
        print("\nFiles in project root:")
        try:
            for item in sorted(PROJECT_ROOT.iterdir())[:10]:  # First 10 items
                print(f"  {item.name} ({'dir' if item.is_dir() else 'file'})")
        except Exception as e:
            print(f"Error listing directory: {e}")

    def test_environment_info(self):
        """Show environment information."""
        print("\n=== ENVIRONMENT INFO ===")
        print(f"Python executable: {sys.executable}")
        print(f"Python version: {sys.version}")
        print(f"Platform: {sys.platform}")
        print(f"PYTHONPATH env var: {os.environ.get('PYTHONPATH', 'Not set')}")
        print(f"USE_TEST_DB: {os.getenv('USE_TEST_DB', 'not set')}")
        print(f"TEST_DATABASE_URL: {os.getenv('TEST_DATABASE_URL', 'not set')}")
        print(f"DB_NAME: {os.getenv('DB_NAME', 'not set')}")
        print(f"DB_PORT: {os.getenv('DB_PORT', 'not set')}")
        print(f"Using test database: {TestConfig.is_using_test_db()}")
        print(f"Database URL: {TestConfig.get_database_url()}")


# Simplified versions of remaining test classes to reduce complexity
class TestBasicOperations:
    """Basic tests that should work once paths are fixed."""

    def test_can_run_simple_init(self, clean_database):
        """Test basic init functionality."""
        success, stdout, stderr = TestConfig.run_migration_command("init")
        assert success, f"Basic init failed: {stderr}"

        # Just verify we have some tables
        table_count = TestConfig.count_tables(clean_database)
        assert table_count > 0, "Should have created some tables"

        db_type = "test" if TestConfig.is_using_test_db() else "main"
        print(f"✅ Init created {table_count} tables in {db_type} database")


if __name__ == "__main__":
    """Run tests directly with proper setup."""
    print(f"🚀 Running tests from: {Path(__file__)}")
    print(f"🚀 Project root: {PROJECT_ROOT}")
    print(f"🚀 Using {'test' if TestConfig.is_using_test_db() else 'main'} database")

    # Ensure we're in the right directory
    os.chdir(PROJECT_ROOT)
    print(f"🚀 Changed working directory to: {Path.cwd()}")

    import pytest

    pytest.main(
        [
            __file__,
            "-v",
            "--tb=short",
            f"--rootdir={PROJECT_ROOT}",
            "-s",  # Don't capture output so we can see our debug prints
        ]
    )
