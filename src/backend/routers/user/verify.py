import logging

from fastapi import APIRouter, Cookie, Depends, Query

import database.crud as crud
from App import App
from backend.Responses import (
    ErrorResponse,
    GetVerificationError,
    GetVerificationGetResponse,
    HTMLResponseWithStatus,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredVerificationToken,
    JsonResponseWithStatus,
    ResendVerificationEmailError,
    ResendVerificationEmailPostResponse,
    UserNotFoundError,
    VerifyUserError,
    VerifyUserPostHTMLResponse,
)
from celery_app.tasks.db_tasks import send_verification_email_task
from Queries import UpdateUser

router = APIRouter()


@router.get(
    "/check",
    responses={
        "200": {"model": GetVerificationGetResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": GetVerificationError},
    },
    tags=["User Verification"],
)
def check_verification(
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie(""),
):
    """
    Check if the user is verified
    """
    # Get Redis client
    redis_client = app.get_redis_manager()

    try:
        # Check if auth_token exists in Redis
        auth_info = redis_client.get("auth_token", auth_token)

        # If the token is invalid or missing, return a 401 error
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )
        user_id = auth_info["user_id"]

        # Get user data from database
        db_session = app.get_db_session()
        user = crud.get_user_by_id(db_session, user_id)

        if not user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Return verification status
        return JsonResponseWithStatus(
            status_code=200,
            content=GetVerificationGetResponse(user_is_verified=user.verified),
        )
    except Exception as e:
        logging.error(f"Error checking verification status: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=GetVerificationError(),
        )


@router.post(
    "/resend",
    responses={
        "200": {"model": ResendVerificationEmailPostResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ResendVerificationEmailError},
    },
    tags=["User Verification"],
)
def resend_verification_email(
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie(""),
):
    """
    Resend verification email to the user
    """
    # Get Redis client
    redis_client = app.get_redis_manager()

    try:
        # Check if auth_token exists in Redis
        auth_info = redis_client.get("auth_token", auth_token)

        # If the token is invalid or missing, return a 401 error
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )
        user_id = auth_info["user_id"]

        # Get user data from database
        db_session = app.get_db_session()
        user = crud.get_user_by_id(db_session, user_id)

        if not user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Send verification email
        send_verification_email_task.delay(
            str(user.user_id), str(user.email), user.name
        )

        return JsonResponseWithStatus(
            status_code=200, content=ResendVerificationEmailPostResponse()
        )
    except Exception as e:
        logging.error(f"Error resending verification email: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500, content=ResendVerificationEmailError()
        )


@router.post(
    "/",
    responses={
        "200": {"model": VerifyUserPostHTMLResponse},
        "401": {"model": InvalidOrExpiredVerificationToken},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": VerifyUserError},
    },
    tags=["User Verification"],
)
def verify_email(
    token: str = Query(..., description="Verification token"),
    app: App = Depends(App.get_instance),
):
    """
    Verify user email with the provided token
    """
    # Get Redis client
    redis_client = app.get_redis_manager()
    db_session = app.get_db_session()
    try:
        # Check if token exists in Redis
        verification_info = redis_client.get("email_verification", token)
        # If the token is invalid or missing, return a 401 error
        if verification_info is None or not verification_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredVerificationToken(),
            )
        user_id = verification_info["user_id"]

        # Get current user data first
        current_user = crud.get_user_by_id(db_session, user_id)
        if not current_user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Create UpdateUser object with only verified=True, preserving existing name
        update_data = UpdateUser(
            name=current_user.name, verified=True  # Preserve existing name
        )

        # Update user
        crud.update_user(db_session, user_id, update_data)

        return HTMLResponseWithStatus(
            content=VerifyUserPostHTMLResponse(), status_code=200
        )

    except Exception as e:
        logging.error(f"Error verifying email: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=VerifyUserError(),
        )
