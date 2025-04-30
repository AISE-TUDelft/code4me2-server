from typing import Union

from fastapi import APIRouter

from src.backend.models.Requests import UserExistsPostRequest
from src.backend.models.Responses import UserExistsPostResponse, ErrorResponse

router = APIRouter()


@router.post(
    "/",
    response_model=UserExistsPostResponse,
    responses={
        "400": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
    tags=["Exists"],
)
def check_user_exists(
    body: UserExistsPostRequest,
) -> Union[UserExistsPostResponse, ErrorResponse]:
    """
    Check if a user exists
    """
    # user = crud.get_user_by_email(db, body.email)
    # return UserExistsPostResponse(exists=user is not None)
    print("Exist is called")
    pass


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
