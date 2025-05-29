import logging

from fastapi import APIRouter, Cookie, Depends, Query

import database.crud as crud
from App import App
from backend.Responses import (
    DeleteUserDeleteResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    JsonResponseWithStatus,
    UserNotFoundError,
)

router = APIRouter()


@router.delete(
    "/",
    response_model=DeleteUserDeleteResponse,
    responses={
        "200": {"model": DeleteUserDeleteResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Delete User"],
)
def delete_user(
    delete_data: bool = Query(False, description="Delete users data"),
    auth_token: str = Cookie("auth_token"),
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()
    user_id = session_manager.get_user_id_by_auth_token(auth_token)
    if auth_token is None or user_id is None:
        return JsonResponseWithStatus(
            status_code=401,
            content=InvalidOrExpiredAuthToken(),
        )
    logging.log(logging.INFO, f"Deleting user with ID: {user_id}")
    session_manager.delete_user_sessions(db=db_session, user_id=user_id)
    session_manager.delete_user_auths(user_id=user_id)

    # TODO: check this later on
    if delete_data:
        logging.log(
            logging.INFO, f"Deleting user session and query data for user ID: {user_id}"
        )
        crud.remove_session_by_user_id(db=db_session, user_id=user_id)
        crud.remove_query_by_user_id(db=db_session, user_id=user_id)
    is_deleted = crud.remove_user_by_id(db=db_session, user_id=user_id)
    if is_deleted:
        return JsonResponseWithStatus(
            status_code=200,
            content=DeleteUserDeleteResponse(),
        )
    else:
        return JsonResponseWithStatus(
            status_code=404,
            content=UserNotFoundError(),
        )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
