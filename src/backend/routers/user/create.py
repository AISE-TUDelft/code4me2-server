import logging
from typing import Union

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

import Queries as Queries
import database.crud as crud
from backend.models.Responses import (
    CreateUserPostResponse,
    ErrorResponse,
    JsonResponseWithStatus,
)
from database import Database

router = APIRouter()


@router.post(
    "/",
    response_model=None,
    responses={
        "201": {"model": CreateUserPostResponse},
        "400": {"model": ErrorResponse},
        "409": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Create User"],
)
def create_user(
    user_to_create: Union[Queries.CreateUser, Queries.CreateUserAuth],
    db_session: Session = Depends(Database.get_db_session),
) -> JsonResponseWithStatus:
    """
    Create a new user
    1. The user should be created in the database if it does not exist
    2. The user should be sent a verification email
    3. The user should be sent a success message
    4. If the user already exists, then a 409 error should be returned
    """
    # Check if user already exists
    logging.log(logging.INFO, f"Creating user {user_to_create}")
    existing_user = crud.get_user_by_email(db_session, user_to_create.email)
    if existing_user:
        return JsonResponseWithStatus(
            status_code=409,
            content=ErrorResponse(error_message="User already exists with this email!"),
        )
    # Create user in database
    user = crud.create_user(db_session, user_to_create)

    # TODO: Send verification email

    return JsonResponseWithStatus(
        status_code=201,
        content=CreateUserPostResponse(
            message="User created successfully. Please check your email for verification.",
            user_token=user.token,
        ),
    )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
