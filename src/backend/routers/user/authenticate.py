from typing import Union

from fastapi import APIRouter

from backend.models.Requests import EmailPasswordAuth, OAuthAuth
from backend.models.Responses import (
    UserAuthenticatePostResponse,
    ErrorResponse,
    JsonResponseWithStatus,
)

router = APIRouter()


@router.post(
    "/",
    response_model=UserAuthenticatePostResponse,
    responses={
        "400": {"model": ErrorResponse},
        "401": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Authentication"],
)
def authenticate_user(
    user_to_authenticate: Union[EmailPasswordAuth, OAuthAuth],
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
    3. The authentication should either return a UserAuthenticationPostResponse or anJsonErrorResponse
    """
    print("Authenticate is called")
    pass


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
