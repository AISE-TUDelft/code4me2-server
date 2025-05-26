import logging
from typing import Union

from fastapi import APIRouter, Depends

import database.crud as crud
import Queries as Queries
from App import App
from backend.Responses import (
    CreateUserPostResponse,
    ErrorResponse,
    InvalidOrExpiredToken,
    JsonResponseWithStatus,
    UserAlreadyExistsWithThisEmail,
)
from backend.utils import verify_jwt_token

router = APIRouter()


@router.post(
    "/",
    response_model=CreateUserPostResponse,
    responses={
        "201": {"model": CreateUserPostResponse},
        "401": {"model": InvalidOrExpiredToken},
        "409": {"model": UserAlreadyExistsWithThisEmail},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Create User"],
)
def create_user(
    user_to_create: Union[Queries.CreateUser, Queries.CreateUserOauth],
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Create a new user
    1. The user should be created in the database if it does not exist
    2. The user should be sent a verification email
    3. The user should be sent a success message
    4. If the user already exists, then a 409 error should be returned
    """
    # TODO: at this point if the given provider is not valid or if the token is empty the user will be created normally based on their email and password and no errors will be raised. This could be changed later on if needed.
    # Check if user already exists
    logging.log(logging.INFO, f"Creating user: ({user_to_create})")
    db_session = app.get_db_session()

    existing_user = crud.get_user_by_email(db_session, str(user_to_create.email))
    if existing_user:
        return JsonResponseWithStatus(
            status_code=409,
            content=UserAlreadyExistsWithThisEmail(),
        )
    if (
        isinstance(user_to_create, Queries.CreateUserOauth)
        and not len(user_to_create.token) == 0
    ):
        verification_result = verify_jwt_token(user_to_create.token)
        if (
            verification_result is None
            or verification_result["email"] != user_to_create.email
        ):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredToken(),
            )

    user = crud.create_user(db_session, user_to_create)

    # TODO: Send verification email

    return JsonResponseWithStatus(
        status_code=201,
        content=CreateUserPostResponse(
            user_id=user.user_id,
        ),
    )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
