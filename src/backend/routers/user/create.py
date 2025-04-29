from typing import Union, Optional

from fastapi import APIRouter

from backend.models.Bodies import NewUser
from backend.models.Responses import UserNewPostResponse, ErrorResponse

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
    print("Create is called")
    pass


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
