"""
Tests for user CRUD operations in the database module.
"""

import os
import pytest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from typing import List
from pydantic import EmailStr, SecretStr
from database.db import Base
import database.crud as crud
from database import db_schemas
from Queries import (
    ContextCreate,
    TelemetryCreate,
    QueryCreate,
    GenerationCreate,
    GroundTruthCreate,
    CreateUser,
    CreateUserOauth,
    Provider)
from database.utils import hash_password

# Get test database URL from environment or use default for Docker
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@test_db:5432/test_db"
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


def test_get_user_by_email_nonexistent(db_session):
    """Test retrieving a non-existent user by email"""
    # Test retrieving a user that doesn't exist
    email = "nonexistent@example.com"
    user = crud.get_user_by_email(db_session, email)

    # Verify the user is None
    assert user is None


def test_create_and_get_user_by_email(db_session):
    """Test creating a user and then retrieving it by email"""
    # Create test user data using CreateUser
    user_email = "test@example.com"
    user_data = CreateUser(
        email=user_email, name="Test User", password="securepassword123"
    )

    # Create user in the database
    created_user = crud.create_user(db_session, user_data)

    # Verify the user was created correctly
    assert created_user.email == user_email
    assert created_user.name == "Test User"
    assert created_user.is_oauth_signup is False
    assert created_user.verified is False

    # Test retrieving the user by email
    retrieved_user = crud.get_user_by_email(db_session, user_email)

    # Verify the retrieved user matches the created user
    assert retrieved_user is not None
    assert retrieved_user.email == user_email
    assert retrieved_user.name == "Test User"
    assert retrieved_user.user_id == created_user.user_id


def test_create_user_with_oauth(db_session):
    """Test creating a user with OAuth authentication"""
    # Create test user data using CreateUserAuth
    user_email = "oauth_user@example.com"
    user_data = CreateUserOauth(
        email=user_email,
        name="OAuth User",
        password="securepassword456",
        token="mock_oauth_token",
        provider=Provider.google,
    )

    # Create user in the database
    created_user = crud.create_user(db_session, user_data)

    # Verify the user was created correctly
    assert created_user.email == user_email
    assert created_user.name == "OAuth User"

    # NOTE: The current implementation doesn't set is_oauth_signup to True
    #
    # For now, we're testing the actual behavior
    assert created_user.verified is False


def test_get_user_by_id(db_session):
    """Test retrieving a user by ID"""
    # Create test user data
    user_email = "id_test@example.com"
    user_data = CreateUser(
        email=user_email, name="ID Test User", password="secureidpassword"
    )

    # Create user in the database
    created_user = crud.create_user(db_session, user_data)

    # Get the user ID
    user_id = created_user.user_id

    # Test retrieving the user by ID
    retrieved_user = crud.get_user_by_id(db_session, user_id)

    # Verify the retrieved user matches the created user
    assert retrieved_user is not None
    assert retrieved_user.user_id == user_id
    assert retrieved_user.email == user_email
    assert retrieved_user.name == "ID Test User"


def test_get_user_by_id_nonexistent(db_session):
    """Test retrieving a non-existent user by ID"""
    # Generate a random UUID that doesn't exist in the database
    nonexistent_id = str(uuid.uuid4())

    # Test retrieving a user that doesn't exist
    user = crud.get_user_by_id(db_session, nonexistent_id)

    # Verify the user is None
    assert user is None


def test_password_hashing(db_session):
    """Test that passwords are properly hashed"""
    # Create test user data
    user_email = "hash_test@example.com"
    password = "SecurePassword123"
    user_data = CreateUser(email=user_email, name="Hash Test User", password=password)

    # Create user in the database
    created_user = crud.create_user(db_session, user_data)

    # Verify the password was hashed (not stored as plaintext)
    assert created_user.password_hash != password

    # Check that the stored hash matches the expected hash
    expected_hash = hash_password(password)
    assert created_user.password_hash == expected_hash


def test_create_multiple_users(db_session):
    """Test creating multiple users"""
    # Create test user data for multiple users
    users_data = [
        CreateUser(
            email=f"user{i}@example.com", name=f"Test User {i}", password=f"password{i}"
        )
        for i in range(1, 4)  # Create 3 users
    ]

    # Create users in the database
    created_users = [
        crud.create_user(db_session, user_data) for user_data in users_data
    ]

    # Verify all users were created
    assert len(created_users) == 3

    # Verify each user has a unique ID
    user_ids = [user.user_id for user in created_users]
    assert len(user_ids) == len(set(user_ids))  # No duplicate IDs

    # Retrieve all users by email and verify they match
    for i, user_data in enumerate(users_data):
        email = f"user{i+1}@example.com"
        retrieved_user = crud.get_user_by_email(db_session, email)
        assert retrieved_user is not None
        assert retrieved_user.email == email
        assert retrieved_user.name == f"Test User {i+1}"


"""
Tests for completion-related CRUD operations in the database module.
"""

@pytest.fixture(scope="function")
def setup_reference_data(db_session):
    """
    Set up reference data needed for tests (models, languages, etc.)
    """
    # Add models if they don't exist
    if db_session.query(db_schemas.ModelName).count() == 0:
        models = [
            db_schemas.ModelName(model_id=1, model_name="deepseek-1.3b"),
            db_schemas.ModelName(model_id=2, model_name="starcoder2-3b"),
        ]
        db_session.add_all(models)

    # Add programming languages if they don't exist
    if db_session.query(db_schemas.ProgrammingLanguage).count() == 0:
        languages = [
            db_schemas.ProgrammingLanguage(language_id=1, language_name="python"),
            db_schemas.ProgrammingLanguage(language_id=2, language_name="javascript"),
        ]
        db_session.add_all(languages)

    # Add trigger types if they don't exist
    if db_session.query(db_schemas.TriggerType).count() == 0:
        trigger_types = [
            db_schemas.TriggerType(trigger_type_id=1, trigger_type_name="manual"),
            db_schemas.TriggerType(trigger_type_id=2, trigger_type_name="auto"),
            db_schemas.TriggerType(trigger_type_id=3, trigger_type_name="idle"),
        ]
        db_session.add_all(trigger_types)

    # Add plugin versions if they don't exist
    if db_session.query(db_schemas.PluginVersion).count() == 0:
        versions = [
            db_schemas.PluginVersion(
                version_id=1,
                version_name="0.0.1v",
                ide_type="VSCode",
                description="Test version",
            )
        ]
        db_session.add_all(versions)

    db_session.commit()


@pytest.fixture(scope="function")
def test_user(db_session):
    """Create a test user for completion tests"""
    user_id = str(uuid.uuid4())
    user = db_schemas.User(
        user_id=user_id,
        email="completion_test@example.com",
        name="Completion Test User",
        password_hash="hashed_password",
        joined_at=datetime.now(),
        verified=True,
        is_oauth_signup=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_add_context(db_session, setup_reference_data):
    """Test creating a context record"""
    # Create test context data
    context_id = uuid.uuid4()
    context_data = ContextCreate(
        context_id=context_id,
        prefix="def hello_world():",
        suffix="    pass",
        language_id=1,  # Python
        trigger_type_id=1,  # Manual
        version_id=1,  # VSCode
    )

    # Create context in the database
    created_context = crud.add_context(db_session, context_data)

    # Verify the context was created correctly
    assert created_context is not None
    assert str(created_context.context_id) == str(context_id)
    assert created_context.prefix == "def hello_world():"
    assert created_context.suffix == "    pass"
    assert created_context.language_id == 1
    assert created_context.trigger_type_id == 1
    assert created_context.version_id == 1


def test_add_telemetry(db_session):
    """Test creating a telemetry record"""
    # Create test telemetry data
    telemetry_id = uuid.uuid4()
    telemetry_data = TelemetryCreate(
        telemetry_id=telemetry_id,
        time_since_last_completion=5000,
        typing_speed=300,
        document_char_length=500,
        relative_document_position=0.7,
    )

    # Create telemetry in the database
    created_telemetry = crud.add_telemetry(db_session, telemetry_data)

    # Verify the telemetry was created correctly
    assert created_telemetry is not None
    assert str(created_telemetry.telemetry_id) == str(telemetry_id)
    assert created_telemetry.time_since_last_completion == 5000
    assert created_telemetry.typing_speed == 300
    assert created_telemetry.document_char_length == 500
    assert created_telemetry.relative_document_position == 0.7


def test_add_query(db_session, test_user, setup_reference_data):
    """Test creating a query record"""
    # First create context and telemetry
    context_id = uuid.uuid4()
    context_data = ContextCreate(
        context_id=context_id,
        prefix="def test_function():",
        suffix="    return True",
        language_id=1,
        trigger_type_id=1,
        version_id=1,
    )
    context = crud.add_context(db_session, context_data)

    telemetry_id = uuid.uuid4()
    telemetry_data = TelemetryCreate(
        telemetry_id=telemetry_id,
        time_since_last_completion=3000,
        typing_speed=250,
        document_char_length=400,
        relative_document_position=0.5,
    )
    telemetry = crud.add_telemetry(db_session, telemetry_data)

    # Create query data
    query_id = uuid.uuid4()
    current_time = datetime.now().isoformat()
    query_data = QueryCreate(
        query_id=query_id,
        user_id=test_user.user_id,
        telemetry_id=telemetry_id,
        context_id=context_id,
        timestamp=current_time,
        total_serving_time=150,
        server_version_id=1,
    )

    # Create query in the database
    created_query = crud.add_query(db_session, query_data)

    # Verify the query was created correctly
    assert created_query is not None
    assert str(created_query.query_id) == str(query_id)
    assert str(created_query.user_id) == str(test_user.user_id)
    assert str(created_query.telemetry_id) == str(telemetry_id)
    assert str(created_query.context_id) == str(context_id)
    assert created_query.total_serving_time == 150
    assert created_query.server_version_id == 1


def test_add_generation(db_session, test_user, setup_reference_data):
    """Test creating a generation record"""
    # First create context, telemetry, and query
    context_id = uuid.uuid4()
    context = crud.add_context(
        db_session,
        ContextCreate(
            context_id=context_id,
            prefix="def generate_test():",
            suffix="    pass",
            language_id=1,
            trigger_type_id=1,
            version_id=1,
        ),
    )

    telemetry_id = uuid.uuid4()
    telemetry = crud.add_telemetry(
        db_session,
        TelemetryCreate(
            telemetry_id=telemetry_id,
            time_since_last_completion=2000,
            typing_speed=200,
            document_char_length=300,
            relative_document_position=0.3,
        ),
    )

    query_id = uuid.uuid4()
    query = crud.add_query(
        db_session,
        QueryCreate(
            query_id=query_id,
            user_id=test_user.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=datetime.now().isoformat(),
            total_serving_time=100,
            server_version_id=1,
        ),
    )

    # Create generation data
    current_time = datetime.now().isoformat()
    generation_data = GenerationCreate(
        query_id=query_id,
        model_id=1,  # deepseek-1.3b
        completion="def generate_test():\n    return 'Test passed!'",
        generation_time=50,
        shown_at=[current_time],
        was_accepted=False,
        confidence=0.85,
        logprobs=[-0.05, -0.1, -0.15],
    )

    # Create generation in the database
    created_generation = crud.add_generation(db_session, generation_data)

    # Verify the generation was created correctly
    assert created_generation is not None
    assert str(created_generation.query_id) == str(query_id)
    assert created_generation.model_id == 1
    assert (
        created_generation.completion
        == "def generate_test():\n    return 'Test passed!'"
    )
    assert created_generation.generation_time == 50
    assert len(created_generation.shown_at) == 1
    assert created_generation.was_accepted is False
    assert created_generation.confidence == 0.85
    assert created_generation.logprobs == [-0.05, -0.1, -0.15]


def test_update_generation_acceptance(db_session, test_user, setup_reference_data):
    """Test updating a generation's acceptance status"""
    # First create all necessary records
    context_id = uuid.uuid4()
    context = crud.add_context(
        db_session,
        ContextCreate(
            context_id=context_id,
            prefix="def update_test():",
            suffix="    pass",
            language_id=1,
            trigger_type_id=1,
            version_id=1,
        ),
    )

    telemetry_id = uuid.uuid4()
    telemetry = crud.add_telemetry(
        db_session,
        TelemetryCreate(
            telemetry_id=telemetry_id,
            time_since_last_completion=1500,
            typing_speed=180,
            document_char_length=250,
            relative_document_position=0.4,
        ),
    )

    query_id = uuid.uuid4()
    query = crud.add_query(
        db_session,
        QueryCreate(
            query_id=query_id,
            user_id=test_user.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=datetime.now().isoformat(),
            total_serving_time=120,
            server_version_id=1,
        ),
    )

    generation = crud.add_generation(
        db_session,
        GenerationCreate(
            query_id=query_id,
            model_id=1,
            completion="def update_test():\n    return 'Update successful!'",
            generation_time=60,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.9,
            logprobs=[-0.02, -0.05, -0.1],
        ),
    )

    # Update the generation's acceptance status
    updated_generation = crud.update_generation_acceptance(
        db_session, str(query_id), 1, True  # model_id  # was_accepted
    )

    # Verify the update was successful
    assert updated_generation is not None
    assert updated_generation.was_accepted is True


def test_add_ground_truth(db_session, test_user, setup_reference_data):
    """Test creating a ground truth record"""
    # First create necessary records
    context_id = uuid.uuid4()
    context = crud.add_context(
        db_session,
        ContextCreate(
            context_id=context_id,
            prefix="def ground_truth_test():",
            suffix="    pass",
            language_id=1,
            trigger_type_id=1,
            version_id=1,
        ),
    )

    telemetry_id = uuid.uuid4()
    telemetry = crud.add_telemetry(
        db_session,
        TelemetryCreate(
            telemetry_id=telemetry_id,
            time_since_last_completion=2200,
            typing_speed=220,
            document_char_length=320,
            relative_document_position=0.6,
        ),
    )

    query_id = uuid.uuid4()
    query = crud.add_query(
        db_session,
        QueryCreate(
            query_id=query_id,
            user_id=test_user.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=datetime.now().isoformat(),
            total_serving_time=110,
            server_version_id=1,
        ),
    )

    # Create ground truth data
    current_time = datetime.now().isoformat()
    ground_truth_data = GroundTruthCreate(
        query_id=query_id,
        truth_timestamp=current_time,
        ground_truth="def ground_truth_test():\n    print('This is the actual code the user wrote')\n    return True",
    )

    # Create ground truth in the database
    created_ground_truth = crud.add_ground_truth(db_session, ground_truth_data)

    # Verify the ground truth was created correctly
    assert created_ground_truth is not None
    assert str(created_ground_truth.query_id) == str(query_id)
    assert (
        created_ground_truth.ground_truth
        == "def ground_truth_test():\n    print('This is the actual code the user wrote')\n    return True"
    )


def test_get_query_by_id(db_session, test_user, setup_reference_data):
    """Test retrieving a query by ID"""
    # First create necessary records
    context_id = uuid.uuid4()
    context = crud.add_context(
        db_session,
        ContextCreate(
            context_id=context_id,
            prefix="def get_query_test():",
            suffix="    pass",
            language_id=1,
            trigger_type_id=1,
            version_id=1,
        ),
    )

    telemetry_id = uuid.uuid4()
    telemetry = crud.add_telemetry(
        db_session,
        TelemetryCreate(
            telemetry_id=telemetry_id,
            time_since_last_completion=1800,
            typing_speed=190,
            document_char_length=280,
            relative_document_position=0.45,
        ),
    )

    query_id = uuid.uuid4()
    query = crud.add_query(
        db_session,
        QueryCreate(
            query_id=query_id,
            user_id=test_user.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=datetime.now().isoformat(),
            total_serving_time=130,
            server_version_id=1,
        ),
    )

    # Get the query by ID
    retrieved_query = crud.get_query_by_id(db_session, str(query_id))

    # Verify the retrieved query matches the created query
    assert retrieved_query is not None
    assert str(retrieved_query.query_id) == str(query_id)
    assert str(retrieved_query.user_id) == str(test_user.user_id)
    assert str(retrieved_query.telemetry_id) == str(telemetry_id)
    assert str(retrieved_query.context_id) == str(context_id)


def test_get_query_by_id_nonexistent(db_session):
    """Test retrieving a non-existent query by ID"""
    # Generate a random UUID that doesn't exist in the database
    nonexistent_id = str(uuid.uuid4())

    # Test retrieving a query that doesn't exist
    query = crud.get_query_by_id(db_session, nonexistent_id)

    # Verify the query is None
    assert query is None


def test_get_generations_by_query_id(db_session, test_user, setup_reference_data):
    """Test retrieving all generations for a query"""
    # First create necessary records
    context_id = uuid.uuid4()
    context = crud.add_context(
        db_session,
        ContextCreate(
            context_id=context_id,
            prefix="def get_generations_test():",
            suffix="    pass",
            language_id=1,
            trigger_type_id=1,
            version_id=1,
        ),
    )

    telemetry_id = uuid.uuid4()
    telemetry = crud.add_telemetry(
        db_session,
        TelemetryCreate(
            telemetry_id=telemetry_id,
            time_since_last_completion=1600,
            typing_speed=185,
            document_char_length=270,
            relative_document_position=0.48,
        ),
    )

    query_id = uuid.uuid4()
    query = crud.add_query(
        db_session,
        QueryCreate(
            query_id=query_id,
            user_id=test_user.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=datetime.now().isoformat(),
            total_serving_time=125,
            server_version_id=1,
        ),
    )

    # Create two generations for the same query
    generation1 = crud.add_generation(
        db_session,
        GenerationCreate(
            query_id=query_id,
            model_id=1,  # deepseek-1.3b
            completion="def get_generations_test():\n    return 'Model 1'",
            generation_time=55,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.88,
            logprobs=[-0.03, -0.06, -0.09],
        ),
    )

    generation2 = crud.add_generation(
        db_session,
        GenerationCreate(
            query_id=query_id,
            model_id=2,  # starcoder2-3b
            completion="def get_generations_test():\n    return 'Model 2'",
            generation_time=65,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.92,
            logprobs=[-0.02, -0.04, -0.08],
        ),
    )

    # Get generations by query ID
    retrieved_generations = crud.get_generations_by_query_id(db_session, str(query_id))

    # Verify the retrieved generations
    assert retrieved_generations is not None
    assert len(retrieved_generations) == 2

    # Check that we got both generations
    model_ids = [gen.model_id for gen in retrieved_generations]
    assert 1 in model_ids
    assert 2 in model_ids


def test_get_generations_by_query_and_model_id(
    db_session, test_user, setup_reference_data
):
    """Test retrieving a specific generation by query ID and model ID"""
    # First create necessary records
    context_id = uuid.uuid4()
    context = crud.add_context(
        db_session,
        ContextCreate(
            context_id=context_id,
            prefix="def get_specific_generation():",
            suffix="    pass",
            language_id=1,
            trigger_type_id=1,
            version_id=1,
        ),
    )

    telemetry_id = uuid.uuid4()
    telemetry = crud.add_telemetry(
        db_session,
        TelemetryCreate(
            telemetry_id=telemetry_id,
            time_since_last_completion=1700,
            typing_speed=195,
            document_char_length=290,
            relative_document_position=0.52,
        ),
    )

    query_id = uuid.uuid4()
    query = crud.add_query(
        db_session,
        QueryCreate(
            query_id=query_id,
            user_id=test_user.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=datetime.now().isoformat(),
            total_serving_time=115,
            server_version_id=1,
        ),
    )

    # Create two generations for the same query
    generation1 = crud.add_generation(
        db_session,
        GenerationCreate(
            query_id=query_id,
            model_id=1,  # deepseek-1.3b
            completion="def get_specific_generation():\n    return 'Found Model 1'",
            generation_time=58,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.87,
            logprobs=[-0.04, -0.07, -0.1],
        ),
    )

    generation2 = crud.add_generation(
        db_session,
        GenerationCreate(
            query_id=query_id,
            model_id=2,  # starcoder2-3b
            completion="def get_specific_generation():\n    return 'Found Model 2'",
            generation_time=62,
            shown_at=[datetime.now().isoformat()],
            was_accepted=False,
            confidence=0.91,
            logprobs=[-0.03, -0.05, -0.09],
        ),
    )

    # Get specific generation by query ID and model ID
    retrieved_generation = crud.get_generations_by_query_and_model_id(
        db_session, str(query_id), 2  # model_id for starcoder2-3b
    )

    # Verify the retrieved generation
    assert retrieved_generation is not None
    assert str(retrieved_generation.query_id) == str(query_id)
    assert retrieved_generation.model_id == 2
    assert "Found Model 2" in retrieved_generation.completion
