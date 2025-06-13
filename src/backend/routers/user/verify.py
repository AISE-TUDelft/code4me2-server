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

# Initialize API router for user verification routes
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
    Check if the currently authenticated user has verified their email.

    Parameters:
    - app (App): Dependency-injected application context providing access to services.
    - auth_token (str): Authentication token retrieved from the user's cookie.

    Returns:
    - JsonResponseWithStatus: User verification status or appropriate error message.
    """
    # Get Redis client
    redis_client = app.get_redis_manager()

    try:
        # Retrieve authentication information from Redis
        auth_info = redis_client.get("auth_token", auth_token)

        # Return 401 if the token is invalid or lacks user_id
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]

        # Retrieve user from database using user_id
        db_session = app.get_db_session()
        user = crud.get_user_by_id(db_session, user_id)

        # Return 404 if user is not found
        if not user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Return the verification status of the user
        return JsonResponseWithStatus(
            status_code=200,
            content=GetVerificationGetResponse(user_is_verified=user.verified),
        )
    except Exception as e:
        # Log and return 500 on unexpected failure
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
    Resend a verification email to the user if not already verified.

    Parameters:
    - app (App): Application context for accessing services.
    - auth_token (str): Auth token from cookie identifying the user.

    Returns:
    - JsonResponseWithStatus: Success confirmation or error message.
    """
    # Get Redis client
    redis_client = app.get_redis_manager()

    try:
        # Retrieve user info using the provided auth token
        auth_info = redis_client.get("auth_token", auth_token)

        # Return 401 if the token is missing or invalid
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]

        # Retrieve the user from the database
        db_session = app.get_db_session()
        user = crud.get_user_by_id(db_session, user_id)

        # Return 404 if the user is not found
        if not user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Enqueue a Celery task to send the verification email
        send_verification_email_task.delay(
            str(user.user_id), str(user.email), user.name
        )

        # Return success response
        return JsonResponseWithStatus(
            status_code=200, content=ResendVerificationEmailPostResponse()
        )
    except Exception as e:
        # Log error and return 500
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
    Verify the user's email address using the provided verification token.

    Parameters:
    - token (str): Token for email verification, passed via query parameter.
    - app (App): Application context for accessing services.

    Returns:
    - HTMLResponseWithStatus: HTML response indicating verification outcome.
    """
    # Get Redis and database session clients
    redis_client = app.get_redis_manager()
    db_session = app.get_db_session()

    try:
        # Lookup verification info from Redis using the token
        verification_info = redis_client.get("email_verification", token)

        # Return 401 if token is invalid or user_id is missing
        if verification_info is None or not verification_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredVerificationToken(),
            )

        user_id = verification_info["user_id"]

        # Fetch the user from the database
        current_user = crud.get_user_by_id(db_session, user_id)

        # Return 404 if user doesn't exist
        if not current_user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Prepare updated user data with verified=True
        update_data = UpdateUser(
            name=current_user.name,  # Preserve existing name
            verified=True,  # Set verification to true
        )

        # Update the user's verification status in the DB
        crud.update_user(db_session, user_id, update_data)

        # Return success response in HTML format
        return HTMLResponseWithStatus(
            content=VerifyUserPostHTMLResponse(), status_code=200
        )

    except Exception as e:
        # Log the error and return 500
        logging.error(f"Error verifying email: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=VerifyUserError(),
        )
