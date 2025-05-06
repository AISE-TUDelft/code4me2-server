import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid

from src.backend.database.db import Base
from src.backend.database.crud import create_auth_user, get_user_by_token
from src.backend.database.db_schemas import UserCreate

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


def test_create_and_get_user(db_session):
    """Test creating a user and then retrieving it by token"""
    # Create test user data
    user_token = str(uuid.uuid4())
    user_data = UserCreate(
        token=user_token,
        joined_at=datetime.now().isoformat(),
        email="test@example.com",
        name="Test User",
        password="securepassword123",
        is_google_signup=False,
        verified=True
    )

    # Create user in the database
    created_user = create_auth_user(db_session, user_data)

    # Verify the user was created correctly
    assert created_user.token == uuid.UUID(user_token)
    assert created_user.email == "test@example.com"
    assert created_user.name == "Test User"

    # Test retrieving the user by token
    retrieved_user = get_user_by_token(db_session, user_token)

    # Verify the retrieved user matches the created user
    assert retrieved_user is not None
    assert retrieved_user.token == uuid.UUID(user_token)
    assert retrieved_user.email == "test@example.com"
    assert retrieved_user.name == "Test User"