# Code4me V2 - AI-Powered Code Completion Platform

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![React](https://img.shields.io/badge/react-19+-blue.svg)](https://reactjs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115.0+-green.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://docker.com)
[![License](https://img.shields.io/badge/License-Apache_2.0-brightgreen.svg)](https://www.apache.org/licenses/LICENSE-2.0)

An advanced AI-powered code completion platform, featuring ghost text suggestions, multi-model inference, a context-aware chat assistant, an autonomous coding agent (ACP), real-time WebSocket communication, and collaborative development tools suitable for conducting empirical studies on developer behaviour.

## 🎯 Project Overview

Code4me V2 is a research platform that combines transformer models with real-world developer workflows through a JetBrains Plugin integration. It provides:

- **Real-time Code Completion**: Multi-model AI inference with WebSocket streaming
- **Collaborative Development**: Multi-user project management and session tracking
- **Autonomous Coding Agent**: A locally-launched ACP (Agent Client Protocol) agent, with server-assigned model/provider/tools for A/B study arms — see "Agent Subsystem (ACP)" under System Architecture below
- **Advanced Analytics**: Comprehensive telemetry and behavioral analysis in a dedicated analysis platform
- **Research Platform**: Ground truth collection and model evaluation tools

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose (version 2.0+)
- 8GB+ RAM (16GB+ recommended for production)
- NVIDIA GPU with Docker support (for AI model inference)
- 30GB+ free disk space (for model cache and data storage)

### 1. Clone and Setup
```bash
git clone <repository-url>
cd code4me2-server
chmod +x setup_data_dir.sh
./setup_data_dir.sh
```

### 2. Environment Configuration
Create a `.env` file with required variables:
```bash
# Database Configuration
DB_HOST=db
DB_PORT=5432
DB_NAME=code4meV2
DB_USER=postgres
DB_PASSWORD=your_secure_password

# Redis Configuration
REDIS_HOST=redis
REDIS_PORT=6379
CELERY_BROKER_HOST=redis-celery
CELERY_BROKER_PORT=6379

# Server Configuration
SERVER_HOST=0.0.0.0
SERVER_PORT=8008

# Hugging Face (for AI models)
HF_TOKEN=your_huggingface_token

# Data Directory
DATA_DIR=./data

# Authentication & Security
AUTHENTICATION_TOKEN_EXPIRES_IN_SECONDS=3600
SESSION_TOKEN_EXPIRES_IN_SECONDS=3600

# Email Configuration (for user verification)
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USERNAME=your_email@gmail.com
EMAIL_PASSWORD=your_app_password
EMAIL_FROM=noreply@code4me.com

# Agent Subsystem (ACP) — upstream provider for the coding agent.
# Defaults to a local Ollama instance (no key, no cost). See .env.example
# for the full list of agent variables and the "Agent Subsystem" section below.
AGENT_UPSTREAM_BASE_URL=http://localhost:11434/v1
AGENT_UPSTREAM_API_KEY=
```

See [`.env.example`](.env.example) for the complete, documented set of agent-specific variables (per-provider API keys, Codex reasoning effort override, etc.).

### 3. Deploy with Docker
```bash
# Start all services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f backend
```

### 4. Access the Application
- **Frontend Dashboard**: http://localhost:8000
- **API Documentation**: http://localhost:8008/docs
- **Health Check**: `HEAD http://localhost:8008/api/ping`

### 5. Verify Services are Healthy
```bash
curl -s http://localhost:8008/api/ping | jq .
docker-compose ps
```

## 🏗️ System Architecture

### Services Overview
| Service | Port | Purpose | Dependencies |
|---------|------|---------|-------------|
| **nginx** | 8000 | Reverse proxy and load balancer | website, backend |
| **backend** | 8008 | FastAPI application server | db, redis, redis-celery |
| **website** | 3000 | React frontend application | backend |
| **db** | 5432 | PostgreSQL with pgvector extension | None |
| **redis** | 6379 | Session storage and caching | None |
| **redis-celery** | 6380 | Celery message broker | None |
| **celery-worker** | - | AI/ML background processing | redis-celery, db |

### Core Components

#### 🤖 AI/ML Processing
- **Multi-Model Support**: Simultaneous inference from multiple transformer models
- **Real-time Streaming**: WebSocket-based completion delivery with sub-second response times
- **Context Awareness**: Multi-file context analysis for improved completion relevance
- **Background Processing**: Celery task queue for AI inference and database operations

#### 🔌 Real-time WebSocket Features
- **Code Completion Streaming**: `/api/ws/completion` - Real-time code completion
- **Project Chat System**: `/api/ws/chat` - Collaborative chat functionality
- **Multi-file Context**: `/api/ws/multi-file-context` - Context updates

#### 🤖 Agent Subsystem (ACP)
The `backend` service is also a relay + telemetry sink for an autonomous coding agent (it does not run agent inference loops itself):

- **Third-party agents** (e.g. Goose, Codex) run inside the IDE plugin process and call back through `POST /api/agent/inference`, which the backend proxies to an OpenAI-compatible upstream (Ollama, OpenAI, Groq, OpenRouter, or any compatible endpoint).
- **The built-in `code4me2-agent`** runs as a separate local OS process, launched by the IDE plugin, speaking ACP over stdio. It authenticates via a grant → session handoff: the plugin calls `POST /api/acp/grant`, the agent process exchanges it for a bearer token at `POST /api/acp/session/exchange`, then fetches its assigned model/provider/tools from `GET /api/acp/agent-config`.
- An **agent profile** (`agent_profile` table) defines a runtime + provider + model + tools + approval policy — used as an A/B study arm. An **agent assignment** is a sticky, server-authoritative per-user draw; the client never self-selects.
- Provider API keys are never stored in the database — a profile stores only the *name* of an environment variable (`api_key_ref`), resolved from the backend's own environment at request time. See [`.env.example`](.env.example).
- Installing and running the local `code4me2-agent` CLI is a separate, standalone step — see "Running the Agent CLI (`code4me2-agent`)" under Development below.

#### 🧩 Provider-backed classic chat/completion models

The classic endpoints (`POST /api/chat/request`, `POST /api/completion/request`) can be served by an OpenAI-compatible provider (OpenRouter, Ollama, OpenAI, Groq, vLLM) instead of a local HuggingFace model. A `model_name` row opts in through its `model_parameters` JSON; every other row keeps the local path unchanged. **No plugin change is required** — the plugin keeps sending `model_ids` and the server resolves the row.

**Default:** the rows the plugin uses by default are seeded provider-backed through OpenRouter (key from `OPENROUTER_API_KEY`): id 1 `deepseek-ai/deepseek-coder-1.3b-base` (completion) is answered by `mistralai/codestral-2508`, and id 3 `mistralai/Ministral-8B-Instruct-2410` (chat) by `mistralai/ministral-8b-2512`. Rows keep their names; `provider_model` records the model that answers. The other rows (StarCoder2, Mellum) run locally and are used only when explicitly chosen. `CLASSIC_MODELS_ENABLED=false` refuses the classic endpoints entirely (HTTP 503) without loading anything.

```json
{
  "provider": "openai_compatible",
  "kind": "chat",                          // "chat" | "completion"; default: "chat" for *instruct* model names, else "completion"
  "base_url": "https://openrouter.ai/api/v1",
  "api_key_ref": "OPENROUTER_API_KEY",     // optional; name of the env var holding the key (never the key itself)
  "provider_model": "mistralai/ministral-8b-2512",  // optional; default = model_name
  "endpoint": "chat",                      // "chat" (chat-completions) | "completions" (legacy /completions); default "chat"
  "max_new_tokens": 256,
  "temperature": 0.2,
  "top_p": 0.95,
  "timeout_seconds": 60
}
```

- `base_url` is required. `api_key_ref` is optional (a local Ollama needs no key), and like agent profiles it stores only the environment-variable *name*: the value is read from the backend's environment at request time and is never stored or logged. A referenced variable that is missing is a hard error — there is no unauthenticated fallback.
- `kind` selects the model class (chat vs. FIM completion) and defaults from the model name. `endpoint` selects the wire format for completion rows: `chat` sends one system message plus the formatted FIM prompt as a user message, `completions` posts the formatted prompt to `/completions` with `stop` sequences.
- Provider rows report no `confidence` (stored as NULL and left out of the calibration and model analytics) and empty `logprobs` (token logprobs exist only on the local path). A database created before this change needs `ALTER TABLE had_generation ALTER COLUMN confidence DROP NOT NULL;` once (the schema has a single consolidated revision, so existing databases are not migrated automatically); until then provider generations fail to save.
- Unknown `model_parameters` keys are rejected, so a typo cannot silently change the wire shape.

A database seeded before this default keeps local rows; adopt the default once with:

```sql
UPDATE model_name
SET model_parameters = '{"provider": "openai_compatible", "kind": "completion", "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "OPENROUTER_API_KEY", "provider_model": "mistralai/codestral-2508", "max_new_tokens": 64}'
WHERE model_name = 'deepseek-ai/deepseek-coder-1.3b-base';
UPDATE model_name
SET model_parameters = '{"provider": "openai_compatible", "kind": "chat", "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "OPENROUTER_API_KEY", "provider_model": "mistralai/ministral-8b-2512", "max_new_tokens": 256}'
WHERE model_name = 'mistralai/Ministral-8B-Instruct-2410';
```

To host a row locally instead (a HuggingFace model on this server; practical only with a GPU), set it back explicitly:

```sql
UPDATE model_name SET model_parameters = '{"max_new_tokens": 64}'
WHERE model_name = 'deepseek-ai/deepseek-coder-1.3b-base';
UPDATE model_name SET model_parameters = '{"max_new_tokens": 256}'
WHERE model_name = 'mistralai/Ministral-8B-Instruct-2410';
```

Set the referenced variable in `.env` (e.g. `OPENROUTER_API_KEY=...`) and restart the `backend` service. The `backend` container must be able to reach `base_url`; a host-run Ollama is `http://host.docker.internal:11434/v1` from inside Docker. The live end-to-end check is `E2E_CLASSIC_PROVIDER=1 python3 -m unittest tests.test_classic_provider_e2e -v`, run from `e2e/`.

#### 📊 Analytics & Telemetry
- **Behavioral Analytics**: Typing patterns, acceptance rates, interaction timings
- **Performance Metrics**: Model response times, accuracy measurements
- **Dashboard Visualizations**: System metrics, user activity, resource utilization
- **Research Data**: Ground truth collection for model evaluation

## 📚 API Reference

### Authentication & User Management
```http
POST   /api/user/create/             # Create new user account
POST   /api/user/authenticate/       # User login (email/password or OAuth)
GET    /api/user/get                 # Get current user profile
PUT    /api/user/update/             # Update user profile
DELETE /api/user/delete/             # Delete user account

# Email verification
POST   /api/user/verify/             # Verify email with token (query: ?token=...)
POST   /api/user/verify/resend       # Resend verification email
GET    /api/user/verify/check        # Check verification status

# Password reset flow
POST   /api/user/reset-password/request # Request password reset email (?email=...)
GET    /api/user/reset-password/        # Show password reset form (?token=...)
POST   /api/user/reset-password/change  # Submit new password (form: token, new_password)
```

### Session Management
```http
GET    /api/session/acquire         # Acquire or create a session (sets cookie)
PUT    /api/session/deactivate/     # Deactivate current session
```

### Project Management
```http
POST   /api/project/create       # Create new project
PUT    /api/project/activate     # Activate existing project (body: { project_id })
```

### Code Completion
```http
POST   /api/completion/request                     # Request code completion
GET    /api/completion/{query_id}                 # Retrieve completion results
POST   /api/completion/feedback                    # Submit completion feedback
POST   /api/completion/multi-file-context/update   # Update project context
```

### Chat System
```http
POST   /api/chat/request                            # Request chat completion
GET    /api/chat/get/{page_number}                  # Paginated chat history
DELETE /api/chat/delete/{chat_id}                   # Delete chat session
```

### Agent Subsystem (ACP)
```http
# Profiles & assignments (admin auth)
GET    /api/agent/profiles                    # List agent profiles
POST   /api/agent/profiles                    # Create agent profile
GET    /api/agent/available-tools             # List tools an adapter can expose
GET    /api/agent/registry                    # List available adapter frameworks
GET    /api/agent/assignments                 # List per-user profile assignments

# Self-report ingestion & memory (ACP bearer auth, from the local agent process)
POST   /api/agent/events/ingest               # Ingest agent run/edit telemetry
GET    /api/agent/runs/{run_id}               # Fetch a stored agent run
GET    /api/agent/memory/{session_id}         # Read persisted agent memory
PUT    /api/agent/memory/{session_id}         # Update persisted agent memory
DELETE /api/agent/memory/{session_id}         # Delete persisted agent memory

# Task lifecycle & inference relay (plugin session-cookie auth)
POST   /api/agent/task                        # Start an agent task
POST   /api/agent/task/{task_id}/close        # Close an agent task
POST   /api/agent/task/{task_id}/telemetry    # Report task-level telemetry
POST   /api/agent/inference                   # Relay a chat-completion call to the assigned upstream

# ACP grant handoff (bootstraps the locally launched agent process)
POST   /api/acp/grant                              # Issue a single-use grant (plugin -> backend)
POST   /api/acp/session/exchange                   # Exchange a grant for a bearer session
POST   /api/acp/session/validate-or-refresh        # Validate/refresh an ACP session
GET    /api/acp/agent-config                       # Fetch assigned model/provider/tools
POST   /api/acp/persistent-auth-token              # Issue a long-lived token for unattended runs (admin only)
```

### Real-time WebSocket Endpoints
```http
WS     /api/ws/completion                         # Real-time completion streaming
WS     /api/ws/chat                               # Project chat functionality
WS     /api/ws/completion/multi-file-context      # Multi-file context updates
```

### Example Requests
```bash
# Acquire a session (sets session cookie)
curl -i http://localhost:8008/api/session/acquire

# Request a completion (example body)
curl -s \
  -H "Content-Type: application/json" \
  -d '{
        "model_ids": [1],
        "context": {"prefix": "def add(a, b):\n  ", "suffix": ""},
        "contextual_telemetry": {"version_id": 1, "trigger_type_id": 1, "language_id": 1},
        "behavioral_telemetry": {}
      }' \
  http://localhost:8008/api/completion/request | jq .
```

## 🗄️ Database Architecture

### Core Tables
- **Users**: Authentication, profiles, and OAuth integration
- **Projects**: Collaborative workspaces with multi-file context
- **Sessions**: Development session tracking and analytics
- **Completions**: AI-generated suggestions with performance metrics
- **Telemetry**: Comprehensive usage analytics for research
- **Context**: Multi-file code context with vector embeddings
- **Agent tables**: `agent_profile`, `agent_profile_assignment`, `agent_task`, `agent_event`, `agent_edit`, `agent_memory`, `study_agent_profile` — coding agent A/B profiles, per-user assignments, task lifecycle, and self-reported telemetry/edits

### Key Features
- **PostgreSQL + pgvector**: Vector similarity search for code context
- **Polymorphic Inheritance**: Flexible query system for different completion types
- **Migration System**: Hybrid SQL initialization + Alembic migrations
- **Comprehensive Indexing**: Optimized for high-volume analytics queries

### Running Migrations
The agent/research tables (and any future schema change) are applied via the hybrid migration manager, not automatically on startup:
```bash
# First-time setup (initializes from init.sql + stamps Alembic tracking)
python src/database/migration/migration_manager.py init

# Apply pending migrations
python src/database/migration/migration_manager.py migrate

# Check current revision / history
python src/database/migration/migration_manager.py status
```
There is a single consolidated Alembic revision, `8a0084080b46_consolidated_schema.py` (fresh-schema only): it creates the research/agent schema and refuses to run against a database that already holds the previous research tables, so create a new database/volume instead of upgrading in place. Default agent profiles (`default-code4me2-agent`, `default-goose`, `default-codex`) are **not** created by a migration; they are seeded by the development seeder (`scripts/dev/seed_local_dev.sh`, see **Research Platform** below). Only `default-code4me2-agent` is seeded active by default; the Goose/Codex profiles are seeded inactive (BYOA) until you provide those external binaries.

**Current default profile (initial testing config)**: `default-code4me2-agent` points at OpenRouter (`https://openrouter.ai/api/v1`) using the free `cohere/north-mini-code:free` model, with `api_key_ref=OPENROUTER_API_KEY`. Set `OPENROUTER_API_KEY` in `.env` once you have a key — until then, requests are forwarded unauthenticated and OpenRouter will 401. Update the profile via `PUT /api/agent/profiles/{profile_id}` (or the admin UI) to switch models/providers later.

## ⚡ Asynchronous Processing

### Celery Task Queues
The system uses **Celery with Redis** for distributed task processing:

#### LLM Tasks (High Priority)
- **`completion_request_task`**: AI model inference for code completions
- **`completion_feedback_task`**: Process user feedback on completions
- **`chat_request_task`**: Handle conversational AI chat completions
- **`update_multi_file_context_task`**: Update project-wide code context

#### Database Tasks (Background Processing)
- **`add_completion_query_task`**: Store completion metadata for analytics
- **`add_generation_task`**: Store AI model responses and performance metrics
- **`add_telemetry_task`**: Store comprehensive user behavior analytics
- **`add_context_task`**: Store code context with semantic embeddings

### Task Processing Flow
1. **Authentication Validation**: Verify session and project tokens using Redis
2. **Model Loading**: Initialize AI models and completion services
3. **Request Processing**: Execute specific task (completion, feedback, chat, etc.)
4. **Result Publishing**: Send results back to WebSocket connection
5. **Error Handling**: Graceful error handling with client notification

## 💾 Redis Data Management

### Session Architecture
Redis manages authentication state, project access, and real-time connection tracking:

#### Token Hierarchy
- **Auth Token** → Primary authentication credential (1 hour lifetime)
- **Session Token** → Links to auth token + project access list (1 hour lifetime)
- **Project Tokens** → Project-specific access + multi-file contexts
- **User Token** → User preferences and session data

#### Data Structures
```redis
auth_token:uuid → {user_id, expires_at}
session_token:uuid → {auth_token, project_tokens, user_preferences}
project_token:uuid → {project_id, session_tokens, multi_file_contexts}
```

### Session Lifecycle
- **Creation**: Generate unique tokens with expiration hooks
- **Validation**: Real-time token verification for all requests
- **Cleanup**: Automatic cascading deletion on expiration
- **Persistence**: Important session data synced to PostgreSQL

## 🔐 Security & Privacy

### Multi-layered Security
- **Secret Detection**: Automatic detection and redaction of sensitive information

- **Rate Limiting**: Per-endpoint request throttling with configurable limits
- **Session Security**: HttpOnly cookies, SameSite protection, automatic expiration

### Secret Detection Features
Automatically detects and redacts:
- Cloud provider credentials (AWS, Azure, GCP)
- Development platform tokens (GitHub, GitLab, JetBrains)
- Communication tokens (Slack, Discord, Telegram)
- Payment system keys (Stripe, PayPal)
- AI/ML service keys (OpenAI, Hugging Face)
- High entropy secrets and authentication tokens

### Data Privacy & GDPR Compliance
- **User Consent**: Opt-in for telemetry collection
- **Data Minimization**: Only collect necessary data
- **Right to Deletion**: Complete user data removal
- **Data Portability**: Export user data in standard formats

## 🔧 Development

### Local Development Setup
```bash
# Start infrastructure only
docker-compose up -d db redis redis-celery

# Install Python dependencies
pip install -r requirements.txt

# Set environment
export PYTHONPATH=$PWD/src

# Run backend locally
python src/main.py
```

Notes:
- The API runs on `SERVER_HOST`/`SERVER_PORT` (default `0.0.0.0:8008`).
- When running via Docker, `nginx` serves the website on `http://localhost:8000` and proxies `/api/*` to the backend.

### Running the Agent CLI (`code4me2-agent`)
`code4me2-agent` is a **separate, standalone package** (`pyproject.toml` at the repo root) — it is not installed into the backend's `requirements.txt`/Docker image, since it runs as its own local OS process launched by the JetBrains plugin (or manually, for development), not inside the FastAPI server.

```bash
# Install the agent CLI package (from the repo root, ideally in its own venv)
pip install -e .

# Write default config files to ~/.code4me/ and print IDE setup instructions
code4me2-agent --setup

# Run it directly over stdio (normally the IDE plugin does this for you)
code4me2-agent
```
Notes:
- The agent has **no hardcoded model/provider/API key** — after it authenticates via the ACP grant handoff (`POST /api/acp/grant` → `POST /api/acp/session/exchange`), it fetches `GET /api/acp/agent-config` and the backend's assigned agent profile overrides its local config. This is deliberate: a stale local config can't silently override a study assignment.
- The seeded default profile currently points at OpenRouter (`cohere/north-mini-code:free`) — set `OPENROUTER_API_KEY` in `.env` before it will authenticate. If you instead assign a local-Ollama profile, make sure `ollama serve` is running before launching the agent.
- Local-process environment variables (set by whatever launches the agent, e.g. the plugin): `CODE4ME_ACP_BACKEND_URL`, `CODE4ME_ACP_GRANT`, `CODE4ME_ACP_TOKEN`, `CODE4ME_ACP_LOG_SECRETS`, `CODE4ME_AGENT_LOG_LEVEL`, `CODE4ME_MODEL_REQUEST_LIMIT`, `CODE4ME_MODEL_REQUEST_WINDOW_SECONDS`, `CODE4ME_BACKEND_429_MAX_RETRIES`, `CODE4ME_BACKEND_429_RETRY_SLEEP`, `CODE4ME_RATE_LIMIT_STATE_PATH`. These are distinct from the backend's own `AGENT_*`/`*_API_KEY` variables in `.env`.

### Frontend Development
```bash
cd src/website
npm install
npm start
```

### Testing
```bash
# Run all tests with coverage
pytest tests/ --cov

# Backend tests only
pytest tests/backend_tests/

# Database tests
pytest tests/database_tests/

# WebSocket tests
pytest tests/backend_tests/test_ws_*.py
```

### Code Quality Standards
- **Type Hints**: Full type annotations for better IDE support
- **Testing**: Unit tests with 80%+ coverage requirement
- **Code Quality**: Black formatting, Ruff linting
- **Security**: Automated secret detection and secure coding practices

### Project Layout
```text
src/
  main.py                   # FastAPI entrypoint (serves /api, docs at /docs)
  App.py                    # Application singleton (DB, Redis, Celery, Models)
  backend/routers/          # REST and WS route modules (mounted at /api)
    agent/                  # Agent profiles, consent, ingest, memory (/api/agent)
    agents.py               # Agent task lifecycle + inference relay (/api/agent)
    acp/                    # ACP grant handoff (/api/acp)
  celery_app/               # Celery setup and task modules
  agents/                   # Backend-side agent logic: provider routing, registry,
                             #  normalization, event/telemetry ingestion (imported
                             #  by backend/routers/agent* — not a Celery task)
  code4me2_agent/           # Standalone ACP agent CLI (separate package, see
                             #  pyproject.toml — not part of the backend image)
  database/                 # SQLAlchemy models, CRUD, migrations, pgvector
  website/                  # React frontend (served via nginx at :8000)
    src/pages/AgentProfiles.js, AgentAssignments.js   # Admin-only agent management UI
```

## 📊 Monitoring & Health Checks

### Health Endpoints
```bash
# Application health (HEAD)
curl -I http://localhost:8008/api/ping

# Container status
docker-compose ps

# Service logs
docker-compose logs -f backend
docker-compose logs -f celery-worker
```

### Performance Monitoring
- **System Metrics**: CPU, memory, disk usage
- **Performance Analytics**: Response times, throughput
- **User Activity**: Session tracking, completion patterns
- **Resource Utilization**: GPU usage, model performance

## 🚨 Troubleshooting

### Common Issues

**Port Conflicts**
- Ensure ports 8000, 8008, 3000, 5432, 6379, 6380 are available
- Check for existing services using these ports

**GPU Issues**
- Verify NVIDIA Docker is installed: `nvidia-smi`
- Check GPU availability in containers
- Ensure proper CUDA environment variables

**Database Connection**
- Wait for database initialization: `docker-compose logs db`
- Verify connection string in `.env`
- Check PostgreSQL logs for errors

**Memory Issues**
- Increase Docker memory limit (16GB+ recommended)
- Reduce model concurrency in Celery workers
- Monitor Redis memory usage

**WebSocket Connection Issues**
- Check authentication tokens in cookies
- Verify session and project token validity
- Monitor Celery broker connectivity

**Common 401/403 Causes**
- Missing `session_token`/`project_token` cookies when calling protected endpoints.
- Using POST for `/api/session/acquire` (it is GET).
- Agent endpoints under `/api/agent/events`, `/api/agent/memory` require an ACP bearer token (from `/api/acp/session/exchange`), not a session cookie.
- `/api/agent/profiles` and `/api/agent/assignments` require an admin user.

**Agent Subsystem Issues**
- **401 from OpenRouter**: the seeded default profile (`default-code4me2-agent`) uses OpenRouter with `api_key_ref=OPENROUTER_API_KEY`. Set `OPENROUTER_API_KEY` in `.env` and restart `backend` — until it's set, calls are forwarded unauthenticated and OpenRouter rejects them.
- **Agent can't reach Ollama from inside Docker** (only relevant if you switch a profile back to a local Ollama `base_url`): `http://localhost:11434/v1` resolves to the *container*, not your host. Either run the backend outside Docker for agent development, or set that profile's `base_url` (or the `AGENT_UPSTREAM_BASE_URL` fallback) to `http://host.docker.internal:11434/v1` (the `backend` service already maps `host.docker.internal` via `extra_hosts` in `docker-compose.yml`).
- **Agent tables missing**: run `python src/database/migration/migration_manager.py migrate` — the agent tables are not created by `init.sql`, only by Alembic migrations. **Default profiles absent**: run the development seeder (`scripts/dev/seed_local_dev.sh`); the default agent profiles are not created by a migration.
- **Agent CLI won't authenticate**: check that the launching process (normally the JetBrains plugin) set `CODE4ME_ACP_BACKEND_URL`, `CODE4ME_ACP_GRANT`, and `CODE4ME_ACP_TOKEN`, or that the workspace handoff file exists — the grant is single-use and short-lived.

### Debugging Commands
```bash
# View all logs
docker-compose logs

# Specific service logs
docker-compose logs backend
docker-compose logs celery-worker

# Follow logs in real-time
docker-compose logs -f

# Check Redis connectivity
docker-compose exec redis redis-cli ping

# Check database connectivity
docker-compose exec db psql -U postgres -d code4meV2 -c "SELECT 1;"
```

## 🔧 Configuration

### Key Environment Variables
```bash
# Server Configuration
SERVER_HOST=0.0.0.0
SERVER_PORT=8008
TEST_MODE=false

# Database Configuration
DB_HOST=db
DB_PORT=5432
DB_NAME=code4meV2
DB_USER=postgres
DB_PASSWORD=your_secure_password

# Redis Configuration
REDIS_HOST=redis
REDIS_PORT=6379
CELERY_BROKER_HOST=redis-celery
CELERY_BROKER_PORT=6379

# AI Model Configuration
HF_TOKEN=your_huggingface_token
MODEL_CACHE_DIR=./data/hf
MODEL_MAX_NEW_TOKENS=64
MODEL_USE_CACHE=true

# Agent Subsystem (ACP) — see .env.example for the full documented list
AGENT_UPSTREAM_BASE_URL=http://localhost:11434/v1
AGENT_UPSTREAM_API_KEY=
OLLAMA_API_KEY=
OPENAI_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
AGENT_REASONING_EFFORT=

# Security Configuration
AUTHENTICATION_TOKEN_EXPIRES_IN_SECONDS=3600
SESSION_TOKEN_EXPIRES_IN_SECONDS=3600
DEFAULT_MAX_REQUEST_RATE_PER_HOUR=1000

# Email Configuration
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USERNAME=your_email@gmail.com
EMAIL_PASSWORD=your_app_password
EMAIL_FROM=noreply@code4me.com
```

## 🧪 Research Platform

The research platform (Issue 01+) is mounted under `/api/research` and is
documented in [`docs/research-platform/`](docs/research-platform/). It is
admin/operator-facing: compatibility, protocol publication, registry, bootstrap,
and exports record evidence or define experimental intent and are never exposed
to participants.

```http
# ACP host capability receipts + compatibility gate (admin)
GET/POST /api/research/compatibility/...

# Immutable StudyProtocolV1 authoring + publication (admin)
GET/POST /api/research/studies/...

# Agent registry, artifacts, and capability snapshots (admin)
GET/POST /api/research/agents/...

# Participant identity, consent, and withdrawal
GET/POST /api/research/participants/...

# Bootstrap manifest retrieval/validation
GET/POST /api/research/bootstrap/...

# Research session lifecycle (session capability auth)
GET/POST /api/research/sessions/...

# Canonical telemetry batch ingestion
GET/POST /api/research/telemetry/...

# Researcher read models + pilot operations/release gate (RBAC/admin)
GET/POST /api/research/operations/...
```

### Seed a fresh database from the built runtime

A fresh database is made immediately usable by importing the runtime **producer
manifest together with its exact archive bytes** (never a hand-typed digest),
checking the producer tests for its built platforms, pinning
`default-code4me2-agent` to it, marking the participant-installed profiles
(`default-goose`/`default-codex`) as BYOA, and publishing one study revision with
a working `session_policy`.

```bash
cd code4me2-server
scripts/dev/seed_local_dev.sh
```

`seed_local_dev.sh` is a thin driver: it stages the built archives into the
running backend container and imports the producer manifest unchanged. Generate
it first with the [native release command](docs/research-platform/RELEASES.md). Every declared archive's real `sha256`/size are recomputed and checked,
so no digest or size is ever typed by hand. Override the source manifest with
`MANIFEST=/path/to/manifest.json`.

The same verified import is the only way a release enters the catalogue, and is
exposed to CI as an admin **multipart** endpoint:

```bash
curl -X POST http://localhost:8008/api/research/agents/releases/import \
    -H "Cookie: auth_token=$TOKEN" \
    -F 'manifest=<producer-manifest.json' \
    -F 'archives=@code4me-agent-macos-arm64.zip'
```

Exactly the manifest's declared basenames must be uploaded and match; a missing,
duplicate, unexpected or mismatched upload rejects the whole import with a typed
error and creates no release row. Streaming limits are
`RESEARCH_IMPORT_MAX_ARCHIVE_BYTES` (default 256 MiB) and
`RESEARCH_IMPORT_MAX_TOTAL_BYTES` (default 1 GiB). It is idempotent: re-importing
the same manifest returns the existing release (`created: false`).

### Seed a study (participant enrollment)

The participant runbook needs a published study and an ACTIVE enrollment before
the plugin can join. The same seeder mints that onboarding state by calling the
real research services (protocol publication, agent registry, identity
enrollment/consent) — it does not re-implement any domain logic. It is a
development-only tool and refuses to run unless `CODE4ME_DEV_SEED=1` (or
`TEST_MODE=true`) is set.

```bash
cd code4me2-server
scripts/dev/seed_local_dev.sh --builtin-study-name "My Study"
```

Common options: `--builtin-study-name`, `--connection-label`,
`--connection-base-url`, `--connection-secret-ref`, `--connection-model`.

The command prints a copy-pasteable summary. The `enrollment_id` is the opaque
"enrollment code" the participant pastes into IntelliJ's **Tools → Join Research
Study...** action:

```text
  enrollment_id (join code): <uuid>
  enrollment_status:         ACTIVE
  study_id:                  <uuid>
  revision_id:               <uuid>
  agent_id:                  code4me-synthetic-agent
  release_id:                code4me-synthetic-agent-synthetic-1
  artifact_digest:           sha256:<64 hex>
  login email:               participant@example.com
  login password:            Password123
```

The script is deterministic and safe to re-run: the same arguments resolve the
same account, release, study, published revision and enrollment instead of
piling up duplicates. A password is only printed when this run created the
account (`--create-account`); an existing account is left untouched. The release
is registered `QUALIFIED` and immutable — changing `--artifact-digest` without a
new `--release-id` is rejected rather than mutating a published release.

## 📖 Documentation

- **API Documentation**: http://localhost:8008/docs (when running)
- **Database Schema**: See `src/database/resources/documentation/`
- **Authentication Workflows**: See `src/backend/resources/documentation/`
- **Research Platform**: See [`docs/research-platform/`](docs/research-platform/)

## 🔒 Production Notes
- Restrict CORS origins and cookies in production.
- Configure strong `DB_PASSWORD`, rotate secrets, and set per-environment `.env`.
- Place `nginx` behind TLS termination and enable HSTS.
- Consider scaling `celery-worker` replicas per GPU availability; update device IDs accordingly.

## 🤝 Contributing

### Development Workflow
1. **Fork and Clone**: Create your development environment
2. **Install Dependencies**: Follow local development setup
3. **Run Tests**: Ensure all tests pass before making changes
4. **Code Standards**: Follow type hints, testing, and security guidelines
5. **Submit PR**: Include comprehensive description and test results

### Architecture Patterns
- **FastAPI**: Async/await patterns for I/O operations
- **SQLAlchemy ORM**: Proper relationship definitions
- **Pydantic Models**: Request/response validation with detailed field descriptions
- **Celery**: Background task processing with proper error handling
- **WebSocket**: Real-time communication with connection management

## 🙏 Acknowledgments
- **JetBrains** for IDE integration platform and development tools
- **Hugging Face** for AI model infrastructure and transformer ecosystem
- **FastAPI Community** for excellent async web framework
