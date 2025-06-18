# Code4me V2 Database Documentation




## 🔍 Overview

The Code4me V2 database is built on **PostgreSQL 16+ with pgvector extension** for AI-powered code completion with vector similarity search. The system uses **SQLAlchemy 2.0+ ORM** with **Alembic migrations** for schema management.

### Key Features
- **Vector Search**: pgvector extension for semantic code similarity
- **Polymorphic Inheritance**: Flexible query type system using table inheritance
- **Comprehensive Telemetry**: Detailed analytics for AI model performance
- **Session Management**: User sessions and project collaboration
- **Hybrid Migrations**: SQL initialization + Alembic migrations

### Architecture Overview

```mermaid
graph TB
    subgraph "Application Layer"
        FastAPI[FastAPI Backend]
        SQLAlchemy[SQLAlchemy ORM]
    end

    subgraph "Database Layer"
        PostgreSQL[PostgreSQL 16+]
        pgvector[pgvector Extension]
        Indexes[Optimized Indexes]
    end

    subgraph "Migration System"
        Alembic[Alembic Migrations]
        InitSQL[init.sql / init_test.sql]
        MigrationManager[Migration Manager]
    end

    FastAPI --> SQLAlchemy
    SQLAlchemy --> PostgreSQL
    PostgreSQL --> pgvector
    PostgreSQL --> Indexes
    MigrationManager --> Alembic
    MigrationManager --> InitSQL
    Alembic --> PostgreSQL
```

## 🗄️ Database Schema

### Core Configuration Tables

#### `config`
System-wide configuration storage.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `config_id` | BIGSERIAL | PK | Primary key |
| `config_data` | TEXT | NOT NULL | JSON configuration data |

### User Management

#### `user`
Core user accounts and authentication.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | UUID | PK | Primary key |
| `joined_at` | TIMESTAMP | NOT NULL | Account creation timestamp |
| `email` | VARCHAR | UNIQUE, NOT NULL | User email address |
| `name` | VARCHAR | NOT NULL | Display name |
| `password` | VARCHAR | NULL | Argon2 hashed password |
| `is_oauth_signup` | BOOLEAN | NOT NULL | OAuth vs standard signup |
| `verified` | BOOLEAN | NOT NULL, DEFAULT FALSE | Email verification status |
| `config_id` | BIGINT | FK | Reference to user config |
| `preference` | TEXT | NULL | JSON user preferences |
| `auth_token` | UUID | NULL | Current authentication token |

**Relationships:**
- `config_id` → `config.config_id` (SET NULL)

**Indexes:**
- `idx_user_email` (UNIQUE)
- `idx_user_auth_token`

### Project Collaboration

#### `project`
Coding projects with multi-file context support.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `project_id` | UUID | PK | Primary key |
| `project_name` | VARCHAR | NOT NULL | Human-readable project name |
| `multi_file_contexts` | TEXT | DEFAULT '{}' | JSON: related file contexts |
| `multi_file_context_changes` | TEXT | DEFAULT '{}' | JSON: context change history |
| `created_at` | TIMESTAMP | NOT NULL | Project creation timestamp |

#### `project_users`
Project membership and access control.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `project_id` | UUID | PK, FK | Reference to project |
| `user_id` | UUID | PK, FK | Reference to user |
| `joined_at` | TIMESTAMP | NOT NULL | When user joined project |

**Relationships:**
- `project_id` → `project.project_id` (CASCADE)
- `user_id` → `user.user_id` (CASCADE)

**Indexes:**
- `idx_project_users_project_id`
- `idx_project_users_user_id`

### Session Management

#### `session`
User coding sessions for analytics.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `session_id` | UUID | PK | Primary key |
| `user_id` | UUID | FK | Session owner |
| `start_time` | TIMESTAMP | NOT NULL | Session start timestamp |
| `end_time` | TIMESTAMP | NULL | Session end timestamp |

**Relationships:**
- `user_id` → `user.user_id` (SET NULL)

#### `session_projects`
Session-project associations (many-to-many).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `session_id` | UUID | PK, FK | Reference to session |
| `project_id` | UUID | PK, FK | Reference to project |

**Relationships:**
- `session_id` → `session.session_id` (CASCADE)
- `project_id` → `project.project_id` (CASCADE)

### Chat System

#### `chat`
Project-based chat conversations.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `chat_id` | UUID | PK | Primary key |
| `project_id` | UUID | FK, NOT NULL | Parent project |
| `user_id` | UUID | FK, NOT NULL | Chat owner |
| `title` | VARCHAR | NOT NULL | Chat title |
| `created_at` | TIMESTAMP | NOT NULL | Creation timestamp |

**Relationships:**
- `project_id` → `project.project_id` (CASCADE)
- `user_id` → `user.user_id` (SET NULL)

### AI Model Configuration

#### `model_name`
Available AI models for code completion.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `model_id` | BIGSERIAL | PK | Primary key |
| `model_name` | VARCHAR | UNIQUE, NOT NULL | Model identifier |
| `model_description` | TEXT | NULL | Model description |

#### `programming_language`
Supported programming languages.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `language_id` | BIGSERIAL | PK | Primary key |
| `language_name` | VARCHAR | UNIQUE, NOT NULL | Language name (e.g., 'python') |

#### `trigger_type`
Code completion trigger types.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `trigger_type_id` | BIGSERIAL | PK | Primary key |
| `trigger_name` | VARCHAR | UNIQUE, NOT NULL | Trigger type name |

#### `plugin_version`
JetBrains plugin version tracking.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `version_id` | BIGSERIAL | PK | Primary key |
| `version_name` | VARCHAR | UNIQUE, NOT NULL | Version string |

### Context and Telemetry

#### `context`
Code context information for AI completions.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `context_id` | UUID | PK | Primary key |
| `prefix` | TEXT | NULL | Code before cursor |
| `suffix` | TEXT | NULL | Code after cursor |
| `file_name` | TEXT | NULL | Source file name |
| `selected_text` | TEXT | NULL | User-selected text |

#### `contextual_telemetry`
Environment and context metadata.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `contextual_telemetry_id` | UUID | PK | Primary key |
| `version_id` | BIGINT | FK, NOT NULL | Plugin version |
| `trigger_type_id` | BIGINT | FK, NOT NULL | How completion was triggered |
| `language_id` | BIGINT | FK, NOT NULL | Programming language |
| `file_path` | TEXT | NULL | Full file path |
| `caret_line` | INTEGER | NULL | Cursor line number |
| `document_char_length` | INTEGER | NULL | Total document length |
| `relative_document_position` | DOUBLE | NULL | Cursor position (0.0-1.0) |

**Relationships:**
- `version_id` → `plugin_version.version_id` (RESTRICT)
- `trigger_type_id` → `trigger_type.trigger_type_id` (RESTRICT)
- `language_id` → `programming_language.language_id` (RESTRICT)

#### `behavioral_telemetry`
User behavior and interaction patterns.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `behavioral_telemetry_id` | UUID | PK | Primary key |
| `time_since_last_shown` | BIGINT | NULL | Time since last completion (ms) |
| `time_since_last_accepted` | BIGINT | NULL | Time since last acceptance (ms) |
| `typing_speed` | DOUBLE | NULL | Current typing speed |

### Query System (Polymorphic Inheritance)

#### `meta_query`
Base table for all query types using table inheritance.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `query_id` | UUID | PK | Primary key |
| `user_id` | UUID | FK, NOT NULL | Query owner |
| `session_id` | UUID | FK, NOT NULL | Associated session |
| `project_id` | UUID | FK, NOT NULL | Associated project |
| `timestamp` | TIMESTAMP | NOT NULL | Query timestamp |
| `context_id` | UUID | FK | Code context |
| `contextual_telemetry_id` | UUID | FK | Environment telemetry |
| `behavioral_telemetry_id` | UUID | FK | Behavioral telemetry |

**Relationships:**
- `user_id` → `user.user_id` (SET NULL)
- `session_id` → `session.session_id` (SET NULL)
- `project_id` → `project.project_id` (SET NULL)
- `context_id` → `context.context_id` (SET NULL)
- `contextual_telemetry_id` → `contextual_telemetry.contextual_telemetry_id` (SET NULL)
- `behavioral_telemetry_id` → `behavioral_telemetry.behavioral_telemetry_id` (SET NULL)

#### `completion_query`
Code completion requests (inherits from meta_query).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `meta_query_id` | UUID | PK, FK | Reference to meta_query |

**Relationships:**
- `meta_query_id` → `meta_query.meta_query_id` (CASCADE)

#### `had_generation`
AI model responses for completions.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `meta_query_id` | UUID | PK, FK | Parent meta query |
| `model_id` | BIGINT | PK, FK | AI model used |
| `completion` | TEXT | NOT NULL | Generated completion |
| `generation_time` | INTEGER | NOT NULL | Time taken (milliseconds) |
| `shown_at` | TIMESTAMP[] | NOT NULL | When completion was shown |
| `was_accepted` | BOOLEAN | NOT NULL | User acceptance status |
| `confidence` | DOUBLE | NOT NULL | Model confidence score |
| `logprobs` | DOUBLE[] | NOT NULL | Log probabilities array |

**Relationships:**
- `meta_query_id` → `meta_query.meta_query_id` (CASCADE)
- `model_id` → `model_name.model_id` (RESTRICT)

#### `ground_truth`
Ground truth data for model evaluation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `ground_truth_id` | UUID | PK | Primary key |
| `completion_query_id` | UUID | FK, NOT NULL | Associated completion query |
| `truth_timestamp` | TIMESTAMP | NOT NULL | When truth was recorded |
| `ground_truth` | TEXT | NOT NULL | Actual correct completion |

**Relationships:**
- `completion_query_id` → `completion_query.completion_query_id` (CASCADE)

### Documentation with Vector Search

#### `documentation`
Code documentation with semantic embeddings.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `documentation_id` | BIGSERIAL | PK | Primary key |
| `content` | TEXT | NOT NULL | Documentation content |
| `language` | VARCHAR | NULL | Programming language |
| `embedding` | VECTOR(384) | NULL | Semantic embedding vector |

**Indexes:**
- Vector similarity index on `embedding` column using pgvector
- 
## 🔄 Migration System

The Code4me V2 migration system uses a **hybrid approach** combining SQL initialization with Alembic migrations for seamless development workflows.

### Migration Architecture

```mermaid
graph TB
    subgraph "First-Time Setup"
        A[Empty Database] --> B[Execute init.sql/init_test.sql]
        B --> C[Create Baseline Migration]
        C --> D[Mark as Current Head]
    end

    subgraph "Ongoing Development"
        E[Schema Changes] --> F[Create Migration]
        F --> G[Review Migration]
        G --> H[Apply Migration]
    end

    D --> E
```

### Migration Manager CLI

#### Quick Start Commands

```bash
# Initialize migration system (first time)
python src/database/migration/migration_manager.py init

# Check system status
python src/database/migration/migration_manager.py status

# Create new migration after schema changes
python src/database/migration/migration_manager.py create "Add user last_login column"

# Apply all pending migrations
python src/database/migration/migration_manager.py migrate
```

#### Available Commands

| Command | Purpose | Example |
|---------|---------|---------|
| `init` | Initialize migration system | `python migration_manager.py init` |
| `create` | Create new migration | `python migration_manager.py create "Add index"` |
| `migrate` | Apply pending migrations | `python migration_manager.py migrate` |
| `current` | Show current revision | `python migration_manager.py current` |
| `history` | Show migration history | `python migration_manager.py history` |
| `status` | Detailed system status | `python migration_manager.py status` |
| `reset` | Reset database (destructive) | `python migration_manager.py reset` |

### Migration Workflow

#### 1. First-Time Setup

```bash
# Start database
docker-compose up -d db  # or test_db for testing

# Initialize migration system
python src/database/migration/migration_manager.py init
```

**What happens:**
1. Detects empty database
2. Executes `init.sql` (or `init_test.sql` for test DB)
3. Creates baseline Alembic migration
4. Sets up tracking for future migrations

#### 2. Schema Changes

```bash
# 1. Edit SQLAlchemy models in db_schemas.py
# Add/modify/remove columns, tables, indexes, etc.

# 2. Create migration
python src/database/migration/migration_manager.py create "Add user preferences table"

# 3. Review generated migration
cat src/database/migration/versions/*_add_user_preferences_table.py

# 4. Apply migration
python src/database/migration/migration_manager.py migrate
```

#### 3. Development vs Production

**Development (Test Database):**
```bash
# Use test database for safe experimentation
export TEST_MODE=true
python src/database/migration/migration_manager.py init
```

**Production:**
```bash
# Use main database
python src/database/migration/migration_manager.py init
```

### Generated Migration Example

```python
"""Add user last_login column

Revision ID: abc123def456
Revises: def456abc123
Create Date: 2024-01-15 10:30:00.123456
"""
from alembic import op
import sqlalchemy as sa

revision = 'abc123def456'
down_revision = 'def456abc123'
branch_labels = None
depends_on = None

def upgrade() -> None:
    """Upgrade database schema."""
    op.add_column('user', sa.Column('last_login', sa.DateTime(timezone=True), nullable=True))
    op.create_index('idx_user_last_login', 'user', ['last_login'])

def downgrade() -> None:
    """Downgrade database schema."""
    op.drop_index('idx_user_last_login', table_name='user')
    op.drop_column('user', 'last_login')
```

### Migration Best Practices

#### Schema Changes
- **Always test migrations** on development database first
- **Review generated migrations** before applying to production
- **Use descriptive migration messages** for easy identification
- **Consider backwards compatibility** for zero-downtime deployments

#### Data Migrations
```python
# Example: Populate new column with default values
def upgrade() -> None:
    # Add column
    op.add_column('user', sa.Column('last_login', sa.DateTime(timezone=True), nullable=True))
    
    # Populate with current timestamp for existing users
    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE user SET last_login = joined_at WHERE last_login IS NULL")
    )
```


### Documentation with Vector Search

```python
# Create documentation with embedding
doc_data = Queries.CreateDocumentation(
    content="This function calculates the Fibonacci sequence",
    language="python"
)
doc = crud.create_documentation(db, doc_data)

# Search similar documentation
similar_docs = crud.search_similar_documentation(
    db, 
    query_text="fibonacci calculation", 
    limit=5,
    similarity_threshold=0.8
)
```

## 🚀 Database Setup

### Development Setup

```bash
# 1. Start PostgreSQL with pgvector
docker-compose up -d db

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Set environment variables
export PYTHONPATH=$PWD/src
export DB_HOST=localhost
export DB_PORT=5432
export DB_NAME=code4meV2
export DB_USER=postgres
export DB_PASSWORD=postgres

# 4. Initialize database
python src/database/migration/migration_manager.py init

# 5. Verify setup
python src/database/migration/migration_manager.py status
```


### Environment Variables

```bash
# Database Configuration
DB_HOST=localhost
DB_PORT=5432
DB_NAME=code4meV2
DB_USER=postgres
DB_PASSWORD=secure_password

# Test Database (optional)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/test_db

# Migration Settings
TEST_MODE=false  # Set to true for test database
```

## ⚡ Performance Optimization

### Indexing Strategy

```sql
-- User table indexes
CREATE INDEX CONCURRENTLY idx_user_email ON "user"(email);
CREATE INDEX CONCURRENTLY idx_user_auth_token ON "user"(auth_token);
CREATE INDEX CONCURRENTLY idx_user_verified ON "user"(verified);

-- Completion query indexes
CREATE INDEX CONCURRENTLY idx_completion_query_user_id ON completion_query(user_id);
CREATE INDEX CONCURRENTLY idx_completion_query_project_id ON completion_query(project_id);
CREATE INDEX CONCURRENTLY idx_completion_query_timestamp ON completion_query(timestamp);

-- Generation indexes for analytics
CREATE INDEX CONCURRENTLY idx_had_generation_model_id ON had_generation(model_id);
CREATE INDEX CONCURRENTLY idx_had_generation_accepted ON had_generation(accepted);
CREATE INDEX CONCURRENTLY idx_had_generation_timestamp ON had_generation(generation_timestamp);

-- Vector similarity index
CREATE INDEX CONCURRENTLY idx_documentation_embedding ON documentation 
USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```
`

## 🔍 Troubleshooting

### Common Issues

#### Migration System Not Initialized
```bash
# Error: Database connection failed
# Solution: Start database first
docker-compose up -d db

# Error: alembic.ini not found
# Solution: Ensure you're in project root
cd /path/to/code4me-v2
python src/database/migration/migration_manager.py init
```

#### Connection Issues
```bash
# Check database status
docker-compose ps
docker-compose logs db

# Test connection manually
psql -h localhost -p 5432 -U postgres -d code4meV2
```

#### Migration Conflicts
```bash
# Multiple migration heads
python src/database/migration/migration_manager.py history
alembic merge -m "Merge conflicting migrations"

# Reset database (destructive)
python src/database/migration/migration_manager.py reset
```


## 📚 Additional Resources

### SQLAlchemy Resources
- **[SQLAlchemy 2.0 Documentation](https://docs.sqlalchemy.org/en/20/)**
- **[Alembic Documentation](https://alembic.sqlalchemy.org/)**
- **[PostgreSQL Documentation](https://www.postgresql.org/docs/)**

### pgvector Resources
- **[pgvector GitHub](https://github.com/pgvector/pgvector)**
- **[Vector Similarity Search Guide](https://github.com/pgvector/pgvector#querying)**

### Code References
- **[Database Models](../db_schemas.py)** - SQLAlchemy ORM definitions
- **[CRUD Operations](../crud.py)** - Database operation functions
- **[Migration Manager](migration/migration_manager.py)** - Migration CLI tool
- **[Initialization Scripts](../init.sql)** - Database schema setup

---

## Database Schema Diagram

```mermaid
erDiagram
    CONFIG {
        bigint config_id PK
        text config_data
    }

    USER {
        uuid user_id PK
        timestamp joined_at
        varchar email UK
        varchar name
        varchar password
        boolean is_oauth_signup
        boolean verified
        bigint config_id FK
        text preference
        uuid auth_token
    }

    PROJECT {
        uuid project_id PK
        varchar project_name
        text multi_file_contexts
        text multi_file_context_changes
        timestamp created_at
    }

    PROJECT_USERS {
        uuid project_id PK,FK
        uuid user_id PK,FK
        timestamp joined_at
    }

    SESSION {
        uuid session_id PK
        uuid user_id FK
        timestamp start_time
        timestamp end_time
    }

    SESSION_PROJECTS {
        uuid session_id PK,FK
        uuid project_id PK,FK
    }

    CHAT {
        uuid chat_id PK
        uuid project_id FK
        uuid user_id FK
        varchar title
        timestamp created_at
    }

    MODEL_NAME {
        bigserial model_id PK
        varchar model_name UK
        text model_description
    }

    PROGRAMMING_LANGUAGE {
        bigserial language_id PK
        varchar language_name UK
    }

    TRIGGER_TYPE {
        bigserial trigger_type_id PK
        varchar trigger_name UK
    }

    PLUGIN_VERSION {
        bigserial version_id PK
        varchar version_name UK
    }

    CONTEXT {
        uuid context_id PK
        text prefix
        text suffix
        text file_name
        text selected_text
    }

    CONTEXTUAL_TELEMETRY {
        uuid contextual_telemetry_id PK
        bigint version_id FK
        bigint trigger_type_id FK
        bigint language_id FK
        text file_path
        integer caret_line
        integer document_char_length
        double relative_document_position
    }

    BEHAVIORAL_TELEMETRY {
        uuid behavioral_telemetry_id PK
        bigint time_since_last_shown
        bigint time_since_last_accepted
        double typing_speed
    }

    META_QUERY {
        uuid meta_query_id PK
        uuid user_id FK
        uuid contextual_telemetry_id FK
        uuid behavioral_telemetry_id FK
        uuid context_id FK
        uuid session_id FK
        uuid project_id FK
        text multi_file_context_changes_indexes
        timestamp timestamp
        integer total_serving_time
        bigint server_version_id
        varchar query_type
    }

    COMPLETION_QUERY {
        uuid meta_query_id PK,FK
    }

    CHAT_QUERY {
        uuid meta_query_id PK,FK
        uuid chat_id FK
        boolean web_enabled
    }

    HAD_GENERATION {
        uuid meta_query_id PK,FK
        bigint model_id PK,FK
        text completion
        integer generation_time
        timestamp_array shown_at
        boolean was_accepted
        double confidence
        double_array logprobs
    }

    GROUND_TRUTH {
        uuid completion_query_id PK,FK
        timestamp truth_timestamp PK
        text ground_truth
    }

    DOCUMENTATION {
        serial documentation_id PK
        text content
        varchar language
        vector_384 embedding
        timestamp created_at
    }

    %% Relationships
    USER ||--o{ CONFIG : references
    PROJECT ||--o{ PROJECT_USERS : contains
    USER ||--o{ PROJECT_USERS : belongs_to
    SESSION ||--o{ SESSION_PROJECTS : spans
    PROJECT ||--o{ SESSION_PROJECTS : includes
    USER ||--o{ SESSION : owns
    PROJECT ||--o{ CHAT : contains
    USER ||--o{ CHAT : creates
    
    CONTEXTUAL_TELEMETRY ||--|| PLUGIN_VERSION : uses
    CONTEXTUAL_TELEMETRY ||--|| TRIGGER_TYPE : triggered_by
    CONTEXTUAL_TELEMETRY ||--|| PROGRAMMING_LANGUAGE : for_language
    
    META_QUERY ||--o| USER : created_by
    META_QUERY ||--o| SESSION : within
    META_QUERY ||--|| PROJECT : for_project
    META_QUERY ||--o| CONTEXT : has_context
    META_QUERY ||--o| CONTEXTUAL_TELEMETRY : has_contextual
    META_QUERY ||--o| BEHAVIORAL_TELEMETRY : has_behavioral
    
    COMPLETION_QUERY ||--|| META_QUERY : inherits
    CHAT_QUERY ||--|| META_QUERY : inherits
    CHAT_QUERY ||--|| CHAT : belongs_to
    
    HAD_GENERATION ||--|| META_QUERY : generated_for
    HAD_GENERATION ||--|| MODEL_NAME : generated_by
    
    GROUND_TRUTH ||--|| COMPLETION_QUERY : truth_for
```





This documentation is maintained alongside the codebase and updated with each schema change. 