# Code4me V2 - AI-Powered Code Completion Service

## 🚀 Overview

Code4me V2 is a comprehensive AI-powered code completion service that provides intelligent code suggestions using state-of-the-art language models. The platform offers real-time code completions, multi-file context awareness, and detailed analytics for developers and organizations.

### Key Features

- **🤖 AI-Powered Completions**: Leverages advanced models like DeepSeek Coder and StarCoder2
- **🔐 Secure Authentication**: Email/password and Google OAuth support
- **📊 Analytics Dashboard**: Comprehensive metrics and visualization
- **🗂️ Multi-File Context**: Context-aware completions across multiple files
- **⚡ Real-Time Performance**: Fast, low-latency code suggestions
- **📈 Telemetry & Feedback**: Detailed usage analytics and model improvement feedback
- **🌐 Web Interface**: Modern React-based dashboard
- **🐳 Docker Support**: Easy deployment with Docker Compose

## 🏗️ Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   React Web    │    │  FastAPI        │    │  PostgreSQL     │
│   Frontend      │◄──►│  Backend        │◄──►│  Database       │
│   (Port 3000)   │    │  (Port 8008)    │    │  (Port 5432)    │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         │              ┌─────────────────┐    ┌─────────────────┐
         │              │     Redis       │    │   AI Models     │
         └──────────────►│ Session Store   │    │ (DeepSeek/      │
                        │ (Port 6379)     │    │  StarCoder)     │
                        └─────────────────┘    └─────────────────┘
```

### Technology Stack

**Backend:**
- FastAPI (Python 3.11+)
- SQLAlchemy ORM
- PostgreSQL Database
- Redis Session Management
- Transformers & PyTorch for AI models
- Argon2 for password hashing

**Frontend:**
- React 18
- CSS Variables for theming
- Google OAuth integration
- Modern responsive design

**Infrastructure:**
- Docker & Docker Compose
- Nginx reverse proxy
- GitLab CI/CD
- Pytest for testing

## 📋 Prerequisites

- **Docker & Docker Compose** (recommended)
- **Python 3.11+** (for local development)
- **Node.js 18+** (for frontend development)
- **PostgreSQL 16+** (if running without Docker)
- **Redis 7.0+** (if running without Docker)

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd code4me-v2
   ```

2. **Set up environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

3. **Start all services**
   ```bash
   docker-compose up -d
   ```

4. **Access the application**
   - Web Interface: http://localhost:3000
   - API Documentation: http://localhost:8008/docs
   - PgAdmin: http://localhost:5050

### Option 2: Local Development

1. **Database Setup**
   ```bash
   # Start PostgreSQL and Redis
   docker-compose up -d db redis
   ```

2. **Backend Setup**
   ```bash
   # Install Python dependencies
   pip install -r requirements.txt
   
   # Set environment variables
   export PYTHONPATH=$PWD/src
   
   # Run the backend
   python src/main.py
   ```

3. **Frontend Setup**
   ```bash
   cd src/website
   npm install
   npm start
   ```

## ⚙️ Configuration

### Environment Variables

Create a `.env` file in the project root:

```bash
# Database Configuration
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your_password
DB_NAME=code4me_v2

# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379

# Backend Configuration
REACT_APP_BACKEND_HOST=http://localhost
REACT_APP_BACKEND_PORT=8008

# Frontend Configuration
WEBSITE_HOST=localhost
WEBSITE_PORT=3000

# Authentication
REACT_APP_GOOGLE_CLIENT_ID=your_google_client_id

# PgAdmin
PGADMIN_PORT=5050
PGADMIN_DEFAULT_EMAIL=admin@example.com
PGADMIN_DEFAULT_PASSWORD=admin

# Application Settings
SERVER_VERSION_ID=1
SESSION_LENGTH=3600
MAX_FAILED_SESSION_ATTEMPTS=5
MAX_REQUEST_RATE=1000
PRELOAD_MODELS=true
DEBUG_MODE=false
TEST_MODE=false

# Survey (Optional)
SURVEY_LINK=https://your-survey-link.com
```

### Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable Google+ API
4. Create OAuth 2.0 credentials
5. Add your domain to authorized origins
6. Copy the Client ID to your `.env` file

## 🗄️ Database

### Schema Overview

The database contains several key tables:

- **user**: User accounts and authentication
- **query**: Completion requests from users
- **context**: Code context for completions
- **telemetry**: Usage metrics and performance data
- **had_generation**: AI model completions
- **ground_truth**: User feedback and actual code
- **model_name**: Available AI models
- **programming_language**: Supported languages

### Database Initialization

The database is automatically initialized with:
- Default AI models (DeepSeek Coder, StarCoder2)
- Programming language mappings
- Trigger types and plugin versions

See `src/database/init.sql` for complete initialization script.

## 🔌 API Documentation

### Authentication Endpoints

```
POST /api/user/create/          # Create new user
POST /api/user/authenticate/    # User login
PUT  /api/user/update/          # Update user profile
DELETE /api/user/delete/        # Delete user account
```

### Completion Endpoints

```
POST /api/completion/request/            # Request code completion
GET  /api/completion/{query_id}          # Get completions by query ID
POST /api/completion/feedback/           # Submit completion feedback
POST /api/completion/multi-file-context/update/  # Update multi-file context
```

### Interactive API Documentation

Visit http://localhost:8008/docs for complete Swagger/OpenAPI documentation with interactive testing.

## 🧪 Testing

### Backend Tests

```bash
# Run all tests
pytest

# Run specific test modules
pytest tests/backend_tests/

# Run with coverage
pytest --cov=./src/backend --cov-report=html
```

### Frontend Tests

```bash
cd src/website
npm test
```

### Test Database

Tests use a separate test database. Ensure your test environment is configured:

```bash
# Start test database
docker-compose up -d test_db

# Run tests with test database
TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:5433/test_db" pytest
```

## 🚀 Deployment

### Production Deployment

1. **Prepare environment**
   ```bash
   # Set production environment variables
   export DEBUG_MODE=false
   export TEST_MODE=false
   ```

2. **Deploy with Docker Compose**
   ```bash
   docker-compose -f docker-compose.prod.yml up -d
   ```

3. **Set up reverse proxy** (Nginx configuration included)

4. **Configure SSL/TLS** for production security

### CI/CD Pipeline

The project includes GitLab CI/CD configuration (`.gitlab-ci.yml`) with:

- **Linting**: Prettier, Black, Ruff
- **Testing**: Pytest with coverage
- **Security**: Dependency scanning
- **Deployment**: Automated deployment stages

## 🛠️ Development

### Code Style

The project uses several tools for code quality:

- **Python**: Black (formatting), Ruff (linting)
- **JavaScript/React**: Prettier (formatting)
- **Pre-commit hooks**: Automatic formatting on commit

```bash
# Install pre-commit hooks
pip install pre-commit
pre-commit install

# Run formatting manually
npx prettier --write .
black src/ tests/
ruff check src/ tests/
```

### Adding New AI Models

1. **Register in database**
   ```sql
   INSERT INTO model_name (model_name) VALUES ('your-model-name');
   ```

2. **Update model loading**
   ```python
   # In App.py setup method
   self.__completion_models.load_model('your-model-name')
   ```

3. **Test the integration**
   ```bash
   pytest tests/backend_tests/test_completion_request.py
   ```

## 📊 Monitoring & Analytics

### Built-in Metrics

- **Completion Performance**: Response times, model accuracy
- **User Activity**: Request patterns, feature usage
- **System Health**: Database performance, error rates
- **Model Analytics**: Completion acceptance rates, feedback

### Dashboard Features

- Real-time performance monitoring
- User activity visualization
- Completion success metrics
- System resource utilization

## 🔧 Troubleshooting

### Common Issues

**Database Connection Issues**
```bash
# Check database status
docker-compose ps db

# View database logs
docker-compose logs db

# Reset database
docker-compose down -v
docker-compose up -d db
```

**Model Loading Problems**
```bash
# Check available disk space (models require ~12GB for StarCoder)
df -h

# View backend logs
docker-compose logs backend

# Disable model preloading for development
export PRELOAD_MODELS=false
```

**Frontend Build Issues**
```bash
# Clear npm cache
npm cache clean --force

# Reinstall dependencies
rm -rf node_modules package-lock.json
npm install
```

## 📝 Contributing

1. **Fork the repository**
2. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. **Make your changes** following code style guidelines
4. **Add tests** for new functionality
5. **Submit a pull request**

### Development Guidelines

- Follow PEP 8 for Python code
- Use TypeScript for new React components
- Write comprehensive tests
- Update documentation for API changes
- Use conventional commit messages

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## 🆘 Support

- **Documentation**: Check this README and inline code comments
- **API Reference**: http://localhost:8008/docs
- **Issues**: Use the GitHub issue tracker
- **Discussions**: Use GitHub Discussions for questions

## 🗺️ Roadmap

- [ ] **Plugin System**: IDE plugins for VS Code, JetBrains
- [ ] **Advanced Models**: Integration with GPT-4, Claude
- [ ] **Team Features**: Organization management, team analytics
- [ ] **Performance**: Model optimization, caching improvements
- [ ] **Security**: Enhanced authentication, audit logging

---

## 📚 Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [React Documentation](https://reactjs.org/docs/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Docker Compose Reference](https://docs.docker.com/compose/)
- [Transformers Library](https://huggingface.co/docs/transformers/)

---

**Built with ❤️ for developers, by developers**