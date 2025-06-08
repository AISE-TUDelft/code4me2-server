"""
Alembic Environment

This handles database connections and model imports for your specific setup.
"""

import os
import sys
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Add src directory to Python path
current_dir = os.path.dirname(__file__)  # src/database/migration
project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))  # project root
src_dir = os.path.join(project_root, 'src')
sys.path.insert(0, src_dir)

# Import database models
try:
    from database.db_schemas import Base

    target_metadata = Base.metadata
except ImportError as e:
    print(f"Warning: Could not import models: {e}")
    target_metadata = None

# Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_database_url():
    """Get database URL from environment variables."""
    # Check if we're in test mode
    test_mode = os.getenv('TEST_MODE', 'false').lower() == 'true'

    if test_mode:
        # Use test database
        return os.getenv('TEST_DATABASE_URL', 'postgresql://postgres:postgres@localhost:5433/test_db')

    # Use main database from .env
    db_user = os.getenv('DB_USER', 'postgres')
    db_password = os.getenv('DB_PASSWORD', 'postgres')
    db_host = os.getenv('DB_HOST', 'localhost')
    db_port = os.getenv('DB_PORT', '2345')
    db_name = os.getenv('DB_NAME', 'code4meV2')

    return f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section)
    configuration['sqlalchemy.url'] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()