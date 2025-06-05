import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

import database.crud as crud
from App import App
from backend.Responses import ErrorResponse, JsonResponseWithStatus
from Queries import UpdateUser

router = APIRouter()


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
    redis_client = app.get_session_manager().redis_client

    # Check if token exists in Redis
    user_id = redis_client.get(f"email_verification:{token}")

    if not user_id:
        return JsonResponseWithStatus(
            status_code=404,
            content=ErrorResponse(
                message="The verification token is invalid or has expired."
            ),
        )

    # Decode user_id from bytes to string
    user_id = user_id.decode("utf-8")

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

        # Delete token from Redis
        redis_client.delete(f"email_verification:{token}")

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
            status_code=400,
            content=ErrorResponse(message=f"Failed to verify email: {str(e)}"),
        )
