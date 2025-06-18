"""
This module defines a FastAPI router for user account creation.

Endpoints:
- POST /: Creates a new user using either standard or OAuth registration.

Features:
- Conflict checking for existing email addresses.
- Optional JWT verification for OAuth users.
- Asynchronous email verification via Celery.
- Structured response models and status codes.
"""

import logging
from typing import Union

from fastapi import APIRouter, Depends

import database.crud as crud
import Queries as Queries
from App import App
from backend.Responses import (
    CreateUserError,
    CreateUserPostResponse,
    ErrorResponse,
    InvalidOrExpiredJWTToken,
    JsonResponseWithStatus,
    UserAlreadyExistsWithThisEmail,
)
from backend.utils import verify_jwt_token

# Initialize the API router for user creation
router = APIRouter()


@router.post(
    "",
    response_model=CreateUserPostResponse,
    responses={
        "201": {"model": CreateUserPostResponse},
        "401": {"model": InvalidOrExpiredJWTToken},
        "409": {"model": UserAlreadyExistsWithThisEmail},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": CreateUserError},
    },
)
def create_user(
    user_to_create: Union[Queries.CreateUser, Queries.CreateUserOauth],
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Create a new user in the system using standard or OAuth-based data.

    Args:
        user_to_create (Union[CreateUser, CreateUserOauth]):
            User data from the request body (standard or OAuth-based).
        app (App):
            Application context with access to database and services.

    Returns:
        JsonResponseWithStatus: JSON response indicating success or failure.

    Flow:
        1. Check if a user already exists with the given email.
        2. If using OAuth, validate the JWT token.
        3. Insert the new user into the database.
        4. Send a verification email via Celery.
        5. Return HTTP 201 with the new user ID.
    """
    db_session = app.get_db_session()

    try:
        # Check for existing user by email
        existing_user = crud.get_user_by_email(db_session, str(user_to_create.email))
        if existing_user:
            return JsonResponseWithStatus(
                status_code=409,
                content=UserAlreadyExistsWithThisEmail(),
            )

        # Verify JWT token if OAuth-based registration
        if (
            isinstance(user_to_create, Queries.CreateUserOauth)
            and user_to_create.token != ""
        ):
            verification_result = verify_jwt_token(user_to_create.token)
            if (
                verification_result is None
                or verification_result.get("email") != user_to_create.email
            ):
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredJWTToken(),
                )

        # Create the user in the database
        user = crud.create_user(db_session, user_to_create)

        # Send verification email asynchronously via Celery
        from celery_app.tasks.db_tasks import send_verification_email_task

        send_verification_email_task.delay(
            str(user.user_id), str(user.email), user.name
        )

        return JsonResponseWithStatus(
            status_code=201,
            content=CreateUserPostResponse(user_id=user.user_id),
        )
    except Exception as e:
        logging.error(f"Error processing user creation request: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=CreateUserError(),
        )
    finally:
        db_session.close()


def __init__():
    """
    Optional module initializer.

    Placeholder for any setup logic required during import.
    """
    pass
