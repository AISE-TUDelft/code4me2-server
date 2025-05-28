import logging

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
import Queries as Queries
from App import App
from backend.Responses import (
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredJWTToken,
    JsonResponseWithStatus,
    UpdateUserPutResponse,
)
from base_models import UserBase

router = APIRouter()


@router.put(
    "/",
    response_model=UpdateUserPutResponse,
    responses={
        "201": {"model": UpdateUserPutResponse},
        "401": {"model": InvalidOrExpiredJWTToken},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Update User"],
)
def update_user(
    user_to_update: Queries.UpdateUser,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie("auth_token"),
) -> JsonResponseWithStatus:
    logging.log(logging.INFO, f"Updating user: ({user_to_update})")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()

    user_id = session_manager.get_user_id_by_auth_token(auth_token)
    if auth_token is None or user_id is None:
        return JsonResponseWithStatus(
            status_code=401,
            content=InvalidOrExpiredAuthToken(),
        )
    updated_user = crud.update_user(
        db=db_session, user_id=user_id, user_to_update=user_to_update
    )
    return JsonResponseWithStatus(
        status_code=201,
        content=UpdateUserPutResponse(user=UserBase.model_validate(updated_user)),
    )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
