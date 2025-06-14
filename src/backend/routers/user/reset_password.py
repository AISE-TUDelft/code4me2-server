"""
Password reset router for Code4meV2.

This module provides endpoints for initiating password reset requests and processing
password changes through email-based verification tokens. Users can request a password
reset email and then use the provided link to securely change their password.
"""

import logging

from fastapi import APIRouter, Cookie, Depends, Form, Query

from App import App
from backend.email_utils import send_reset_password_email
from backend.Responses import (
    ErrorResponse,
    ErrorShowingPasswordResetForm,
    HTMLResponseWithStatus,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredResetToken,
    InvalidPreviousPassword,
    JsonResponseWithStatus,
    PasswordResetError,
    PasswordResetGetHTMLResponse,
    PasswordResetRequestPostResponse,
    UserNotFoundError,
)
from database import crud
from Queries import UpdateUser
from utils import create_uuid, verify_password

# Initialize the router
router = APIRouter()


@router.post(
    "/request",
    response_model=PasswordResetRequestPostResponse,
    responses={
        "200": {"model": PasswordResetRequestPostResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": PasswordResetError},
    },
)
def request_password_reset(
    auth_token: str = Cookie(""), app: App = Depends(App.get_instance)
) -> JsonResponseWithStatus:
    """
    Initiate a password reset request by sending a reset email to the user.

    This endpoint generates a secure reset token, stores it in Redis with expiration,
    and sends an email containing a link to the password reset form.

    Parameters:
    - email (str): The email address of the user requesting password reset
    - app (App): Application context for accessing services

    Returns:
    - JsonResponseWithStatus: Success confirmation or error response
    """
    db_session = app.get_db_session()
    redis_client = app.get_redis_manager()
    redis_manager = app.get_redis_manager()

    try:
        # Retrieve the user ID from the auth token
        auth_info = redis_manager.get("auth_token", auth_token)

        # If the token is invalid or missing, return a 401 error
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]
        # Fetch the user by ID from the database
        user = crud.get_user_by_id(db_session, user_id)
        # If user not found, return a 404 error
        if not user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Generate a secure reset token
        reset_token = create_uuid()

        # # Store the reset token in Redis with user info and expiration (15 minutes)
        app = App.get_instance()
        app.get_redis_manager().set(
            "password_reset",
            reset_token,
            {"user_id": user_id, "email": str(user.email)},
            force_reset_exp=True,
        )
        # Send the password reset email with the generated token
        send_reset_password_email(str(user.email), str(user.name), str(reset_token))

        # send_reset_password_email_task.delay(user_id, str(user.email), str(user.name))

        # Return success response
        return JsonResponseWithStatus(
            status_code=200,
            content=PasswordResetRequestPostResponse(),
        )

    except Exception as e:
        logging.error(f"Error processing password reset request: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=PasswordResetError(),
        )


@router.get(
    "/",
    response_model=PasswordResetGetHTMLResponse,
    responses={
        "200": {"model": PasswordResetGetHTMLResponse},
        "401": {"model": InvalidOrExpiredResetToken},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": PasswordResetError},
    },
)
def show_password_reset_form(
    token: str = Query(..., description="Password reset token"),
    app: App = Depends(App.get_instance),
) -> HTMLResponseWithStatus:
    """
    Display the password reset form page.

    This endpoint validates the reset token and displays an HTML form where
    users can enter their current password and new password to complete the reset.

    Parameters:
    - token (str): Password reset token from the email link
    - app (App): Application context for accessing services

    Returns:
    - HTMLResponse: Password reset form or error page
    """
    redis_client = app.get_redis_manager()

    try:
        # Validate the reset token
        reset_info = redis_client.get("password_reset", token)

        # Return 401 if token is invalid or expired
        if reset_info is None or not reset_info.get("user_id"):
            return HTMLResponseWithStatus(
                content=PasswordResetGetHTMLResponse(
                    error=InvalidOrExpiredResetToken().message
                ),
                status_code=401,
            )

        # Display the password reset form with the valid token
        return HTMLResponseWithStatus(
            content=PasswordResetGetHTMLResponse(token=token), status_code=200
        )

    except Exception as e:
        logging.error(f"Error displaying password reset form: {str(e)}")
        return HTMLResponseWithStatus(
            content=PasswordResetGetHTMLResponse(
                error=ErrorShowingPasswordResetForm().message
            ),
            status_code=500,
        )


@router.post(
    "/change",
    response_model=PasswordResetGetHTMLResponse,
    responses={
        "200": {"model": PasswordResetGetHTMLResponse},
        "401": {"model": InvalidOrExpiredResetToken},
        "403": {"model": InvalidPreviousPassword},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": PasswordResetError},
    },
)
def change_password(
    token: str = Form(..., description="Password reset token"),
    current_password: str = Form(..., description="Current password for verification"),
    new_password: str = Form(..., description="New password", min_length=8),
    app: App = Depends(App.get_instance),
) -> HTMLResponseWithStatus:
    """
    Process the password change request.

    This endpoint validates the reset token, verifies the current password,
    ensures the new password matches the confirmation, and updates the user's
    password in the database.

    Parameters:
    - token (str): Password reset token from the form
    - current_password (str): User's current password for verification
    - new_password (str): The new password to set
    - confirm_password (str): Confirmation of the new password
    - app (App): Application context for accessing services

    Returns:
    - HTMLResponse: Success page or error page with appropriate status
    """
    redis_client = app.get_redis_manager()
    db_session = app.get_db_session()

    try:
        # Validate the reset token
        reset_info = redis_client.get("password_reset", token)

        # Return 401 if token is invalid or expired
        if reset_info is None or not reset_info.get("user_id"):
            return HTMLResponseWithStatus(
                content=PasswordResetGetHTMLResponse(
                    error=InvalidOrExpiredResetToken().message
                ),
                status_code=401,
            )

        user_id = reset_info["user_id"]

        # Fetch the user from the database
        current_user = crud.get_user_by_id(db_session, user_id)

        # Return 404 if user doesn't exist
        if not current_user:
            return HTMLResponseWithStatus(
                content=PasswordResetGetHTMLResponse(error=UserNotFoundError().message),
                status_code=404,
            )

        # Verify the current password
        if not verify_password(current_password, current_user.password):
            return HTMLResponseWithStatus(
                content=PasswordResetGetHTMLResponse(
                    error=InvalidPreviousPassword().message
                ),
                status_code=403,
            )

        # Prepare updated user data with new password
        update_data = UpdateUser(
            name=current_user.name,  # Preserve existing name
            verified=current_user.verified,  # Preserve verification status
            password=new_password,
        )

        # Update the user's password in the database
        crud.update_user(db_session, user_id, update_data)

        # Delete the used reset token from Redis
        redis_client.delete("password_reset", token, db_session)

        # Return success response
        return HTMLResponseWithStatus(
            content=PasswordResetGetHTMLResponse(success=True), status_code=200
        )

    except Exception as e:
        logging.error(f"Error changing password: {str(e)}")
        return HTMLResponseWithStatus(
            content=PasswordResetGetHTMLResponse(error=PasswordResetError().message),
            status_code=500,
        )
