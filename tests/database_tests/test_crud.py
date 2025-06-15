import json
import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from dotenv import load_dotenv
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import database.crud as crud
import Queries
from database import db_schemas
from database.db import Base

load_dotenv()

# Get test database URL from environment or use default for Docker
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


@pytest.fixture(scope="function")
def db_session():
    """
    Creates a fresh database session for each test function.
    """
    # Create test database engine
    engine = create_engine(TEST_DB_URL)

    # Create all tables
    Base.metadata.create_all(engine)

    # Create session
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()

    try:
        yield db
    finally:
        db.close()
        # Drop all tables after test
        Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def setup_reference_data(db_session):
    """
    Set up reference data needed for tests (config, models, languages, etc.)
    """
    # Add config if it doesn't exist
    if db_session.query(db_schemas.Config).count() == 0:
        config = db_schemas.Config(config_data='{"test": true, "version": "1.0"}')
        db_session.add(config)

    # Add models if they don't exist
    if db_session.query(db_schemas.ModelName).count() == 0:
        models = [
            db_schemas.ModelName(
                model_name="deepseek-1.3b", is_instruction_tuned=False
            ),
            db_schemas.ModelName(
                model_name="starcoder2-3b", is_instruction_tuned=False
            ),
            db_schemas.ModelName(model_name="gpt-4-turbo", is_instruction_tuned=True),
        ]
        db_session.add_all(models)

    # Add programming languages if they don't exist
    if db_session.query(db_schemas.ProgrammingLanguage).count() == 0:
        languages = [
            db_schemas.ProgrammingLanguage(language_name="python"),
            db_schemas.ProgrammingLanguage(language_name="javascript"),
            db_schemas.ProgrammingLanguage(language_name="typescript"),
        ]
        db_session.add_all(languages)

    # Add trigger types if they don't exist
    if db_session.query(db_schemas.TriggerType).count() == 0:
        trigger_types = [
            db_schemas.TriggerType(trigger_type_name="manual"),
            db_schemas.TriggerType(trigger_type_name="auto"),
            db_schemas.TriggerType(trigger_type_name="idle"),
        ]
        db_session.add_all(trigger_types)

    # Add plugin versions if they don't exist
    if db_session.query(db_schemas.PluginVersion).count() == 0:
        versions = [
            db_schemas.PluginVersion(
                version_name="0.1.0v",
                ide_type="VSCode",
                description="Test version with chat support",
            ),
            db_schemas.PluginVersion(
                version_name="0.1.0j",
                ide_type="JetBrains",
                description="Test version with chat support",
            ),
        ]
        db_session.add_all(versions)

    db_session.commit()


@pytest.fixture(scope="function")
def test_config(db_session, setup_reference_data):
    """Get the test config"""
    return db_session.query(db_schemas.Config).first()


@pytest.fixture(scope="function")
def test_user(db_session, test_config):
    """Create a test user for tests"""
    user_data = Queries.CreateUser(
        email="test_user@example.com",
        name="Test User",
        password="SecurePassword123",
        config_id=test_config.config_id,
    )
    created_user = crud.create_user(db_session, user_data)
    return created_user


@pytest.fixture(scope="function")
def test_project(db_session, test_user):
    """Create a test project"""
    project_data = Queries.CreateProject(
        project_name="Test Project",
    )
    created_project = crud.create_project(db_session, project_data)

    # Add user to project
    project_user_data = Queries.CreateUserProject(
        project_id=created_project.project_id,
        user_id=test_user.user_id,
    )
    crud.create_user_project(db_session, project_user_data)

    return created_project


@pytest.fixture(scope="function")
def test_session(db_session, test_user, test_project):
    """Create a test session with project association"""
    session_data = Queries.CreateSession(user_id=test_user.user_id)
    created_session = crud.create_session(db_session, session_data)

    # Create session-project association
    session_project_data = Queries.CreateSessionProject(
        session_id=created_session.session_id, project_id=test_project.project_id
    )
    crud.create_session_project(db_session, session_project_data)

    return created_session


@pytest.fixture(scope="function")
def test_chat(db_session, test_user, test_project):
    """Create a test chat"""
    chat_data = Queries.CreateChat(
        project_id=test_project.project_id, user_id=test_user.user_id, title="Test Chat"
    )
    # Pass the chat_id parameter that the function expects
    chat_id = str(uuid.uuid4())
    return crud.create_chat(db_session, chat_data, chat_id)


# ============================================================================
# CONFIG TESTS
# ============================================================================


def test_create_and_get_config(db_session):
    """Test creating and retrieving config"""
    config_data = Queries.CreateConfig(
        config_data='{"test_mode": true, "max_completions": 5}'
    )

    created_config = crud.create_config(db_session, config_data)

    assert created_config is not None
    assert created_config.config_data == '{"test_mode": true, "max_completions": 5}'
    assert created_config.config_id is not None

    # Test retrieving by ID
    retrieved_config = crud.get_config_by_id(db_session, created_config.config_id)
    assert retrieved_config is not None
    assert retrieved_config.config_id == created_config.config_id


def test_get_all_configs(db_session, setup_reference_data):
    """Test getting all configs"""
    configs = crud.get_all_configs(db_session)
    assert len(configs) >= 1  # At least the setup config


# ============================================================================
# USER TESTS
# ============================================================================


def test_get_user_by_email_nonexistent(db_session):
    """Test retrieving a non-existent user by email"""
    email = "nonexistent@example.com"
    user = crud.get_user_by_email(db_session, email)
    assert user is None


def test_create_and_get_user_by_email(db_session, test_config):
    """Test creating a user and then retrieving it by email"""
    user_email = "test@example.com"
    user_data = Queries.CreateUser(
        email=user_email,
        name="Test User",
        password="SecurePassword123",
        config_id=test_config.config_id,
    )

    created_user = crud.create_user(db_session, user_data)

    assert created_user.email == user_email
    assert created_user.name == "Test User"
    assert created_user.config_id == test_config.config_id
    assert created_user.is_oauth_signup is False
    assert created_user.verified is False

    retrieved_user = crud.get_user_by_email(db_session, user_email)
    assert retrieved_user is not None
    assert retrieved_user.email == user_email
    assert retrieved_user.user_id == created_user.user_id


def test_create_user_with_oauth(db_session, test_config):
    """Test creating a user with OAuth authentication"""
    user_email = "oauth_user@example.com"
    user_data = Queries.CreateUserOauth(
        email=user_email,
        name="OAuth User",
        password="SecurePassword456",
        config_id=test_config.config_id,
        token="mock_oauth_token",
        provider=Queries.Provider.google,
    )

    created_user = crud.create_user(db_session, user_data)

    assert created_user.email == user_email
    assert created_user.name == "OAuth User"
    assert created_user.is_oauth_signup is True
    assert created_user.verified is False


def test_update_user(db_session, test_user):
    """Test updating user information"""
    update_data = Queries.UpdateUser(
        name="Updated Name", preference={"theme": "auto", "notifications": True}
    )

    updated_user = crud.update_user(db_session, test_user.user_id, update_data)

    assert updated_user is not None
    assert updated_user.name == "Updated Name"
    # Check that preference was updated (it gets merged with defaults)
    preference_dict = json.loads(updated_user.preference)
    assert preference_dict["theme"] == "auto"
    assert preference_dict["notifications"] is True


def test_get_user_by_id(db_session, test_user):
    """Test retrieving user by ID"""
    retrieved_user = crud.get_user_by_id(db_session, test_user.user_id)
    assert retrieved_user is not None
    assert retrieved_user.user_id == test_user.user_id
    assert retrieved_user.email == test_user.email


def test_get_user_by_email_password(db_session, test_config):
    """Test user authentication with email and password"""
    email = "auth_test@example.com"
    password = "TestPassword123"

    user_data = Queries.CreateUser(
        email=email,
        name="Auth Test User",
        password=password,
        config_id=test_config.config_id,
    )
    created_user = crud.create_user(db_session, user_data)

    # Test correct password
    auth_user = crud.get_user_by_email_password(db_session, email, password)
    assert auth_user is not None
    assert auth_user.user_id == created_user.user_id

    # Test wrong password
    wrong_auth = crud.get_user_by_email_password(db_session, email, "WrongPassword")
    assert wrong_auth is None


def test_delete_user_by_id(db_session, test_config):
    """Test deleting a user by ID"""
    user_data = Queries.CreateUser(
        email="delete_test@example.com",
        name="Delete Test User",
        password="DeletePassword123",
        config_id=test_config.config_id,
    )
    created_user = crud.create_user(db_session, user_data)
    user_id = created_user.user_id

    # Delete the user
    result = crud.delete_user_by_id(db_session, user_id)
    assert result is True

    # Verify user is deleted
    deleted_user = crud.get_user_by_id(db_session, user_id)
    assert deleted_user is None


# ============================================================================
# PROJECT TESTS
# ============================================================================


def test_create_and_get_project(db_session):
    """Test creating and retrieving a project"""
    project_data = Queries.CreateProject(
        project_name="My Test Project",
    )

    created_project = crud.create_project(db_session, project_data)

    assert created_project is not None
    assert created_project.project_name == "My Test Project"

    retrieved_project = crud.get_project_by_id(db_session, created_project.project_id)
    assert retrieved_project is not None
    assert retrieved_project.project_id == created_project.project_id


def test_update_project(db_session, test_project):
    """Test updating a project"""
    update_data = Queries.UpdateProject(
        project_name="Updated Project Name",
    )

    result = crud.update_project(db_session, test_project.project_id, update_data)
    assert result > 0

    # Verify update
    updated_project = crud.get_project_by_id(db_session, test_project.project_id)
    assert updated_project.project_name == "Updated Project Name"


def test_add_user_to_project(db_session, test_user, test_project):
    """Test adding a user to a project"""
    # User should already be added via fixture, test retrieval
    project_users = crud.get_project_users(db_session, test_project.project_id)

    assert len(project_users) == 1
    assert project_users[0].user_id == test_user.user_id


def test_get_projects_for_user(db_session, test_user, test_project):
    """Test getting all projects for a user"""
    projects = crud.get_projects_for_user(db_session, test_user.user_id)

    assert len(projects) == 1
    assert projects[0].project_id == test_project.project_id


def test_remove_user_from_project(db_session, test_user, test_project):
    """Test removing a user from a project"""
    # First verify user is in project
    user_project = crud.get_user_project(
        db_session, test_user.user_id, test_project.project_id
    )
    assert user_project is not None

    # Remove user from project
    result = crud.remove_user_from_project(
        db_session, test_project.project_id, test_user.user_id
    )
    assert result is True

    # Verify user is removed
    user_project = crud.get_user_project(
        db_session, test_user.user_id, test_project.project_id
    )
    assert user_project is None


# ============================================================================
# SESSION TESTS
# ============================================================================


def test_create_and_get_session(db_session, test_user, test_project):
    """Test creating and retrieving a session"""
    session_data = Queries.CreateSession(user_id=test_user.user_id)

    created_session = crud.create_session(db_session, session_data)

    assert created_session is not None
    assert created_session.user_id == test_user.user_id
    assert created_session.start_time is not None
    assert created_session.end_time is None

    retrieved_session = crud.get_session_by_id(db_session, created_session.session_id)
    assert retrieved_session is not None


def test_update_session_end_time(db_session, test_session, test_user):
    """Test updating session end time"""
    end_time = datetime.now().isoformat()
    update_data = Queries.UpdateSession(end_time=end_time)

    result = crud.update_session(db_session, test_session.session_id, update_data)
    assert result > 0

    updated_session = crud.get_session_by_id(db_session, test_session.session_id)
    assert updated_session.end_time is not None


def test_get_sessions_for_user(db_session, test_user, test_session):
    """Test getting all sessions for a user"""
    sessions = crud.get_sessions_for_user(db_session, test_user.user_id)
    assert len(sessions) >= 1
    assert any(s.session_id == test_session.session_id for s in sessions)


def test_session_project_association(db_session, test_session, test_project):
    """Test session-project association"""
    session_project = crud.get_session_project(
        db_session, test_session.session_id, test_project.project_id
    )
    assert session_project is not None
    assert session_project.session_id == test_session.session_id
    assert session_project.project_id == test_project.project_id


# ============================================================================
# CHAT TESTS
# ============================================================================


def test_create_and_get_chat(db_session, test_user, test_project):
    """Test creating and retrieving a chat"""
    chat_data = Queries.CreateChat(
        project_id=test_project.project_id, user_id=test_user.user_id, title="Test Chat"
    )

    chat_id = str(uuid.uuid4())
    created_chat = crud.create_chat(db_session, chat_data, chat_id)

    assert created_chat is not None
    assert created_chat.title == "Test Chat"
    assert created_chat.project_id == test_project.project_id
    assert created_chat.user_id == test_user.user_id

    retrieved_chat = crud.get_chat_by_id(db_session, created_chat.chat_id)
    assert retrieved_chat is not None


def test_get_chats_for_project(db_session, test_user, test_project):
    """Test getting all chats for a project"""
    # Create multiple chats
    chat1_data = Queries.CreateChat(
        project_id=test_project.project_id,
        user_id=test_user.user_id,
        title="Chat 1",
    )
    chat1 = crud.create_chat(db_session, chat1_data, str(uuid.uuid4()))

    chat2_data = Queries.CreateChat(
        project_id=test_project.project_id,
        user_id=test_user.user_id,
        title="Chat 2",
    )
    chat2 = crud.create_chat(db_session, chat2_data, str(uuid.uuid4()))

    chats = crud.get_chats_for_project(db_session, test_project.project_id)
    assert len(chats) == 2


def test_get_chats_for_user(db_session, test_user, test_chat):
    """Test getting all chats for a user"""
    chats = crud.get_chats_for_user(db_session, test_user.user_id)
    assert len(chats) >= 1
    assert any(c.chat_id == test_chat.chat_id for c in chats)


def test_update_chat(db_session, test_chat):
    """Test updating a chat"""
    update_data = Queries.UpdateChat(title="Updated Chat Title")

    result = crud.update_chat(db_session, test_chat.chat_id, update_data)
    assert result is not None


# ============================================================================
# CONTEXT AND TELEMETRY TESTS
# ============================================================================


def test_create_context(db_session):
    """Test creating a context record"""
    context_data = Queries.ContextData(
        prefix="def hello_world():",
        suffix="    pass",
        file_name="test.py",
        selected_text="hello_world",
    )

    created_context = crud.create_context(db_session, context_data)

    assert created_context is not None
    assert created_context.prefix == "def hello_world():"
    assert created_context.suffix == "    pass"
    assert created_context.file_name == "test.py"
    assert created_context.selected_text == "hello_world"


def test_get_context_by_id(db_session):
    """Test retrieving context by ID"""
    context_data = Queries.ContextData(
        prefix="test prefix",
        suffix="test suffix",
        file_name="test.py",
    )
    created_context = crud.create_context(db_session, context_data)

    retrieved_context = crud.get_context_by_id(db_session, created_context.context_id)
    assert retrieved_context is not None
    assert retrieved_context.context_id == created_context.context_id


def test_create_contextual_telemetry(db_session, setup_reference_data):
    """Test creating contextual telemetry record"""
    telemetry_data = Queries.ContextualTelemetryData(
        version_id=1,
        trigger_type_id=1,
        language_id=1,
        file_path="/path/to/test.py",
        caret_line=42,
        document_char_length=1500,
        relative_document_position=0.7,
    )

    created_telemetry = crud.create_contextual_telemetry(db_session, telemetry_data)

    assert created_telemetry is not None
    assert created_telemetry.version_id == 1
    assert created_telemetry.trigger_type_id == 1
    assert created_telemetry.language_id == 1
    assert created_telemetry.file_path == "/path/to/test.py"
    assert created_telemetry.caret_line == 42


def test_create_behavioral_telemetry(db_session):
    """Test creating behavioral telemetry record"""
    telemetry_data = Queries.BehavioralTelemetryData(
        time_since_last_shown=5000, time_since_last_accepted=10000, typing_speed=300
    )

    created_telemetry = crud.create_behavioral_telemetry(db_session, telemetry_data)

    assert created_telemetry is not None
    assert created_telemetry.time_since_last_shown == 5000
    assert created_telemetry.time_since_last_accepted == 10000
    assert created_telemetry.typing_speed == 300


def test_get_contextual_telemetry_by_id(db_session, setup_reference_data):
    """Test retrieving contextual telemetry by ID"""
    telemetry_data = Queries.ContextualTelemetryData(
        version_id=1,
        trigger_type_id=1,
        language_id=1,
    )
    created_telemetry = crud.create_contextual_telemetry(db_session, telemetry_data)

    retrieved_telemetry = crud.get_contextual_telemetry_by_id(
        db_session, created_telemetry.contextual_telemetry_id
    )
    assert retrieved_telemetry is not None
    assert (
        retrieved_telemetry.contextual_telemetry_id
        == created_telemetry.contextual_telemetry_id
    )


def test_get_behavioral_telemetry_by_id(db_session):
    """Test retrieving behavioral telemetry by ID"""
    telemetry_data = Queries.BehavioralTelemetryData(typing_speed=250)
    created_telemetry = crud.create_behavioral_telemetry(db_session, telemetry_data)

    retrieved_telemetry = crud.get_behavioral_telemetry_by_id(
        db_session, created_telemetry.behavioral_telemetry_id
    )
    assert retrieved_telemetry is not None
    assert (
        retrieved_telemetry.behavioral_telemetry_id
        == created_telemetry.behavioral_telemetry_id
    )


# ============================================================================
# QUERY TESTS (COMPLETION AND CHAT)
# ============================================================================


def test_create_completion_query(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test creating a completion query"""
    # Create context
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="def test_function():",
            suffix="    return True",
            file_name="test.py",
            selected_text="test_function",
        ),
    )

    # Create telemetries
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(
            version_id=1,
            trigger_type_id=1,
            language_id=1,
            file_path="/test.py",
            caret_line=5,
            document_char_length=200,
            relative_document_position=0.5,
        ),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session,
        Queries.BehavioralTelemetryData(
            time_since_last_shown=3000, time_since_last_accepted=6000, typing_speed=250
        ),
    )

    # Create completion query
    query_data = Queries.CreateCompletionQuery(
        user_id=test_user.user_id,
        contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
        behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
        context_id=context.context_id,
        session_id=test_session.session_id,
        project_id=test_project.project_id,
        multi_file_context_changes_indexes={"index": 1},
        total_serving_time=150,
        server_version_id=1,
    )

    created_query = crud.create_completion_query(db_session, query_data)

    assert created_query is not None
    assert created_query.meta_query_id is not None

    # Verify the meta_query was created correctly
    meta_query = crud.get_meta_query_by_id(db_session, created_query.meta_query_id)
    assert meta_query is not None
    assert meta_query.query_type == "completion"
    assert meta_query.user_id == test_user.user_id


def test_create_chat_query(
    db_session, test_user, test_project, test_session, test_chat, setup_reference_data
):
    """Test creating a chat query"""
    # Create context and telemetries
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="How do I implement",
            suffix="in Python?",
            file_name="chat.md",
            selected_text="authentication",
        ),
    )

    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(
            version_id=1,
            trigger_type_id=2,
            language_id=1,
            file_path="/chat.md",
            caret_line=1,
            document_char_length=50,
            relative_document_position=0.8,
        ),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session,
        Queries.BehavioralTelemetryData(
            time_since_last_shown=1000, time_since_last_accepted=5000, typing_speed=180
        ),
    )

    # Create chat query
    query_data = Queries.CreateChatQuery(
        user_id=test_user.user_id,
        contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
        behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
        context_id=context.context_id,
        session_id=test_session.session_id,
        project_id=test_project.project_id,
        chat_id=test_chat.chat_id,
        web_enabled=True,
        total_serving_time=200,
    )

    created_query = crud.create_chat_query(db_session, query_data)

    assert created_query is not None
    assert created_query.meta_query_id is not None
    assert created_query.chat_id == test_chat.chat_id
    assert created_query.web_enabled is True

    # Verify the meta_query was created correctly
    meta_query = crud.get_meta_query_by_id(db_session, created_query.meta_query_id)
    assert meta_query is not None
    assert meta_query.query_type == "chat"


def test_get_chat_queries_for_chat(
    db_session, test_user, test_project, test_session, test_chat, setup_reference_data
):
    """Test getting all chat queries for a chat"""
    # Create context and telemetries
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    # Create multiple chat queries for the same chat
    for i in range(3):
        query_data = Queries.CreateChatQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
            chat_id=test_chat.chat_id,
        )
        crud.create_chat_query(db_session, query_data)

    chat_queries = crud.get_chat_queries_for_chat(db_session, test_chat.chat_id)
    assert len(chat_queries) == 3


# ============================================================================
# GENERATION TESTS
# ============================================================================


def test_create_generation(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test creating a generation record"""
    # Create completion query first
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="def generate_test():", suffix="    pass", file_name="generate.py"
        ),
    )

    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(
            version_id=1,
            trigger_type_id=1,
            language_id=1,
            file_path="/generate.py",
            caret_line=3,
            document_char_length=100,
            relative_document_position=0.3,
        ),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session,
        Queries.BehavioralTelemetryData(
            time_since_last_shown=2000, time_since_last_accepted=4000, typing_speed=200
        ),
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
            total_serving_time=100,
        ),
    )

    # Create generation - pass meta_query_id as the id parameter
    current_time = datetime.now().isoformat()
    generation_data = Queries.CreateGeneration(
        model_id=1,
        completion="def generate_test():\n    return 'Generated successfully!'",
        generation_time=50,
        shown_at=[current_time],
        was_accepted=False,
        confidence=0.85,
        logprobs=[-0.05, -0.1, -0.15],
    )

    # Pass the meta_query_id as the id parameter to use existing meta_query
    created_generation = crud.create_generation(
        db_session, generation_data, str(completion_query.meta_query_id)
    )

    assert created_generation is not None
    assert created_generation.meta_query_id == completion_query.meta_query_id
    assert created_generation.model_id == 1
    assert "Generated successfully!" in created_generation.completion
    assert created_generation.generation_time == 50
    assert len(created_generation.shown_at) == 1
    assert created_generation.was_accepted is False
    assert created_generation.confidence == 0.85


def test_get_generations_by_meta_query(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test getting generations by meta query"""
    # Create completion query
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    # Create multiple generations - fixing the way we create them
    for model_id in [1, 2]:
        generation_data = Queries.CreateGeneration(
            model_id=model_id,
            completion=f"Generated content from model {model_id}",
            generation_time=50,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.8,
            logprobs=[-0.1, -0.2],
        )

        # Create generation with correct meta_query_id
        crud.create_generation(
            db_session, generation_data, str(completion_query.meta_query_id)
        )

    generations = crud.get_generations_by_meta_query(
        db_session, completion_query.meta_query_id
    )
    assert len(generations) == 2


def test_get_generation_by_meta_query_and_model(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test getting specific generation by meta query and model"""
    # Create completion query and generation
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    generation_data = Queries.CreateGeneration(
        model_id=1,
        completion="Test generation",
        generation_time=50,
        shown_at=[datetime.now().isoformat()],
        was_accepted=False,
        confidence=0.8,
        logprobs=[-0.1],
    )

    crud.create_generation(
        db_session, generation_data, str(completion_query.meta_query_id)
    )

    generation = crud.get_generation_by_meta_query_and_model(
        db_session, completion_query.meta_query_id, 1
    )
    assert generation is not None
    assert generation.model_id == 1


# ============================================================================
# MODEL TESTS
# ============================================================================


def test_create_model(db_session):
    """Test creating a model"""
    model_data = Queries.CreateModel(
        model_name="test-model-1b", is_instruction_tuned=True
    )

    created_model = crud.create_model(db_session, model_data)
    assert created_model is not None
    assert created_model.model_name == "test-model-1b"
    assert created_model.is_instruction_tuned is True


def test_get_model_by_id(db_session, setup_reference_data):
    """Test getting model by ID"""
    model = crud.get_model_by_id(db_session, 1)
    assert model is not None
    assert model.model_id == 1


def test_get_all_model_names(db_session, setup_reference_data):
    """Test getting all model names"""
    models = crud.get_all_model_names(db_session)
    assert len(models) >= 3  # From setup_reference_data


def test_get_all_models(db_session, setup_reference_data):
    """Test getting all models"""
    models = crud.get_all_models(db_session)
    assert len(models) >= 3  # From setup_reference_data


# ============================================================================
# GROUND TRUTH TESTS
# ============================================================================


def test_create_ground_truth(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test creating a ground truth record"""
    # Create completion query first
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="def ground_truth_test():", suffix="    pass", file_name="truth.py"
        ),
    )

    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(
            version_id=1,
            trigger_type_id=1,
            language_id=1,
            file_path="/truth.py",
            caret_line=1,
            document_char_length=60,
        ),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    # Create ground truth
    ground_truth_data = Queries.CreateGroundTruth(
        completion_query_id=completion_query.meta_query_id,
        ground_truth="def ground_truth_test():\n    print('This is the actual code')\n    return True",
    )

    created_ground_truth = crud.create_ground_truth(db_session, ground_truth_data)

    assert created_ground_truth is not None
    assert created_ground_truth.completion_query_id == completion_query.meta_query_id
    assert "actual code" in created_ground_truth.ground_truth


def test_get_ground_truths_for_completion(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test getting ground truths for a completion"""
    # Create completion query and ground truth
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    ground_truth_data = Queries.CreateGroundTruth(
        completion_query_id=completion_query.meta_query_id,
        ground_truth="Test ground truth code",
    )
    crud.create_ground_truth(db_session, ground_truth_data)

    ground_truths = crud.get_ground_truths_for_completion(
        db_session, completion_query.meta_query_id
    )
    assert len(ground_truths) == 1
    assert ground_truths[0].ground_truth == "Test ground truth code"


# ============================================================================
# REFERENCE DATA TESTS
# ============================================================================


def test_get_all_programming_languages(db_session, setup_reference_data):
    """Test getting all programming languages"""
    languages = crud.get_all_programming_languages(db_session)
    assert len(languages) >= 3  # From setup_reference_data


def test_get_all_trigger_types(db_session, setup_reference_data):
    """Test getting all trigger types"""
    trigger_types = crud.get_all_trigger_types(db_session)
    assert len(trigger_types) >= 3  # From setup_reference_data


def test_get_all_plugin_versions(db_session, setup_reference_data):
    """Test getting all plugin versions"""
    versions = crud.get_all_plugin_versions(db_session)
    assert len(versions) >= 2  # From setup_reference_data


def test_get_programming_language_by_id(db_session, setup_reference_data):
    """Test getting programming language by ID"""
    language = crud.get_programming_language_by_id(db_session, 1)
    assert language is not None
    assert language.language_id == 1


def test_get_trigger_type_by_id(db_session, setup_reference_data):
    """Test getting trigger type by ID"""
    trigger_type = crud.get_trigger_type_by_id(db_session, 1)
    assert trigger_type is not None
    assert trigger_type.trigger_type_id == 1


def test_get_plugin_version_by_id(db_session, setup_reference_data):
    """Test getting plugin version by ID"""
    version = crud.get_plugin_version_by_id(db_session, 1)
    assert version is not None
    assert version.version_id == 1


# ============================================================================
# CONSTRAINT VALIDATION TESTS
# ============================================================================


def test_foreign_key_constraints(db_session, setup_reference_data):
    """Test that foreign key constraints are enforced"""

    # Test invalid config_id in user creation
    with pytest.raises(IntegrityError):
        invalid_user = Queries.CreateUser(
            email="invalid@example.com",
            name="Invalid User",
            password="SecurePassword123",
            config_id=999999,  # Non-existent config
        )
        crud.create_user(db_session, invalid_user)

    # IMPORTANT: Rollback the session after the failed transaction
    db_session.rollback()

    # Test invalid version_id in contextual telemetry
    with pytest.raises(IntegrityError):
        invalid_telemetry = Queries.ContextualTelemetryData(
            version_id=999999,  # Non-existent version
            trigger_type_id=1,
            language_id=1,
        )
        crud.create_contextual_telemetry(db_session, invalid_telemetry)

    # Rollback again after the second failed transaction
    db_session.rollback()


def test_unique_constraints(db_session, test_config):
    """Test unique constraint violations"""

    # Create first user
    user1 = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="unique@example.com",
            name="User One",
            password="SecurePassword123",
            config_id=test_config.config_id,
        ),
    )

    # Try to create second user with same email
    with pytest.raises(IntegrityError):
        user2 = crud.create_user(
            db_session,
            Queries.CreateUser(
                email="unique@example.com",  # Same email
                name="User Two",
                password="SecurePassword456",
                config_id=test_config.config_id,
            ),
        )


# ============================================================================
# BUSINESS LOGIC TESTS
# ============================================================================


def test_password_hashing_and_verification(db_session, test_config):
    """Test password hashing and verification"""

    password = "MySecurePassword123"
    user = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="password_test@example.com",
            name="Password User",
            password=password,
            config_id=test_config.config_id,
        ),
    )

    # Password should be hashed, not plain text
    assert user.password != password
    assert user.password.startswith("$argon2id$")

    # Should be able to authenticate
    authenticated_user = crud.get_user_by_email_password(
        db_session, "password_test@example.com", password
    )
    assert authenticated_user is not None
    assert authenticated_user.user_id == user.user_id

    # Wrong password should fail
    wrong_auth = crud.get_user_by_email_password(
        db_session, "password_test@example.com", "WrongPassword"
    )
    assert wrong_auth is None


def test_chat_history_workflow(
    db_session, test_user, test_project, test_session, test_chat, setup_reference_data
):
    """Test complete chat history workflow"""
    # Create context and telemetries
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="How do I create",
            suffix="in Python?",
            file_name="question.md",
            selected_text="a web server",
        ),
    )

    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(
            version_id=1,
            trigger_type_id=1,
            language_id=1,
        ),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session,
        Queries.BehavioralTelemetryData(typing_speed=250),
    )

    # Create chat query
    chat_query = crud.create_chat_query(
        db_session,
        Queries.CreateChatQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
            chat_id=test_chat.chat_id,
        ),
    )

    # Create generation (response) - fix this
    generation_data = Queries.CreateGeneration(
        model_id=1,
        completion="You can create a web server in Python using Flask or Django...",
        generation_time=100,
        shown_at=[datetime.now().isoformat()],
        was_accepted=True,
        confidence=0.9,
        logprobs=[-0.1, -0.2],
    )

    crud.create_generation(db_session, generation_data, str(chat_query.meta_query_id))

    # Get chat history
    history = crud.get_chat_history(db_session, test_chat.chat_id)
    assert len(history) == 1

    meta_query, context_obj, generations = history[0]
    assert meta_query.meta_query_id == chat_query.meta_query_id
    assert context_obj.context_id == context.context_id
    assert len(generations) == 1
    assert generations[0].completion.startswith("You can create a web server")


def test_project_chat_history(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test getting project chat history"""
    # Create multiple chats
    chat1_data = Queries.CreateChat(
        project_id=test_project.project_id, user_id=test_user.user_id, title="Chat 1"
    )
    chat1 = crud.create_chat(db_session, chat1_data, str(uuid.uuid4()))

    chat2_data = Queries.CreateChat(
        project_id=test_project.project_id, user_id=test_user.user_id, title="Chat 2"
    )
    chat2 = crud.create_chat(db_session, chat2_data, str(uuid.uuid4()))

    # Get project chat history
    history = crud.get_project_chat_history(
        db_session, test_project.project_id, test_user.user_id, page_number=1
    )

    # Should return empty if no chat queries exist yet
    assert isinstance(history, list)


# ============================================================================
# EDGE CASE TESTS
# ============================================================================


def test_empty_and_null_values(db_session, test_config):
    """Test handling of empty strings and optional fields"""

    # Test user with minimal data - should get default preferences
    user = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="minimal@example.com",
            name="Min",
            password="MinPass1",
            config_id=test_config.config_id,
        ),
    )

    # Should get default preference from DEFAULT_USER_PREFERENCE
    assert user.preference is not None
    preference_dict = json.loads(user.preference)
    assert "store_context" in preference_dict

    # Test context with all optional fields as None
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="",  # Empty string instead of None
            suffix="",  # Empty string instead of None
            file_name=None,
            selected_text=None,
        ),
    )

    assert context.prefix == ""
    assert context.suffix == ""


def test_update_generation(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test updating generation"""
    # Create completion query and generation
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    generation_data = Queries.CreateGeneration(
        model_id=1,
        completion="Original completion",
        generation_time=50,
        shown_at=[datetime.now().isoformat()],
        was_accepted=False,
        confidence=0.8,
        logprobs=[-0.1],
    )

    crud.create_generation(
        db_session, generation_data, str(completion_query.meta_query_id)
    )

    # Update generation
    update_data = Queries.UpdateGeneration(was_accepted=True)

    result = crud.update_generation(
        db_session, str(completion_query.meta_query_id), 1, update_data
    )
    assert result > 0  # Should return True (converted to int > 0)


# ============================================================================
# DOCUMENTATION TESTS WITH MOCKING
# ============================================================================


@patch("database.crud.encode_text")
def test_create_documentation_with_mocked_embedding(
    mock_encode_text, db_session, setup_reference_data
):
    """Test creating documentation with mocked embedding"""
    mock_embedding = [0.1] * 384
    mock_encode_text.return_value = mock_embedding

    doc_data = Queries.CreateDocumentation(
        content="def hello(): print('Hello, World!')", language="python"
    )

    created_doc = crud.create_documentation(db_session, doc_data)

    assert created_doc is not None
    assert created_doc.content == doc_data.content
    assert created_doc.language == doc_data.language
    assert np.allclose(created_doc.embedding, mock_embedding)
    mock_encode_text.assert_called_once_with(doc_data.content)


@patch("database.crud.encode_text")
def test_create_documentation_embedding_failure(
    mock_encode_text, db_session, setup_reference_data
):
    """Test creating documentation when embedding generation fails"""
    mock_encode_text.side_effect = Exception("Embedding service failed")

    doc_data = Queries.CreateDocumentation(
        content="def hello(): print('Hello, World!')", language="python"
    )

    created_doc = crud.create_documentation(db_session, doc_data)

    assert created_doc is not None
    assert created_doc.content == doc_data.content
    assert created_doc.language == doc_data.language
    assert created_doc.embedding is None  # Should be None due to failure


# ============================================================================
# CASCADE DELETE TESTS
# ============================================================================


def test_delete_meta_query_cascade(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test cascade deletion of meta query"""
    # Create completion query with generation
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    # Create generation
    generation_data = Queries.CreateGeneration(
        model_id=1,
        completion="Test completion",
        generation_time=50,
        shown_at=[datetime.now().isoformat()],
        was_accepted=False,
        confidence=0.8,
        logprobs=[-0.1],
    )

    # Store the ID before deletion:
    meta_query_id = completion_query.meta_query_id

    # Delete meta query (should cascade to completion_query and generation)
    result = crud.delete_meta_query_cascade(db_session, meta_query_id)
    assert result is True

    # Verify deletion
    deleted_meta_query = crud.get_meta_query_by_id(db_session, meta_query_id)
    assert deleted_meta_query is None

    deleted_completion_query = crud.get_completion_query_by_id(
        db_session, meta_query_id
    )
    assert deleted_completion_query is None

    generations = crud.get_generations_by_meta_query(db_session, meta_query_id)
    assert len(generations) == 0


def test_delete_chat_cascade(db_session, test_user, test_project, setup_reference_data):
    """Test cascade deletion of chat"""
    chat_data = Queries.CreateChat(
        project_id=test_project.project_id,
        user_id=test_user.user_id,
        title="Test Chat for Deletion",
    )
    chat = crud.create_chat(db_session, chat_data, str(uuid.uuid4()))

    # Delete chat
    result = crud.delete_chat_cascade(db_session, chat.chat_id)
    assert result is True

    # Verify deletion
    deleted_chat = crud.get_chat_by_id(db_session, chat.chat_id)
    assert deleted_chat is None


def test_delete_session_cascade(db_session, test_user):
    """Test cascade deletion of session"""
    session_data = Queries.CreateSession(user_id=test_user.user_id)
    session = crud.create_session(db_session, session_data)

    # Delete session
    result = crud.delete_session_cascade(db_session, session.session_id)
    assert result is True

    # Verify deletion
    deleted_session = crud.get_session_by_id(db_session, session.session_id)
    assert deleted_session is None


def test_delete_project_cascade(db_session, test_user):
    """Test cascade deletion of project"""
    project_data = Queries.CreateProject(project_name="Test Project for Deletion")
    project = crud.create_project(db_session, project_data)

    # Delete project
    result = crud.delete_project_cascade(db_session, project.project_id)
    assert result is True

    # Verify deletion
    deleted_project = crud.get_project_by_id(db_session, project.project_id)
    assert deleted_project is None


# ============================================================================
# PASSWORD VALIDATION TESTS
# ============================================================================


def test_password_validation():
    """Test password validation rules explicitly"""

    # Test various invalid passwords
    invalid_passwords = [
        "12345678",  # No uppercase or letters
        "password",  # No uppercase or digits
        "PASSWORD123",  # No lowercase
        "Password",  # No digits
        "Pass1",  # Too short
        "",  # Empty
    ]

    for password in invalid_passwords:
        with pytest.raises(ValidationError):
            Queries.CreateUser(
                email="test@example.com",
                name="Test User",
                password=password,
                config_id=1,
            )

    # Test valid passwords
    valid_passwords = [
        "Password123",
        "SecurePass1",
        "MyP@ssw0rd",
        "ValidPass1",
    ]

    for password in valid_passwords:
        # Should not raise an exception
        user_data = Queries.CreateUser(
            email="test@example.com",
            name="Test User",
            password=password,
            config_id=1,
        )
        assert user_data.password.get_secret_value() == password


# ============================================================================
# COMPREHENSIVE WORKFLOW TEST
# ============================================================================


def test_complete_workflow(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test a complete workflow from context creation to generation"""
    # 1. Create context
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="def workflow_test():",
            suffix="    return result",
            file_name="workflow.py",
            selected_text="workflow_test",
        ),
    )

    # 2. Create telemetries
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(
            version_id=1,
            trigger_type_id=2,
            language_id=1,
            file_path="/workflow.py",
            caret_line=10,
            document_char_length=500,
            relative_document_position=0.6,
        ),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session,
        Queries.BehavioralTelemetryData(
            time_since_last_shown=4000, time_since_last_accepted=8000, typing_speed=280
        ),
    )

    # 3. Create completion query
    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
            total_serving_time=120,
        ),
    )

    # 4. Create multiple generations - fix this
    for model_id in [1, 2]:
        generation_data = Queries.CreateGeneration(
            model_id=model_id,
            completion=f"def workflow_test():\n    result = model_{model_id}_output\n    return result",
            generation_time=40 + model_id * 10,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.8 + model_id * 0.05,
            logprobs=[-0.01, -0.02, -0.03],
        )

        crud.create_generation(
            db_session, generation_data, str(completion_query.meta_query_id)
        )

    # 5. Add ground truth
    crud.create_ground_truth(
        db_session,
        Queries.CreateGroundTruth(
            completion_query_id=completion_query.meta_query_id,
            ground_truth="def workflow_test():\n    result = calculate_workflow()\n    return result",
        ),
    )

    # 6. Verify everything exists
    retrieved_query = crud.get_completion_query_by_id(
        db_session, completion_query.meta_query_id
    )
    assert retrieved_query is not None

    generations = crud.get_generations_by_meta_query(
        db_session, completion_query.meta_query_id
    )
    assert len(generations) == 2

    ground_truths = crud.get_ground_truths_for_completion(
        db_session, completion_query.meta_query_id
    )
    assert len(ground_truths) == 1


# ============================================================================
# ADDITIONAL COVERAGE TESTS
# ============================================================================


def test_get_user_by_id_password(db_session, test_config):
    """Test get_user_by_id_password function"""
    password = "TestPassword123"
    user_data = Queries.CreateUser(
        email="id_password_test@example.com",
        name="ID Password Test User",
        password=password,
        config_id=test_config.config_id,
    )
    created_user = crud.create_user(db_session, user_data)

    # Test correct password
    auth_user = crud.get_user_by_id_password(db_session, created_user.user_id, password)
    assert auth_user is not None
    assert auth_user.user_id == created_user.user_id

    # Test wrong password
    wrong_auth = crud.get_user_by_id_password(
        db_session, created_user.user_id, "WrongPassword"
    )
    assert wrong_auth is None


def test_get_generations_by_meta_query_id_string(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test get_generations_by_meta_query_id with string parameter"""
    # Create completion query and generation
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    generation_data = Queries.CreateGeneration(
        model_id=1,
        completion="Test generation",
        generation_time=50,
        shown_at=[datetime.now().isoformat()],
        was_accepted=False,
        confidence=0.8,
        logprobs=[-0.1],
    )

    crud.create_generation(
        db_session, generation_data, str(completion_query.meta_query_id)
    )

    # Test with string parameter
    generations = crud.get_generations_by_meta_query_id(
        db_session, str(completion_query.meta_query_id)
    )
    assert len(generations) == 1


def test_user_full_wipe_out(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test delete_user_full_wipe_out function"""
    # Create some data associated with the user
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    # Create chat
    chat_data = Queries.CreateChat(
        project_id=test_project.project_id,
        user_id=test_user.user_id,
        title="Test Chat for Wipeout",
    )
    chat = crud.create_chat(db_session, chat_data, str(uuid.uuid4()))

    # Perform full wipeout
    crud.delete_user_full_wipe_out(db_session, test_user.user_id)

    # Verify user and associated data are deleted
    deleted_user = crud.get_user_by_id(db_session, test_user.user_id)
    assert deleted_user is None


def test_hash_and_verify_password_functions(db_session):
    """Test password hashing and verification utility functions"""
    from database.crud import hash_password, verify_password

    password = "TestPassword123"
    hashed = hash_password(password)

    assert hashed != password
    assert hashed.startswith("$argon2id$")

    # Test correct password
    assert verify_password(hashed, password) is True

    # Test wrong password
    assert verify_password(hashed, "WrongPassword") is False

    # Test invalid hash
    assert verify_password("invalid_hash", password) is False


def test_create_context_with_custom_id(db_session):
    """Test creating context with custom ID"""
    custom_id = str(uuid.uuid4())
    context_data = Queries.ContextData(
        prefix="test prefix",
        suffix="test suffix",
        file_name="test.py",
    )

    created_context = crud.create_context(db_session, context_data, custom_id)
    assert str(created_context.context_id) == custom_id


def test_create_contextual_telemetry_with_custom_id(db_session, setup_reference_data):
    """Test creating contextual telemetry with custom ID"""
    custom_id = str(uuid.uuid4())
    telemetry_data = Queries.ContextualTelemetryData(
        version_id=1,
        trigger_type_id=1,
        language_id=1,
    )

    created_telemetry = crud.create_contextual_telemetry(
        db_session, telemetry_data, custom_id
    )
    assert str(created_telemetry.contextual_telemetry_id) == custom_id


def test_create_behavioral_telemetry_with_custom_id(db_session):
    """Test creating behavioral telemetry with custom ID"""
    custom_id = str(uuid.uuid4())
    telemetry_data = Queries.BehavioralTelemetryData(typing_speed=250)

    created_telemetry = crud.create_behavioral_telemetry(
        db_session, telemetry_data, custom_id
    )
    assert str(created_telemetry.behavioral_telemetry_id) == custom_id


def test_create_session_with_custom_id(db_session, test_user):
    """Test creating session with custom ID"""
    custom_id = str(uuid.uuid4())
    session_data = Queries.CreateSession(user_id=test_user.user_id)

    created_session = crud.create_session(db_session, session_data, custom_id)
    assert str(created_session.session_id) == custom_id


def test_create_project_with_custom_id(db_session):
    """Test creating project with custom ID"""
    custom_id = str(uuid.uuid4())
    project_data = Queries.CreateProject(project_name="Custom ID Project")

    created_project = crud.create_project(db_session, project_data, custom_id)
    assert str(created_project.project_id) == custom_id


def test_existing_chat_id_handling(db_session, test_user, test_project):
    """Test handling of existing chat ID in create_chat"""
    chat_id = str(uuid.uuid4())
    chat_data = Queries.CreateChat(
        project_id=test_project.project_id,
        user_id=test_user.user_id,
        title="First Chat",
    )

    # Create first chat
    first_chat = crud.create_chat(db_session, chat_data, chat_id)

    # Try to create second chat with same ID and same user/project
    second_chat = crud.create_chat(db_session, chat_data, chat_id)

    # Should return the existing chat
    assert first_chat.chat_id == second_chat.chat_id
    assert first_chat.title == second_chat.title


def test_create_completion_query_with_custom_id(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test creating completion query with custom ID"""
    custom_id = str(uuid.uuid4())

    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    query_data = Queries.CreateCompletionQuery(
        user_id=test_user.user_id,
        contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
        behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
        context_id=context.context_id,
        session_id=test_session.session_id,
        project_id=test_project.project_id,
    )

    created_query = crud.create_completion_query(db_session, query_data, custom_id)
    assert str(created_query.meta_query_id) == custom_id


def test_create_chat_query_with_custom_id(
    db_session, test_user, test_project, test_session, test_chat, setup_reference_data
):
    """Test creating chat query with custom ID"""
    custom_id = str(uuid.uuid4())

    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    query_data = Queries.CreateChatQuery(
        user_id=test_user.user_id,
        contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
        behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
        context_id=context.context_id,
        session_id=test_session.session_id,
        project_id=test_project.project_id,
        chat_id=test_chat.chat_id,
    )

    created_query = crud.create_chat_query(db_session, query_data, custom_id)
    assert str(created_query.meta_query_id) == custom_id


def test_create_generation_with_custom_id(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test creating generation with custom ID (must reference existing meta_query)"""
    # First create a completion query to get a valid meta_query_id
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="test", suffix="test", file_name="test.py"),
    )
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )
    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    completion_query = crud.create_completion_query(
        db_session,
        Queries.CreateCompletionQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
        ),
    )

    # Use the existing meta_query_id as the custom_id
    custom_id = str(completion_query.meta_query_id)

    generation_data = Queries.CreateGeneration(
        model_id=1,
        completion="Test generation",
        generation_time=50,
        shown_at=[datetime.now().isoformat()],
        was_accepted=False,
        confidence=0.8,
        logprobs=[-0.1],
    )

    created_generation = crud.create_generation(db_session, generation_data, custom_id)
    assert str(created_generation.meta_query_id) == custom_id
