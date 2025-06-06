"""
This module defines the FastAPI router for user creation endpoints.
It handles user registration, including both standard and OAuth-based flows.
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

# Initialize the API router for user creation endpoints
router = APIRouter()


@router.post(
    "/",
    response_model=CreateUserPostResponse,
    responses={
        "201": {"model": CreateUserPostResponse},
        "401": {"model": InvalidOrExpiredJWTToken},
        "409": {"model": UserAlreadyExistsWithThisEmail},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": CreateUserError},
    },
    tags=["Create User"],
)
def create_user(
    user_to_create: Union[Queries.CreateUser, Queries.CreateUserOauth],
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Create a new user in the system.

    Args:
        user_to_create (Union[Queries.CreateUser, Queries.CreateUserOauth]):
            The user data to create, can be standard or OAuth-based.
        app (App):
            The application instance, injected by FastAPI's dependency system.

    Returns:
        JsonResponseWithStatus: Response with status code and content.

    Steps:
        1. Check if the user already exists by email.
        2. If OAuth, verify the JWT token and email.
        3. Create the user in the database if not exists.
        4. Send verification email.
        5. Return appropriate response.
    """
    # Log the user creation attempt
    logging.log(logging.INFO, f"Creating user: ({user_to_create})")
    db_session = app.get_db_session()

    try:
        # Check if user already exists in the database
        existing_user = crud.get_user_by_email(db_session, str(user_to_create.email))
        if existing_user:
            # User already exists, return 409 conflict
            return JsonResponseWithStatus(
                status_code=409,
                content=UserAlreadyExistsWithThisEmail(),
            )
        # If OAuth, verify the provided JWT token
        if isinstance(user_to_create, Queries.CreateUserOauth):
            verification_result = verify_jwt_token(user_to_create.token)
            if (
                verification_result is None
                or verification_result.get("email") != user_to_create.email
            ):
                # Invalid or expired token, return 401 unauthorized
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredJWTToken(),
                )

        # Create the user in the database
        user = crud.create_user(db_session, user_to_create)

        # TODO: Send verification email to the user

        # Return success response with the new user's ID
        return JsonResponseWithStatus(
            status_code=201,
            content=CreateUserPostResponse(
                user_id=str(user.user_id),
            ),
        )
    except Exception as e:
        logging.error(f"Error processing user creation request: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=CreateUserError(),
        )


def __init__():
    """
    Module initializer. Called when the module is imported.
    Used to initialize the module and import the router.
    """
    pass
