from typing import Union

from fastapi import APIRouter

from src.backend.models.Requests import EmailPasswordAuth, OAuthAuth
from src.backend.models.Responses import UserAuthenticatePostResponse, ErrorResponse

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
    body: Union[EmailPasswordAuth, OAuthAuth],
) -> Union[UserAuthenticatePostResponse, ErrorResponse]:
    """
    Authenticate a user
    Note: There are some nuances to how this should be handled.
    1. There is a possibility that the password field is JWT token for OAuth providers (this should be checked for)
    1.1. Authentication should first check if the password is a JWT token
    1.2. If it is a JWT token, then the validity of the token should be checked
    1.3. If the token is valid, then the user should be authenticated using the token and allocated a session
    1.4. The provider is always Google
    2. The filed can also simply represent a password for a user.
    3. The authentication should either return a UserAuthenticationPostResponse or an ErrorResponse
    """
    # Get user by email
    # user = crud.get_user_by_email(db, body.email)
    # if not user:
    #     return Error(error="Invalid email or password")
    #
    # # Verify password
    # if not crud.verify_password(db, body.email, body.password.get_secret_value()):
    #     return Error(error="Invalid email or password")
    #
    # # Check if user is verified after adding verification process
    # # if not user.verified:
    # #    return Error(error="Please verify your email before logging in")
    #
    # # Generate session token - in this case, we just use the user's token (can be changed later)
    # return UserAuthenticatePostResponse(
    #     sessionToken=str(user.token),
    #     user={
    #         "id": user.token,  # Use token as ID
    #         "email": user.email,
    #         "name": user.name,
    #         "createdAt": user.joined_at,
    #         "verified": user.verified
    #     }
    # )
    print("Authenticate is called")
    pass


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
