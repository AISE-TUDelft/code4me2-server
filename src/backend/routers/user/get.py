import logging
import uuid

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    ConfigNotFound,
    ErrorResponse,
    GetUserError,
    GetUserGetResponse,
    InvalidOrExpiredAuthToken,
    JsonResponseWithStatus,
    UserNotFoundError,
)
from response_models import ResponseUser

router = APIRouter()


@router.get(
    "",
    response_model=GetUserGetResponse,
    responses={
        "200": {"model": GetUserGetResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": GetUserError},
    },
    tags=["User"],
)
def get_user_from_auth_token(
    auth_token: str = Cookie(""),
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Get the authenticated user based on the auth_token cookie.

    Args:
        auth_token (Optional[str]): Authentication token stored in cookie.
        app (App): FastAPI dependency to access DB and Redis.

    Returns:
        JsonResponseWithStatus: Response with user info or an error.
    """
    redis_manager = app.get_redis_manager()
    db_session = app.get_db_session()
    try:
        # Get auth data from Redis using the auth token
        auth_info = redis_manager.get("auth_token", auth_token)

        # If token is invalid or user ID is missing, respond with 401
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]
        logging.info(f"Getting user with ID: {user_id}")

        # Check if user exists in the database
        user = crud.get_user_by_id(db_session, uuid.UUID(user_id))
        if not user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        config_data = crud.get_config_by_id(db_session, int(str(user.config_id)))
        if not config_data:
            return JsonResponseWithStatus(
                status_code=404,
                content=ConfigNotFound(),
            )
        return JsonResponseWithStatus(
            status_code=200,
            content=GetUserGetResponse(
                user=ResponseUser.model_validate(user), config=config_data.config_data
            ),
        )

    except Exception as e:
        logging.error(f"Failed to get user from auth_token: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=GetUserError(),
        )
