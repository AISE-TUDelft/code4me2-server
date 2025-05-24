import logging
from typing import Union

from fastapi import APIRouter, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    AuthenticateUserNormalPostResponse,
    AuthenticateUserOAuthPostResponse,
    AuthenticateUserPostResponse,
    ErrorResponse,
    InvalidEmailOrPassword,
    InvalidOrExpiredJWTToken,
    JsonResponseWithStatus,
)
from backend.utils import verify_jwt_token
from base_models import UserBase
from Queries import AuthenticateUserEmailPassword, AuthenticateUserOAuth

router = APIRouter()


@router.post(
    "/",
    response_model=AuthenticateUserPostResponse,
    responses={
        "200": {"model": AuthenticateUserPostResponse},
        "401": {"model": Union[InvalidOrExpiredJWTToken, InvalidEmailOrPassword]},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Authentication"],
)
def authenticate_user(
    user_to_authenticate: Union[AuthenticateUserEmailPassword, AuthenticateUserOAuth],
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Authenticate a user
    Note: There are some nuances to how this should be handled.
    1. There is a possibility that the token field is JWT token for OAuth providers (this should be checked for)
    1.1. Authentication should first check if the token is a JWT token
    1.2. If it is a JWT token, then the validity of the token should be checked
    1.3. If the token is valid, then the user should be authenticated using the token and allocated a session
    1.4. The provider is always Google
    2. The filed can also simply represent a password for a user.
    3. The authentication should either return a JsonResponseWithStatus with content of UserAuthenticationPostResponse or a ErrorResponse
    """
    # TODO: check for too many requests using session manager and return 429 if needed
    logging.log(logging.INFO, f"Authenticating user ({user_to_authenticate})")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()
    config = app.get_config()

    if isinstance(user_to_authenticate, AuthenticateUserOAuth):
        # OAuth Authentication
        verification_result = verify_jwt_token(user_to_authenticate.token)
        if verification_result is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredJWTToken(),
            )
        found_user = crud.get_user_by_email(db_session, verification_result["email"])
        if not found_user:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredJWTToken(),
            )
        else:
            authentication_token = session_manager.create_auth_token(found_user.user_id)
            response_obj = JsonResponseWithStatus(
                status_code=200,
                content=AuthenticateUserOAuthPostResponse(
                    user=UserBase.model_validate(found_user),
                ),
            )

            response_obj.set_cookie(
                key="auth_token",
                value=authentication_token,
                httponly=True,
                samesite="lax",
                expires=config.authentication_token_expires_in_seconds,
            )
            return response_obj

    else:
        # Email/Password Authentication
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
        else:
            authentication_token = session_manager.create_auth_token(found_user.user_id)
            response_obj = JsonResponseWithStatus(
                status_code=200,
                content=AuthenticateUserNormalPostResponse(
                    user=UserBase.model_validate(found_user),
                ),
            )
            response_obj.set_cookie(
                key="auth_token",
                value=authentication_token,
                httponly=True,
                samesite="lax",
                expires=config.authentication_token_expires_in_seconds,
            )
            return response_obj


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
