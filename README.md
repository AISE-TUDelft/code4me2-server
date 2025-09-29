# Code4me V2 - AI-Powered Code Completion Platform

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![React](https://img.shields.io/badge/react-19+-blue.svg)](https://reactjs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115.0+-green.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://docker.com)
[![License](https://img.shields.io/badge/License-Apache_2.0-brightgreen.svg)](https://www.apache.org/licenses/LICENSE-2.0)

An advanced AI-powered code completion platform, featuring ghost text suggestions, multi-model inference, a context-aware chat assistant, real-time WebSocket communication, and collaborative development tools suitable for conducting empirical studies on developer behaviour.

## 🎯 Project Overview

Code4me V2 is a research platform that combines transformer models with real-world developer workflows through a JetBrains Plugin integration. It provides:

- **Real-time Code Completion**: Multi-model AI inference with WebSocket streaming
- **Collaborative Development**: Multi-user project management and session tracking
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
```

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

### Key Features
- **PostgreSQL + pgvector**: Vector similarity search for code context
- **Polymorphic Inheritance**: Flexible query system for different completion types
- **Migration System**: Hybrid SQL initialization + Alembic migrations
- **Comprehensive Indexing**: Optimized for high-volume analytics queries

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
  celery_app/               # Celery setup and task modules
  database/                 # SQLAlchemy models, CRUD, migrations, pgvector
  website/                  # React frontend (served via nginx at :8000)
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

## 📖 Documentation

- **API Documentation**: http://localhost:8008/docs (when running)
- **Database Schema**: See `src/database/resources/documentation/`
- **Authentication Workflows**: See `src/backend/resources/documentation/`

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
