import logging
from typing import Union

from fastapi import APIRouter
from fastapi import Depends
from jwt import ExpiredSignatureError, InvalidTokenError
from sqlalchemy.orm import Session

import database.crud as crud
from App import App
from Queries import AuthenticateUserEmailPassword, AuthenticateUserOAuth
from backend.models.Responses import (
    ErrorResponse,
    JsonResponseWithStatus,
)
from backend.models.Responses import (
    UserAuthenticatePostResponse,
)
from backend.utils import verify_jwt_token
from base_models import UserBase
from utils import hash_password

router = APIRouter()


@router.post(
    "/",
    response_model=UserAuthenticatePostResponse,
    responses={
        "200": {"model": UserAuthenticatePostResponse},
        "401": {"model": ErrorResponse},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Authentication"],
)
def authenticate_user(
    user_to_authenticate: Union[AuthenticateUserEmailPassword, AuthenticateUserOAuth],
    db_session: Session = Depends(App.get_db_session),
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
    logging.log(logging.INFO, f"Authenticating user {user_to_authenticate}")
    if isinstance(user_to_authenticate, AuthenticateUserOAuth):
        # OAuth Authentication
        try:
            verification_result = verify_jwt_token(user_to_authenticate.token)
            if verification_result is None:
                return JsonResponseWithStatus(
                    status_code=422,
                    content=ErrorResponse(message="Invalid or expired token!"),
                )
            found_user = crud.get_user_by_email(
                db_session, verification_result["email"]
            )
            if not found_user:
                return JsonResponseWithStatus(
                    status_code=401,
                    content=ErrorResponse(message="Invalid token"),
                )
            else:
                return JsonResponseWithStatus(
                    status_code=200,
                    content=UserAuthenticatePostResponse(
                        message="User authenticated successfully via OAuth",
                        user_id=found_user.user_id,
                        session_id=None,
                        user=UserBase.model_validate(found_user),
                    ),
                )
        except ExpiredSignatureError:
            logging.log(logging.INFO, f"Expired JWT: {user_to_authenticate.token}")
            return JsonResponseWithStatus(
                status_code=401, content=ErrorResponse(message="Token has expired")
            )
        except InvalidTokenError:
            logging.log(logging.INFO, f"Invalid JWT: {user_to_authenticate.token}")
            return JsonResponseWithStatus(
                status_code=401, content=ErrorResponse(message="Invalid token")
            )

    else:
        # Email/Password Authentication
        found_user = crud.get_user_by_email(db_session, str(user_to_authenticate.email))
        if not found_user or found_user.password_hash != hash_password(
            user_to_authenticate.password.get_secret_value()
        ):
            return JsonResponseWithStatus(
                status_code=401,
                content=ErrorResponse(message="Invalid email or password"),
            )
        else:
            return JsonResponseWithStatus(
                status_code=200,
                content=UserAuthenticatePostResponse(
                    message="User authenticated successfully via email and password",
                    user_id=found_user.user_id,
                    session_id=None,
                    user=UserBase.model_validate(found_user),
                ),
            )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
