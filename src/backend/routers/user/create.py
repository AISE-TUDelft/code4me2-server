from typing import Union, Optional

from fastapi import APIRouter

from src.backend.models.Bodies import NewUser
from src.backend.models.Responses import UserNewPostResponse, ErrorResponse

router = APIRouter()


@router.post(
    "/",
    response_model=None,
    responses={
        "201": {"model": UserNewPostResponse},
        "400": {"model": ErrorResponse},
        "409": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Create User"],
)
def create_user(body: NewUser) -> Optional[Union[UserNewPostResponse, ErrorResponse]]:
    """
    Create a new user
    1. The user should be created in the database if it does not exist
    2. The user should be sent a verification email
    3. The user should be sent a success message
    4. If the user already exists, then a 409 error should be returned
    """
    # Check if user already exists
    # existing_user = crud.get_user_by_email(db, body.email)
    # if existing_user:
    #     return Error(error="User already exists with this email")
    #
    # # Create user object
    # user_create = db_schemas.UserCreate(
    #     token=str(uuid.uuid4()),
    #     joined_at=datetime.now().isoformat(),
    #     email=body.email,
    #     name=body.name,
    #     password=body.password.get_secret_value(),
    #     is_google_signup=body.googleId is not None,
    #     verified=True  # Require email verification
    # )
    #
    # # Create user in database
    # user = crud.create_auth_user(db, user_create)
    #
    # # TODO: Send verification email
    #
    # return UserNewPostResponse(
    #     message="User created successfully. Please check your email for verification.",
    #     userId=user.token
    # )
    pass


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
