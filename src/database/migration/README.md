# Migration System Documentation - Code4me V2

## Overview

The Code4me V2 migration system uses a **hybrid approach** that combines the convenience of SQL initialization files with the power of Alembic migrations. This system automatically detects first-time setups and seamlessly transitions to standard migration workflows for ongoing development.

The instructions are mainly given with test_db to ensure safe experimentation.



## 🚀 Quick Start

### 1. First-Time Setup

```bash
# Start test database
docker-compose up test_db

# Initialize migration system (auto-detects empty database)
python src/database/migration/migration_manager.py init

# Verify setup
python src/database/migration/migration_manager.py status
```

**What happens during `init`:**
1. Detects empty database
2. Executes `init.sql` to create all tables and data
3. Sets up Alembic tracking with baseline migration
4. Marks current state as migration head

### 2. Creating Your First Migration

```bash
# Make changes to db_schemas.py
# Then create migration
python src/database/migration/migration_manager.py create "Add user last_login column"

# Review generated migration
# src/database/migration/versions/xxxx_add_user_last_login_column.py

# Apply migration
python src/database/migration/migration_manager.py migrate
```

### 3. Ongoing Development

```bash
# Check current state
python src/database/migration/migration_manager.py current

# View migration history
python src/database/migration/migration_manager.py history

# Create new migration
python src/database/migration/migration_manager.py create "Your change description"

# Apply all pending migrations
python src/database/migration/migration_manager.py migrate
```

## 🛠️ Migration Manager CLI

### Core Commands

#### `init` - Initialize Migration System
```bash
python src/database/migration/migration_manager.py init
```

**Behavior:**
- **Empty Database**: Runs `init_test.sql`, sets up tracking
- **Existing Tables**: Sets up tracking only  
- **Already Initialized**: No-op, reports status

**Output Example:**
```
Initializing migration system...
Database is empty - initializing from init.sql...
Database initialized from init.sql
Setting up migration tracking...
Migration tracking set up
Migration system ready!
Use 'create' to add new migrations, 'migrate' to apply them
```

#### `create` - Create New Migration
```bash
python src/database/migration/migration_manager.py create "Migration description"
```

**Features:**
- Auto-generates migration from model changes
- Creates empty migration for manual changes
- Ensures system is initialized first

**Generated Migration Example:**
```python
"""Add user last_login column

Revision ID: abc123def456
Revises: def456abc123
Create Date: 2024-01-15 10:30:00.123456
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'abc123def456'
down_revision = 'def456abc123'
branch_labels = None
depends_on = None

def upgrade() -> None:
    """Upgrade database schema."""
    op.add_column('user', sa.Column('last_login', sa.DateTime(), nullable=True))

def downgrade() -> None:
    """Downgrade database schema."""
    op.drop_column('user', 'last_login')
```

#### `migrate` - Apply Migrations
```bash
python src/database/migration/migration_manager.py migrate
```

**Behavior:**
- Ensures system is initialized
- Applies all pending migrations
- Reports progress and errors

#### `current` - Show Current State
```bash
python src/database/migration/migration_manager.py current
```

**Output Example:**
```
abc123def456 (head)
```

#### `history` - Show Migration History
```bash
python src/database/migration/migration_manager.py history
```

**Output Example:**
```
def456abc123 -> abc123def456 (head), Add user last_login column
123456abcdef -> def456abc123, Baseline migration from init_test.sql
<base> -> 123456abcdef, Initial schema
```

#### `status` - Detailed System Status
```bash
python src/database/migration/migration_manager.py status
```

**Output Example:**
```
Migration System Status
========================================
Database: Connected
Database: Initialized
Tables: 19
Migration tracking: Set up
Current version: abc123def456
```

#### `reset` - Reset Database (Destructive)
```bash
python src/database/migration/migration_manager.py reset
```

**Warning:** This drops all data and recreates the database from scratch.



### 1. Adding a New Column

```bash
# 1. Edit SQLAlchemy model
# In db_schemas.py:
class User(Base):
    # ... existing fields ...
    last_login = Column(DateTime(timezone=True), nullable=True)

# 2. Create migration
python src/database/migration/migration_manager.py create "Add user last_login column"

# 3. Review generated migration file
cat src/database/migration/versions/*_add_user_last_login_column.py

# 4. Test migration
python run_tests.py --specific test_migration

# 5. Apply migration
python src/database/migration/migration_manager.py migrate

# 6. Verify changes
python src/database/migration/migration_manager.py current
```

## 🐳 Docker Integration

### Development Setup

```yaml
# docker-compose.yml
services:
  test_db:
    image: postgres:16
    container_name: postgres_test
    ports:
      - "5433:5432"
    environment:
      - POSTGRES_PASSWORD=postgres
      - POSTGRES_USER=postgres
      - POSTGRES_DB=test_db
    volumes:
      - ./src/database/init_test.sql:/docker-entrypoint-initdb.d/init_test.sql
```

### Commands

```bash
# Start test database
docker-compose up test_db

# Run migrations in container
docker-compose exec backend python src/database/migration/migration_manager.py migrate

# Run tests
docker-compose run --rm backend python run_tests.py
```

### Recovery Procedures

#### Complete Reset (Development Only)
```bash
# WARNING: This destroys all data!
python src/database/migration/migration_manager.py reset
```

