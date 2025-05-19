import logging

from fastapi import APIRouter, Cookie, Depends, Query

import database.crud as crud
from App import App
from backend.Responses import (
    DeleteUserDeleteResponse,
    ErrorResponse,
    InvalidSessionToken,
    JsonResponseWithStatus,
)

router = APIRouter()


@router.delete(
    "/",
    response_model=DeleteUserDeleteResponse,
    responses={
        "200": {"model": DeleteUserDeleteResponse},
        "401": {"model": InvalidSessionToken},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Delete User"],
)
def delete_user(
    session_token: str = Cookie("session_token"),
    app: App = Depends(App.get_instance),
    delete_user_data: bool = Query(False, description="Delete users data"),
) -> JsonResponseWithStatus:
    logging.log(logging.INFO, "Deleting user")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()

    user_dict = session_manager.get_session(session_token)
    if session_token is None or user_dict is None:
        return JsonResponseWithStatus(
            status_code=401,
            content=InvalidSessionToken(),
        )

    if delete_user_data:
        crud.remove_user_by_id(db=db_session, user_id=user_dict["user_id"])
    session_manager.delete_user_sessions(user_id=user_dict["user_id"])
    return JsonResponseWithStatus(
        status_code=200,
        content=DeleteUserDeleteResponse(),
    )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
