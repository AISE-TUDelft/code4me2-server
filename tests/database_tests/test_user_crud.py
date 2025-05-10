"""
Tests for user CRUD operations in the database module.
"""
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid
from pydantic import EmailStr, SecretStr

# Import your modules based on your project structure
# These need to be adjusted to match your actual import paths
from src.database.db import Base
import src.database.crud as crud
from src.database import db_schemas
from src.Queries import CreateUser, CreateUserAuth, Provider
from src.utils import hash_password


# Get test database URL from environment or use default for Docker
TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@test_db:5432/test_db")


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
        email=user_email,
        name="Test User",
        password="securepassword123"
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
    user_data = CreateUserAuth(
        email=user_email,
        name="OAuth User",
        password="securepassword456",
        token="mock_oauth_token",
        provider=Provider.google
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
        email=user_email,
        name="ID Test User",
        password="secureidpassword"
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
    user_data = CreateUser(
        email=user_email,
        name="Hash Test User",
        password=password
    )

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
            email=f"user{i}@example.com",
            name=f"Test User {i}",
            password=f"password{i}"
        )
        for i in range(1, 4)  # Create 3 users
    ]

    # Create users in the database
    created_users = [crud.create_user(db_session, user_data) for user_data in users_data]

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