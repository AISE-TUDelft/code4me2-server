import logging
import uuid
from typing import Union

from fastapi import APIRouter, Depends

import database.crud as crud
from App import App
from backend.redis_manager import RedisManager
from backend.Responses import (
    AuthenticateUserError,
    AuthenticateUserNormalPostResponse,
    AuthenticateUserOAuthPostResponse,
    AuthenticateUserPostResponse,
    ConfigNotFound,
    ErrorResponse,
    InvalidEmailOrPassword,
    InvalidOrExpiredJWTToken,
    JsonResponseWithStatus,
)
from backend.utils import verify_jwt_token
from Queries import AuthenticateUserEmailPassword, AuthenticateUserOAuth
from response_models import ResponseUser
from utils import create_uuid

# Initialize the FastAPI router for authentication endpoints
router = APIRouter()


def acquire_auth_token(
    user_auth_token: uuid.UUID, user_id: uuid.UUID, redis_manager: RedisManager
) -> str:
    """
    Acquire or generate an auth token for the user and store it in Redis.

    Args:
        user_auth_token (uuid.UUID): Existing token, if available.
        user_id (uuid.UUID): ID of the user.
        redis_manager (RedisManager): Redis manager to read/write token.

    Returns:
        str: The auth token as a string.
    """
    auth_token = None
    if user_auth_token is not None:
        auth_info = redis_manager.get("auth_token", str(user_auth_token))
        if auth_info is not None:
            auth_token = str(user_auth_token)

    if auth_token is None:
        auth_token = create_uuid()
        redis_manager.set(
            "auth_token",
            auth_token,
            {"user_id": str(user_id)},
            force_reset_exp=True,
        )
    return auth_token


@router.post(
    "",
    response_model=AuthenticateUserPostResponse,
    responses={
        "200": {"model": AuthenticateUserPostResponse},
        "401": {"model": Union[InvalidOrExpiredJWTToken, InvalidEmailOrPassword]},
        "404": {"model": ConfigNotFound},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": AuthenticateUserError},
    },
    tags=["Authentication"],
)
def authenticate_user(
    user_to_authenticate: Union[AuthenticateUserEmailPassword, AuthenticateUserOAuth],
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Authenticate a user using either OAuth (via JWT) or email/password.

    This endpoint supports:
    - OAuth: Validates a JWT token and fetches the user by email.
    - Email/Password: Verifies credentials against the database.

    Args:
        user_to_authenticate (Union[AuthenticateUserEmailPassword, AuthenticateUserOAuth]):
            Either an email/password object or a JWT-based OAuth object.
        app (App): FastAPI dependency that provides access to DB and config.

    Returns:
        JsonResponseWithStatus: Authenticated user info + auth token cookie,
        or an error response.
    """
    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()
    config = app.get_config()

    try:
        # OAuth Authentication
        if isinstance(user_to_authenticate, AuthenticateUserOAuth):
            verification_result = verify_jwt_token(user_to_authenticate.token)
            if verification_result is None:
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredJWTToken(),
                )

            found_user = crud.get_user_by_email(
                db_session, verification_result["email"]
            )
            if not found_user:
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredJWTToken(),
                )

            auth_token = acquire_auth_token(found_user.auth_token, found_user.user_id, redis_manager)  # type: ignore
            setattr(found_user, "auth_token", uuid.UUID(auth_token))

            config_data = crud.get_config_by_id(
                db_session, int(str(found_user.config_id))
            )
            if not config_data:
                return JsonResponseWithStatus(
                    status_code=404,
                    content=ConfigNotFound(),
                )

            response_obj = JsonResponseWithStatus(
                status_code=200,
                content=AuthenticateUserOAuthPostResponse(
                    user=ResponseUser.model_validate(found_user),
                    config=str(config_data.config_data),
                ),
            )
            response_obj.set_cookie(
                key="auth_token",
                value=auth_token,
                httponly=True,
                samesite="lax",
                expires=config.auth_token_expires_in_seconds,
            )
            return response_obj

        # Email/Password Authentication
        else:
            found_user = crud.get_user_by_email_password(
                db_session,
                str(user_to_authenticate.email),
                user_to_authenticate.password.get_secret_value(),
            )
            if not found_user:
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidEmailOrPassword(),
                )

            auth_token = acquire_auth_token(found_user.auth_token, found_user.user_id, redis_manager)  # type: ignore
            setattr(found_user, "auth_token", uuid.UUID(auth_token))

            config_data = crud.get_config_by_id(
                db_session, int(str(found_user.config_id))
            )
            if not config_data:
                return JsonResponseWithStatus(
                    status_code=404,
                    content=ConfigNotFound(),
                )

            response_obj = JsonResponseWithStatus(
                status_code=200,
                content=AuthenticateUserNormalPostResponse(
                    user=ResponseUser.model_validate(found_user),
                    config=config_data.config_data,
                ),
            )
            response_obj.set_cookie(
                key="auth_token",
                value=auth_token,
                httponly=True,
                samesite="lax",
                expires=config.auth_token_expires_in_seconds,
            )
            return response_obj

    except Exception as e:
        logging.error(f"Error processing user authentication request: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=AuthenticateUserError(),
        )


def __init__():
    """
    Module initializer placeholder.

    This function is invoked when the module is imported.
    Can be used for any future module-level setup.
    """
    pass
