import logging

from fastapi import APIRouter, Cookie, Depends, Query
from fastapi.responses import HTMLResponse

import database.crud as crud
from App import App
from backend.Responses import (
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    JsonResponseWithStatus,
)
from celery_app.tasks.db_tasks import send_verification_email_task
from Queries import UpdateUser

router = APIRouter()


@router.get(
    "/check",
    responses={
        "200": {"description": "User verification status retrieved successfully"},
        "404": {
            "model": ErrorResponse,
            "description": "User not found or not authenticated",
        },
        "400": {"model": ErrorResponse, "description": "Bad request"},
        "500": {"model": ErrorResponse, "description": "Internal server error"},
    },
    tags=["User Verification"],
)
def check_verification(
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie("auth_token"),
):
    """
    Check if the user is verified
    """
    # Get Redis client
    redis_client = app.get_redis_manager()

    # Check if auth_token exists in Redis
    user_info = redis_client.get("auth_token", auth_token)

    if not user_info:
        return JsonResponseWithStatus(
            status_code=404,
            content=ErrorResponse(message="User not found or not authenticated."),
        )

    user_id = None
    try:
        user_id = user_info.get("user_id")
    except AttributeError:
        # If user_info is not a dict, it means the token is invalid or expired
        return JsonResponseWithStatus(
            status_code=401,
            content=ErrorResponse(message="Invalid or expired auth token."),
        )

    # Get user data from database
    db_session = app.get_db_session()
    user = crud.get_user_by_id(db_session, user_id)

    if not user:
        return JsonResponseWithStatus(
            status_code=404,
            content=ErrorResponse(message="User not found."),
        )

    # Return verification status
    return JsonResponseWithStatus(
        status_code=200,
        content={"verified": user.verified},
    )


@router.get(
    "/resend",
    responses={
        200: {"description": "Email sent"},
        404: {"description": "User not found or not authenticated"},
        401: {"description": "Invalid or expired auth token"},
        500: {"description": "Server error"},
    },
    tags=["User Verification"],
)
def resend_verification_email(
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie("auth_token"),
):
    """
    Resend verification email to the user
    """
    # Get Redis client
    redis_client = app.get_redis_manager()

    # Check if auth_token exists in Redis
    auth_info = redis_client.get("auth_token", auth_token)
    if auth_info is None:
        return JsonResponseWithStatus(
            status_code=401,
            content=InvalidOrExpiredAuthToken(),
        )

    user_id = auth_info.get("user_id")

    # If user_id is not found, return 404

    if not user_id:
        return JsonResponseWithStatus(
            status_code=404,
            content=ErrorResponse(message="User not found or not authenticated."),
        )

    # Get user data from database
    db_session = app.get_db_session()
    user = crud.get_user_by_id(db_session, user_id)

    if not user:
        return JsonResponseWithStatus(
            status_code=404,
            content=ErrorResponse(message="User not found."),
        )

    # Send verification email

    send_verification_email_task.delay(str(user.user_id), str(user.email), user.name)

    return JsonResponseWithStatus(
        status_code=200,
        content={"message": "Verification email resent successfully."},
    )


@router.get(
    "/",
    responses={
        "200": {"description": "Email verification successful"},
        "400": {"model": ErrorResponse},
        "404": {"model": ErrorResponse},
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

    # Check if token exists in Redis
    user_info = redis_client.get("email_verification", token)
    user_id = None
    try:
        user_id = user_info.get("user_id") if user_info else None
    except AttributeError:
        # If user_info is not a dict, it means the token is invalid or expired
        return JsonResponseWithStatus(
            status_code=404,
            content=ErrorResponse(
                message="The verification token is invalid or has expired."
            ),
        )

    if not user_id:
        return JsonResponseWithStatus(
            status_code=404,
            content=ErrorResponse(
                message="The verification token is invalid or has expired."
            ),
        )

    # Update user verification status
    db_session = app.get_db_session()
    try:
        # Get current user data first
        current_user = crud.get_user_by_id(db_session, user_id)
        if not current_user:
            return JsonResponseWithStatus(
                status_code=404,
                content=ErrorResponse(message="User not found."),
            )

        # Create UpdateUser object with only verified=True, preserving existing name
        update_data = UpdateUser(
            name=current_user.name, verified=True  # Preserve existing name
        )

        # Update user
        crud.update_user(db_session, user_id, update_data)

        # Return success response with HTML
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Email Verification Successful</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    text-align: center;
                    padding: 50px;
                }
                .success {
                    color: #4CAF50;
                    font-size: 24px;
                    margin-bottom: 20px;
                }
                .message {
                    font-size: 18px;
                    margin-bottom: 30px;
                }
                .button {
                    display: inline-block;
                    padding: 10px 20px;
                    background-color: #4CAF50;
                    color: white;
                    text-decoration: none;
                    border-radius: 5px;
                    font-size: 16px;
                }
            </style>
        </head>
        <body>
            <div class="success">Email Verification Successful!</div>
            <div class="message">Your email has been verified successfully. You can now close this page and continue using the application.</div>
            <a href="/" class="button">Go to Homepage</a>
        </body>
        </html>
        """

        return HTMLResponse(content=html_content, status_code=200)

    except Exception as e:
        logging.error(f"Error verifying email: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=ErrorResponse(message=f"Failed to verify email: {str(e)}"),
        )
