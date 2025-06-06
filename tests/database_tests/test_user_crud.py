import json
import os
from datetime import datetime, timedelta

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
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/code4mev2_test"
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
        preference='{"theme": "dark", "language": "en"}',
    )
    created_user = crud.create_user(db_session, user_data)
    return created_user


@pytest.fixture(scope="function")
def test_project(db_session, test_user):
    """Create a test project"""
    project_data = Queries.CreateProject(
        project_name="Test Project",
        multi_file_contexts='{"files": []}',
        multi_file_context_changes='{"changes": []}',
    )
    created_project = crud.create_project(db_session, project_data)

    # Add user to project
    project_user_data = Queries.AddUserToProject(
        project_id=created_project.project_id,
        user_id=test_user.user_id,
        # role="owner"
    )
    crud.add_user_to_project(db_session, project_user_data)

    return created_project


@pytest.fixture(scope="function")
def test_session(db_session, test_user, test_project):
    """Create a test session"""
    session_data = Queries.CreateSession(
        user_id=test_user.user_id, project_id=test_project.project_id
    )
    created_session = crud.create_session(db_session, session_data)
    return created_session


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
        preference='{"theme": "light"}',
    )

    created_user = crud.create_user(db_session, user_data)

    assert created_user.email == user_email
    assert created_user.name == "Test User"
    assert created_user.config_id == test_config.config_id
    assert created_user.preference == '{"theme": "light"}'
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
        name="Updated Name", preference='{"theme": "auto", "notifications": true}'
    )

    updated_user = crud.update_user(db_session, test_user.user_id, update_data)

    assert updated_user is not None
    assert updated_user.name == "Updated Name"
    assert updated_user.preference == '{"theme": "auto", "notifications": true}'


# ============================================================================
# PROJECT TESTS
# ============================================================================


def test_create_and_get_project(db_session):
    """Test creating and retrieving a project"""
    project_data = Queries.CreateProject(
        project_name="My Test Project",
        multi_file_contexts='{"context1": "data"}',
        multi_file_context_changes='{"change1": "data"}',
    )

    created_project = crud.create_project(db_session, project_data)

    assert created_project is not None
    assert created_project.project_name == "My Test Project"
    assert created_project.multi_file_contexts == '{"context1": "data"}'

    retrieved_project = crud.get_project_by_id(db_session, created_project.project_id)
    assert retrieved_project is not None
    assert retrieved_project.project_id == created_project.project_id


def test_add_user_to_project(db_session, test_user, test_project):
    """Test adding a user to a project"""
    # User should already be added via fixture, test retrieval
    project_users = crud.get_project_users(db_session, test_project.project_id)

    assert len(project_users) == 1
    assert project_users[0].user_id == test_user.user_id
    # assert project_users[0].role == "owner"


def test_get_projects_for_user(db_session, test_user, test_project):
    """Test getting all projects for a user"""
    projects = crud.get_projects_for_user(db_session, test_user.user_id)

    assert len(projects) == 1
    assert projects[0].project_id == test_project.project_id


# ============================================================================
# SESSION TESTS
# ============================================================================


def test_create_and_get_session(db_session, test_user, test_project):
    """Test creating and retrieving a session"""
    session_data = Queries.CreateSession(
        user_id=test_user.user_id, project_id=test_project.project_id
    )

    created_session = crud.create_session(db_session, session_data)

    assert created_session is not None
    assert created_session.user_id == test_user.user_id
    assert created_session.project_id == test_project.project_id
    assert created_session.start_time is not None
    assert created_session.end_time is None

    retrieved_session = crud.get_session_by_id(
        db_session, created_session.session_id, test_user.user_id
    )
    assert retrieved_session is not None


def test_update_session_end_time(db_session, test_session):
    """Test updating session end time"""
    end_time = datetime.now().isoformat()
    update_data = Queries.UpdateSession(end_time=end_time)

    updated_session = crud.update_session(
        db_session, test_session.session_id, test_session.user_id, update_data
    )

    assert updated_session is not None
    assert updated_session.end_time is not None


# ============================================================================
# CHAT TESTS
# ============================================================================


def test_create_and_get_chat(db_session, test_user, test_project):
    """Test creating and retrieving a chat"""
    chat_data = Queries.CreateChat(
        project_id=test_project.project_id, user_id=test_user.user_id, title="Test Chat"
    )

    created_chat = crud.create_chat(db_session, chat_data)

    assert created_chat is not None
    assert created_chat.title == "Test Chat"
    assert created_chat.project_id == test_project.project_id
    assert created_chat.user_id == test_user.user_id

    retrieved_chat = crud.get_chat_by_id(db_session, created_chat.chat_id)
    assert retrieved_chat is not None


def test_get_chats_for_project(db_session, test_user, test_project):
    """Test getting all chats for a project"""
    # Create multiple chats
    chat1 = crud.create_chat(
        db_session,
        Queries.CreateChat(
            project_id=test_project.project_id,
            user_id=test_user.user_id,
            title="Chat 1",
        ),
    )

    chat2 = crud.create_chat(
        db_session,
        Queries.CreateChat(
            project_id=test_project.project_id,
            user_id=test_user.user_id,
            title="Chat 2",
        ),
    )

    chats = crud.get_chats_for_project(db_session, test_project.project_id)
    assert len(chats) == 2


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
        multi_file_context_changes_indexes='{"index": 1}',
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
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test creating a chat query"""
    # Create chat
    chat = crud.create_chat(
        db_session,
        Queries.CreateChat(
            project_id=test_project.project_id,
            user_id=test_user.user_id,
            title="Test Chat for Query",
        ),
    )

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
        chat_id=chat.chat_id,
        web_enabled=True,
        total_serving_time=200,
    )

    created_query = crud.create_chat_query(db_session, query_data)

    assert created_query is not None
    assert created_query.meta_query_id is not None
    assert created_query.chat_id == chat.chat_id
    assert created_query.web_enabled is True

    # Verify the meta_query was created correctly
    meta_query = crud.get_meta_query_by_id(db_session, created_query.meta_query_id)
    assert meta_query is not None
    assert meta_query.query_type == "chat"


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

    # Create generation
    current_time = datetime.now().isoformat()
    generation_data = Queries.CreateGeneration(
        meta_query_id=completion_query.meta_query_id,
        model_id=1,
        completion="def generate_test():\n    return 'Generated successfully!'",
        generation_time=50,
        shown_at=[current_time],
        was_accepted=False,
        confidence=0.85,
        logprobs=[-0.05, -0.1, -0.15],
    )

    created_generation = crud.create_generation(db_session, generation_data)

    assert created_generation is not None
    assert created_generation.meta_query_id == completion_query.meta_query_id
    assert created_generation.model_id == 1
    assert "Generated successfully!" in created_generation.completion
    assert created_generation.generation_time == 50
    assert len(created_generation.shown_at) == 1
    assert created_generation.was_accepted is False
    assert created_generation.confidence == 0.85


def test_update_generation_acceptance(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test updating generation acceptance status"""
    # Create completion query and generation (similar to above test)
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="def update_test():", suffix="    pass", file_name="update.py"
        ),
    )

    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(
            version_id=1,
            trigger_type_id=1,
            language_id=1,
            file_path="/update.py",
            caret_line=2,
            document_char_length=80,
        ),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=220)
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

    generation = crud.create_generation(
        db_session,
        Queries.CreateGeneration(
            meta_query_id=completion_query.meta_query_id,
            model_id=1,
            completion="def update_test():\n    return 'Updated!'",
            generation_time=60,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.9,
            logprobs=[-0.02, -0.05],
        ),
    )

    # Update acceptance status
    update_data = Queries.UpdateGenerationAcceptance(
        meta_query_id=completion_query.meta_query_id, model_id=1, was_accepted=True
    )

    updated_generation = crud.update_generation_acceptance(db_session, update_data)

    assert updated_generation is not None
    assert updated_generation.was_accepted is True


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


# ============================================================================
# INTEGRATION TESTS
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

    # 4. Create multiple generations
    for model_id in [1, 2]:
        generation = crud.create_generation(
            db_session,
            Queries.CreateGeneration(
                meta_query_id=completion_query.meta_query_id,
                model_id=model_id,
                completion=f"def workflow_test():\n    result = model_{model_id}_output\n    return result",
                generation_time=40 + model_id * 10,
                shown_at=[datetime.now().isoformat()],
                was_accepted=False,
                confidence=0.8 + model_id * 0.05,
                logprobs=[-0.01, -0.02, -0.03],
            ),
        )

    # 5. Accept one generation
    crud.update_generation_acceptance(
        db_session,
        Queries.UpdateGenerationAcceptance(
            meta_query_id=completion_query.meta_query_id, model_id=2, was_accepted=True
        ),
    )

    # 6. Add ground truth
    crud.create_ground_truth(
        db_session,
        Queries.CreateGroundTruth(
            completion_query_id=completion_query.meta_query_id,
            ground_truth="def workflow_test():\n    result = calculate_workflow()\n    return result",
        ),
    )

    # 7. Verify everything exists
    retrieved_query = crud.get_completion_query_by_id(
        db_session, completion_query.meta_query_id
    )
    assert retrieved_query is not None

    generations = crud.get_generations_by_meta_query(
        db_session, completion_query.meta_query_id
    )
    assert len(generations) == 2

    # Check that one generation was accepted
    accepted_generations = [g for g in generations if g.was_accepted]
    assert len(accepted_generations) == 1
    assert accepted_generations[0].model_id == 2

    ground_truths = crud.get_ground_truths_for_completion(
        db_session, completion_query.meta_query_id
    )
    assert len(ground_truths) == 1


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


# @pytest.mark.skip(
#     reason="SQLAlchemy relationship cascade configuration needs fixing - database constraints work correctly"
# )
def test_cascade_deletions(db_session, test_user, test_project, test_session):
    """Test cascade deletions work properly with SQLAlchemy ORM"""

    # Test 1: User-Session cascade

    # Create another user for testing
    config = crud.get_config_by_id(db_session, 1)
    test_user2 = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="cascade_user@example.com",
            name="Cascade User",
            password="SecurePassword123",
            config_id=config.config_id,
        ),
    )

    # Create sessions for this user
    session1 = crud.create_session(
        db_session,
        Queries.CreateSession(
            user_id=test_user2.user_id,
            project_id=test_project.project_id,
        ),
    )

    session2 = crud.create_session(
        db_session,
        Queries.CreateSession(
            user_id=test_user2.user_id,
            project_id=test_project.project_id,
        ),
    )

    # Verify sessions exist
    user_sessions_before = crud.get_sessions_for_user(db_session, test_user2.user_id)
    assert len(user_sessions_before) == 2

    # Store user ID for later verification
    user_id_to_delete = test_user2.user_id

    # Delete the user using cascade deletion method - should cascade to sessions
    result = crud.delete_user_cascade(db_session, user_id_to_delete)
    assert result is True

    # Verify user is gone
    deleted_user = crud.get_user_by_id(db_session, user_id_to_delete)
    assert deleted_user is None

    # Verify sessions were cascaded (should be empty)
    user_sessions_after = crud.get_sessions_for_user(db_session, user_id_to_delete)
    assert len(user_sessions_after) == 0

    # Test 2: Project-User relationship removal

    # Create another user and project for testing
    test_user3 = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="project_user@example.com",
            name="Project User",
            password="SecurePassword123",
            config_id=config.config_id,
        ),
    )

    test_project2 = crud.create_project(
        db_session,
        Queries.CreateProject(
            project_name="Cascade Test Project",
        ),
    )

    # Add user to project
    crud.add_user_to_project(
        db_session,
        Queries.AddUserToProject(
            project_id=test_project2.project_id,
            user_id=test_user3.user_id,
        ),
    )

    # Verify user is in project
    project_users_before = crud.get_project_users(db_session, test_project2.project_id)
    assert len(project_users_before) == 1
    assert project_users_before[0].user_id == test_user3.user_id

    # Delete the project using cascade deletion - should cascade to project_users relationship
    project_id_to_delete = test_project2.project_id
    result = crud.delete_project_cascade(db_session, project_id_to_delete)
    assert result is True

    # Verify project is gone
    deleted_project = crud.get_project_by_id(db_session, project_id_to_delete)
    assert deleted_project is None

    # Verify project-user relationship was removed
    project_users_after = crud.get_project_users(db_session, project_id_to_delete)
    assert len(project_users_after) == 0

    # User should still exist (users aren't owned by projects)
    still_existing_user = crud.get_user_by_id(db_session, test_user3.user_id)
    assert still_existing_user is not None

    # Test 3: Chat cascade deletion

    # Create chat
    simple_chat = crud.create_chat(
        db_session,
        Queries.CreateChat(
            project_id=test_project.project_id,
            user_id=test_user.user_id,
            title="Simple Cascade Test Chat",
        ),
    )

    # Store chat ID
    simple_chat_id = simple_chat.chat_id

    # Delete chat using cascade method
    result = crud.delete_chat_cascade(db_session, simple_chat_id)
    assert result is True

    # Verify chat is gone
    deleted_chat = crud.get_chat_by_id(db_session, simple_chat_id)
    assert deleted_chat is None


def test_meta_query_cascade_deletion(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test that meta_query deletion cascades properly to generations and ground truths"""

    # Create context and telemetries
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="def cascade_test():", suffix="pass", file_name="cascade.py"
        ),
    )

    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    # Create completion query
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
    generation = crud.create_generation(
        db_session,
        Queries.CreateGeneration(
            meta_query_id=completion_query.meta_query_id,
            model_id=1,
            completion="def cascade_test():\n    return 'cascaded'",
            generation_time=50,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.8,
            logprobs=[-0.1, -0.2],
        ),
    )

    # Create ground truth
    ground_truth = crud.create_ground_truth(
        db_session,
        Queries.CreateGroundTruth(
            completion_query_id=completion_query.meta_query_id,
            ground_truth="def cascade_test():\n    return 'actual truth'",
        ),
    )

    # Verify everything exists
    meta_query_id = completion_query.meta_query_id

    generations_before = crud.get_generations_by_meta_query(db_session, meta_query_id)
    assert len(generations_before) == 1

    ground_truths_before = crud.get_ground_truths_for_completion(
        db_session, meta_query_id
    )
    assert len(ground_truths_before) == 1

    # Delete meta_query using cascade method
    result = crud.delete_meta_query_cascade(db_session, meta_query_id)
    assert result is True

    # Verify meta_query is gone
    deleted_meta_query = crud.get_meta_query_by_id(db_session, meta_query_id)
    assert deleted_meta_query is None

    # Verify generations were cascaded
    generations_after = crud.get_generations_by_meta_query(db_session, meta_query_id)
    assert len(generations_after) == 0

    # Verify ground truths were cascaded
    ground_truths_after = crud.get_ground_truths_for_completion(
        db_session, meta_query_id
    )
    assert len(ground_truths_after) == 0


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


def test_complex_user_workflow(db_session, setup_reference_data):
    """Test a complete user workflow with multiple projects and sessions"""

    # Create user
    config = crud.get_config_by_id(db_session, 1)
    user = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="workflow@example.com",
            name="Workflow User",
            password="SecurePassword123",
            config_id=config.config_id,
        ),
    )

    # Create multiple projects
    projects = []
    for i in range(3):
        project = crud.create_project(
            db_session,
            Queries.CreateProject(
                project_name=f"Project {i + 1}",
            ),
        )
        projects.append(project)

        # Add user to project
        crud.add_user_to_project(
            db_session,
            Queries.AddUserToProject(
                project_id=project.project_id,
                user_id=user.user_id,
            ),
        )

    # Create sessions in each project
    sessions = []
    for project in projects:
        session = crud.create_session(
            db_session,
            Queries.CreateSession(
                user_id=user.user_id,
                project_id=project.project_id,
            ),
        )
        sessions.append(session)

    # Verify user can access all projects
    user_projects = crud.get_projects_for_user(db_session, user.user_id)
    assert len(user_projects) == 3

    # Verify sessions exist
    user_sessions = crud.get_sessions_for_user(db_session, user.user_id)
    assert len(user_sessions) == 3


def test_generation_acceptance_workflow(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test the complete generation acceptance workflow"""

    # Create context and telemetries
    context = crud.create_context(
        db_session,
        Queries.ContextData(prefix="def test():", suffix="pass", file_name="test.py"),
    )

    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    # Create completion query
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

    # Create multiple generations
    generations = []
    for model_id in [1, 2, 3]:
        generation = crud.create_generation(
            db_session,
            Queries.CreateGeneration(
                meta_query_id=completion_query.meta_query_id,
                model_id=model_id,
                completion=f"def test():\n    return {model_id}",
                generation_time=50 + model_id * 10,
                shown_at=[datetime.now().isoformat()],
                was_accepted=False,
                confidence=0.7 + model_id * 0.1,
                logprobs=[-0.1, -0.2, -0.3],
            ),
        )
        generations.append(generation)

    # Accept one generation
    crud.update_generation_acceptance(
        db_session,
        Queries.UpdateGenerationAcceptance(
            meta_query_id=completion_query.meta_query_id, model_id=2, was_accepted=True
        ),
    )

    # Verify acceptance
    all_generations = crud.get_generations_by_meta_query(
        db_session, completion_query.meta_query_id
    )
    accepted_count = sum(1 for g in all_generations if g.was_accepted)
    assert accepted_count == 1

    accepted_gen = next(g for g in all_generations if g.was_accepted)
    assert accepted_gen.model_id == 2


# ============================================================================
# EDGE CASE TESTS
# ============================================================================


def test_empty_and_null_values(db_session, test_config):
    """Test handling of empty strings and optional fields"""

    # Test user with minimal data - password must meet validation requirements
    user = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="minimal@example.com",
            name="Min",  # Minimum length (3 chars)
            password="MinPass1",  # Meets requirements: uppercase, lowercase, digit, 8+ chars
            config_id=test_config.config_id,
            preference=None,  # Optional field
        ),
    )

    assert user.preference is None

    # Test context with all optional fields as None
    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix=None,
            suffix=None,
            file_name=None,
            selected_text=None,
        ),
    )

    assert context.prefix is None
    assert context.suffix is None

    # Test with empty strings (different from None)
    context_empty = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix="",  # Empty string
            suffix="",  # Empty string
            file_name="empty.py",
            selected_text="",
        ),
    )

    assert context_empty.prefix == ""
    assert context_empty.suffix == ""
    assert context_empty.selected_text == ""


def test_large_text_fields(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test handling of large text content"""

    # Large prefix/suffix content
    large_text = "x" * 10000  # 10KB of text

    context = crud.create_context(
        db_session,
        Queries.ContextData(
            prefix=large_text,
            suffix=large_text,
            file_name="large_file.py",
            selected_text=large_text,
        ),
    )

    assert len(context.prefix) == 10000

    # Large completion text
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

    large_completion = "def large_function():\n" + "    # " + "x" * 5000
    generation = crud.create_generation(
        db_session,
        Queries.CreateGeneration(
            meta_query_id=completion_query.meta_query_id,
            model_id=1,
            completion=large_completion,
            generation_time=100,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.8,
            logprobs=[-0.1] * 100,  # Large logprobs array
        ),
    )

    assert len(generation.completion) > 5000
    assert len(generation.logprobs) == 100


def test_datetime_edge_cases(db_session, test_user, test_project, setup_reference_data):
    """Test datetime handling edge cases"""

    # Test session with very short duration
    session = crud.create_session(
        db_session,
        Queries.CreateSession(
            user_id=test_user.user_id,
            project_id=test_project.project_id,
        ),
    )

    # End session immediately
    end_time = datetime.now().isoformat()
    crud.update_session(
        db_session,
        session.session_id,
        test_user.user_id,
        Queries.UpdateSession(end_time=end_time),
    )

    updated_session = crud.get_session_by_id(
        db_session, session.session_id, test_user.user_id
    )
    assert updated_session.end_time is not None

    # Test generation with multiple show times
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
            session_id=session.session_id,
            project_id=test_project.project_id,
        ),
    )

    # Multiple timestamps
    now = datetime.now()
    timestamps = [
        now.isoformat(),
        (now + timedelta(seconds=1)).isoformat(),
        (now + timedelta(seconds=2)).isoformat(),
    ]

    generation = crud.create_generation(
        db_session,
        Queries.CreateGeneration(
            meta_query_id=completion_query.meta_query_id,
            model_id=1,
            completion="test completion",
            generation_time=50,
            shown_at=timestamps,
            was_accepted=False,
            confidence=0.8,
            logprobs=[-0.1, -0.2],
        ),
    )

    assert len(generation.shown_at) == 3


# ============================================================================
# JSON VALIDATION TESTS
# ============================================================================


def test_json_field_validation(db_session, test_config):
    """Test JSON string fields are properly handled"""

    # Test valid JSON in user preferences
    valid_prefs = '{"theme": "dark", "notifications": true, "language": "en"}'
    user = crud.create_user(
        db_session,
        Queries.CreateUser(
            email="json_test@example.com",
            name="JSON User",
            password="SecurePassword123",
            config_id=test_config.config_id,
            preference=valid_prefs,
        ),
    )

    # Verify JSON can be parsed
    prefs = json.loads(user.preference)
    assert prefs["theme"] == "dark"
    assert prefs["notifications"] is True

    # Test project with complex JSON
    complex_contexts = json.dumps(
        {
            "files": ["file1.py", "file2.py"],
            "imports": {"numpy": "np", "pandas": "pd"},
            "config": {"max_lines": 1000},
        }
    )

    project = crud.create_project(
        db_session,
        Queries.CreateProject(
            project_name="JSON Project",
            multi_file_contexts=complex_contexts,
        ),
    )

    parsed_contexts = json.loads(project.multi_file_contexts)
    assert len(parsed_contexts["files"]) == 2
    assert parsed_contexts["config"]["max_lines"] == 1000


# ============================================================================
# PERFORMANCE TESTS
# ============================================================================


def test_bulk_operations_performance(
    db_session, test_user, test_project, setup_reference_data
):
    """Test performance with larger datasets"""

    # Create many contexts
    contexts = []
    for i in range(100):
        context = crud.create_context(
            db_session,
            Queries.ContextData(
                prefix=f"def function_{i}():",
                suffix=f"    return {i}",
                file_name=f"file_{i}.py",
            ),
        )
        contexts.append(context)

    assert len(contexts) == 100

    # Create session for bulk operations
    session = crud.create_session(
        db_session,
        Queries.CreateSession(
            user_id=test_user.user_id,
            project_id=test_project.project_id,
        ),
    )

    # Create telemetries for bulk operations
    contextual_telemetry = crud.create_contextual_telemetry(
        db_session,
        Queries.ContextualTelemetryData(version_id=1, trigger_type_id=1, language_id=1),
    )

    behavioral_telemetry = crud.create_behavioral_telemetry(
        db_session, Queries.BehavioralTelemetryData(typing_speed=250)
    )

    # Create many completion queries
    queries = []
    for i, context in enumerate(contexts[:10]):  # Just 10 to keep test reasonable
        query = crud.create_completion_query(
            db_session,
            Queries.CreateCompletionQuery(
                user_id=test_user.user_id,
                contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
                behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
                context_id=context.context_id,
                session_id=session.session_id,
                project_id=test_project.project_id,
            ),
        )
        queries.append(query)

    assert len(queries) == 10

    # Verify we can retrieve them efficiently
    user_projects = crud.get_projects_for_user(db_session, test_user.user_id)
    assert len(user_projects) >= 1


# ============================================================================
# POLYMORPHIC INHERITANCE TESTS
# ============================================================================


def test_polymorphic_query_inheritance(
    db_session, test_user, test_project, test_session, setup_reference_data
):
    """Test that polymorphic inheritance works correctly"""

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

    # Create chat
    chat = crud.create_chat(
        db_session,
        Queries.CreateChat(
            project_id=test_project.project_id,
            user_id=test_user.user_id,
            title="Polymorphic Test Chat",
        ),
    )

    # Create both types of queries
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

    chat_query = crud.create_chat_query(
        db_session,
        Queries.CreateChatQuery(
            user_id=test_user.user_id,
            contextual_telemetry_id=contextual_telemetry.contextual_telemetry_id,
            behavioral_telemetry_id=behavioral_telemetry.behavioral_telemetry_id,
            context_id=context.context_id,
            session_id=test_session.session_id,
            project_id=test_project.project_id,
            chat_id=chat.chat_id,
        ),
    )

    # Test that we can retrieve both as MetaQueries
    completion_meta = crud.get_meta_query_by_id(
        db_session, completion_query.meta_query_id
    )
    chat_meta = crud.get_meta_query_by_id(db_session, chat_query.meta_query_id)

    assert completion_meta.query_type == "completion"
    assert chat_meta.query_type == "chat"

    # Test that we can retrieve specific types
    specific_completion = crud.get_completion_query_by_id(
        db_session, completion_query.meta_query_id
    )
    specific_chat = crud.get_chat_query_by_id(db_session, chat_query.meta_query_id)

    assert specific_completion is not None
    assert specific_chat is not None
    assert specific_chat.chat_id == chat.chat_id


# ============================================================================
# CONCURRENT ACCESS TESTS
# ============================================================================


def test_sequential_user_creation_simulating_concurrency(db_session, test_config):
    """Test multiple user creation operations sequentially (simpler alternative)"""

    users_created = []

    for i in range(3):
        try:
            user = crud.create_user(
                db_session,
                Queries.CreateUser(
                    email=f"sequential_{i}@example.com",
                    name=f"Sequential User {i}",
                    password="SecurePassword123",
                    config_id=test_config.config_id,
                ),
            )
            users_created.append(user)
        except Exception as e:
            print(f"Error creating user {i}: {e}")

    # Should be able to create multiple users
    assert len(users_created) == 3

    # Verify they all have different IDs
    user_ids = [u.user_id for u in users_created]
    assert len(set(user_ids)) == 3  # All unique


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
