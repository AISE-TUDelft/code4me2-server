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
    auth_token = None
    if user_auth_token is not None:
        auth_info = redis_manager.get(
            "auth_token",
            str(user_auth_token),
        )
        if auth_info is not None:
            auth_token = str(user_auth_token)
    if auth_token is None:
        auth_token = create_uuid()
        redis_manager.set(
            "auth_token",
            auth_token,
            {"user_id": str(user_id), "session_token": ""},
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
    Authenticate a user via either OAuth (JWT token) or traditional email/password.

    This endpoint supports two methods of authentication:
    1. OAuth Authentication:
       - The input contains a JWT token from an OAuth provider (Google).
       - The token's validity is verified.
       - If valid, the user is fetched by email from the database.
       - A session auth token is created and returned as a cookie.
    2. Email/Password Authentication:
       - The input contains user email and password.
       - Credentials are verified against the database.
       - If valid, a session auth token is created and returned as a cookie.

    Args:
        user_to_authenticate: Union of OAuth token or email/password credentials.
        app: FastAPI dependency to access the application context.

    Returns:
        JsonResponseWithStatus: A JSON response containing the authenticated user info
        and a session auth token cookie on success, or an error response otherwise.
    """
    # Log the attempt to authenticate the user
    logging.info(
        f"Authenticating user ({user_to_authenticate.dict(hide_secrets=True)})"
    )

    # Retrieve dependencies for DB, session management, and config
    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()
    config = app.get_config()

    try:
        # OAuth Authentication path
        if isinstance(user_to_authenticate, AuthenticateUserOAuth):
            # Verify the JWT token from the OAuth provider
            verification_result = verify_jwt_token(user_to_authenticate.token)
            if verification_result is None:
                # Token invalid or expired
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredJWTToken(),
                )

            # Retrieve the user by the email embedded in the JWT token
            found_user = crud.get_user_by_email(
                db_session, verification_result["email"]
            )
            if not found_user:
                # User with this email does not exist
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredJWTToken(),
                )

            # Generate authentication token and create response with cookie
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
                    config=config_data.config_data,
                ),
            )
            # Set the auth token as an HttpOnly cookie with appropriate settings
            response_obj.set_cookie(
                key="auth_token",
                value=auth_token,
                httponly=True,
                samesite="lax",
                expires=config.auth_token_expires_in_seconds,
            )
            return response_obj

        # Email/Password Authentication path
        else:
            # Verify email and password against the database
            found_user = crud.get_user_by_email_password(
                db_session,
                str(user_to_authenticate.email),
                user_to_authenticate.password.get_secret_value(),
            )
            if not found_user:
                # Credentials are invalid
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidEmailOrPassword(),
                )
            # Generate authentication token and create response with cookie
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
                    config=str(config_data.config_data),
                ),
            )
            # Set the auth token as an HttpOnly cookie with appropriate settings
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

    This function is called automatically when the module is imported.
    It can be used to perform module-level setup, if needed.
    """
    pass
