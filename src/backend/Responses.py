from abc import ABC
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, PrivateAttr

from response_models import (
    ResponseCompletionResponseData,
    ResponseFeedbackResponseData,
    ResponseUser,
    SerializableBaseModel,
)


class BaseResponse(SerializableBaseModel, ABC):
    message: str = Field(..., description="Response message")


class ErrorResponse(BaseResponse, ABC):
    message: str = Field(..., description="Error message")


class JsonResponseWithStatus(JSONResponse):
    def __init__(self, content: BaseModel, status_code: int):
        self.content = content
        self.status_code = status_code
        # Convert the Pydantic model to a dict
        super().__init__(content=jsonable_encoder(content), status_code=status_code)

    def dict(self) -> dict:
        """Convert the response content to a dictionary."""
        return {
            "status_code": self.status_code,
            "content": jsonable_encoder(self.content),
        }


class HTMLResponseWithStatus(HTMLResponse):
    def __init__(self, content: BaseModel, status_code: int):
        self.content = content
        self.status_code = status_code
        # Extract the HTML string from the serialized model
        html_content = getattr(content, "html", None)
        if html_content is None:
            raise ValueError("HTML content missing from model.")
        super().__init__(content=html_content, status_code=status_code)

    def dict(self) -> dict:
        """Convert the response content to a dictionary."""
        return {
            "status_code": self.status_code,
            "content": jsonable_encoder(self.content),
        }


# /api/user/create
class CreateUserPostResponse(BaseResponse):
    message: str = Field(
        default="User created successfully. Please check your email for verification."
    )
    user_id: UUID = Field(..., description="Created user id")


class UserAlreadyExistsWithThisEmail(ErrorResponse):
    message: str = Field(default="User already exists with this email!")


class InvalidOrExpiredJWTToken(ErrorResponse):
    message: str = Field(default="Invalid or expired token!")


class CreateUserError(ErrorResponse):
    message: str = Field(default="Server failed to create a new user!")


# /api/user/authenticate
class AuthenticateUserPostResponse(BaseResponse, ABC):
    user: ResponseUser = Field(..., description="User details")
    config: str = Field(..., description="User's config HOCON string")


class AuthenticateUserNormalPostResponse(AuthenticateUserPostResponse):
    message: str = Field(
        default="User authenticated successfully via email and password."
    )


class AuthenticateUserOAuthPostResponse(AuthenticateUserPostResponse):
    message: str = Field(default="User authenticated successfully via OAuth.")


class InvalidEmailOrPassword(ErrorResponse):
    message: str = Field(default="Invalid email or password!")


class AuthenticateUserError(ErrorResponse):
    message: str = Field(default="Server failed to authenticate the user!")


class ConfigNotFound(ErrorResponse):
    message: str = Field(default="Config not found!")


# /api/user/delete
class DeleteUserDeleteResponse(BaseResponse):
    message: str = Field(default="User is deleted successfully.")


class DeleteUserError(ErrorResponse):
    message: str = Field(default="Server failed to delete the user!")


class InvalidOrExpiredSessionToken(ErrorResponse):
    message: str = Field(
        default="Session not found! You are not authenticated or your session has expired. "
        "Login before you can perform this action."
    )


class InvalidOrExpiredAuthToken(ErrorResponse):
    message: str = Field(
        default="Authentication token not found! You are not authenticated or your token has expired. "
        "Login before you can perform this action."
    )


# /api/user/update
class UpdateUserPutResponse(BaseResponse):
    message: str = Field(default="User is updated successfully.")
    user: ResponseUser = Field(..., description="User details")


class InvalidPreviousPassword(ErrorResponse):
    message: str = Field(default="Previous password is not correct!")


class UpdateUserError(ErrorResponse):
    message: str = Field(default="Server failed to update the user!")


# /api/completion/request
class CompletionPostResponse(BaseResponse):
    message: str = Field(
        default="Completions generated successfully. Ready for display in your IDE."
    )
    data: ResponseCompletionResponseData = Field(
        ..., description="Generated code completions including query ID and suggestions"
    )


class UserNotFoundError(ErrorResponse):
    message: str = Field(default="User not found")


class GenerateCompletionsError(ErrorResponse):
    message: str = Field(default="Server failed to generate completions.")


# /api/completion/feedback
class CompletionFeedbackPostResponse(BaseResponse):
    message: str = Field(default="Feedback recorded successfully.")
    data: ResponseFeedbackResponseData = Field(
        ..., description="Information about the recorded feedback"
    )


class FeedbackRecordingError(ErrorResponse):
    message: str = Field(default="Server failed to record feedback.")


class GenerationNotFoundError(ErrorResponse):
    message: str = Field(default="Generation record not found")


class QueryNotFoundError(ErrorResponse):
    message: str = Field(default="Query not found")


class CompletionsNotFoundError(ErrorResponse):
    message: str = Field(default="No completions found for this query")


class RetrieveCompletionsError(ErrorResponse):
    message: str = Field(default="Server failed to retrieve completions.")


# /api/chat/request
class GenerateChatCompletionsError(ErrorResponse):
    message: str = Field(default="Server failed to generate chat completions.")


# /api/chat/get
class RetrieveChatCompletionsError(ErrorResponse):
    message: str = Field(default="Server failed to retrieve chat completions.")


# /api/completion/multi-file-context/update
class MultiFileContextUpdatePostResponse(BaseResponse):
    message: str = Field(default="Multi-file context updated successfully.")
    data: dict = Field(..., description="Data related to the multi-file context update")


class MultiFileContextUpdateError(ErrorResponse):
    message: str = Field(default="Server failed to update multi-file context.")


# /api/project/create
class CreateProjectPostResponse(BaseResponse):
    message: str = Field(default="Project created successfully.")
    project_token: str = Field(..., description="Created project token.")


class CreateProjectError(ErrorResponse):
    message: str = Field(default="Server failed to create a project!")


# /api/project/activate
class ActivateProjectPostResponse(BaseResponse):
    message: str = Field(default="Project activated successfully.")


class ActivateProjectError(ErrorResponse):
    message: str = Field(default="Server failed to activate the project.")


class InvalidOrExpiredProjectToken(ErrorResponse):
    message: str = Field(
        default="Invalid or expired project token. Acquire a session token first."
    )


class ProjectNotFoundError(ErrorResponse):
    message: str = Field(
        default="Project not found. You may need to create or activate it again."
    )


# /api/session/deactivate
class DeactivateSessionPostResponse(BaseResponse):
    message: str = Field(default="Session deactivated successfully.")


class DeactivateSessionError(ErrorResponse):
    message: str = Field(default="Server failed to deactivate session.")


# /api/session/acquire
class AcquireSessionGetResponse(BaseResponse):
    message: str = Field(default="Session acquired successfully")
    session_token: str = Field(..., description="Session token")


class AcquireSessionError(BaseResponse):
    message: str = Field(default="Server failed to acquire a session.")


class SessionNotFoundError(ErrorResponse):
    message: str = Field(default="Session not found. You may need to log in again.")


# /api/completion/feedback
class NoAccessToProvideFeedbackError(ErrorResponse):
    message: str = Field(
        default="You are not allowed to provide feedback for this query."
    )


# /api/completion/get
class NoAccessToGetQueryError(ErrorResponse):
    message: str = Field(default="You do not have permission to access this query.")


class TooManyRequests(ErrorResponse):
    message: str = Field(default="Too many requests. Please try again later.")


# /api/chat/delete
class DeleteChatError(ErrorResponse):
    """Error response for chat deletion failure."""

    message: str = "Server failed to delete chat"


# /api/user/verify
class ResendVerificationEmailPostResponse(BaseResponse):
    message: str = Field(
        default="Verification email sent successfully. Please check your inbox."
    )


class ResendVerificationEmailError(ErrorResponse):
    message: str = Field(default="Server failed to resend verification email.")


class GetVerificationGetResponse(BaseResponse):
    message: str = Field(default="Verification get returned successfully.")
    user_is_verified: bool = Field(
        ..., description="Indicates whether the user is verified"
    )


class GetVerificationError(ErrorResponse):
    message: str = Field(default="Server failed to retrieve verification status.")


class VerifyUserGetHTMLResponse(BaseResponse):
    message: str = Field(default="User verified successfully.")
    html: str = Field(
        default="""<!DOCTYPE html>
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
</html>"""
    )


class VerifyUserError(ErrorResponse):
    message: str = Field(default="Server failed to verify the user.")


class InvalidOrExpiredVerificationToken(ErrorResponse):
    message: str = Field(default="Invalid or expired verification token.")


# /api/user/reset-password/request
# Define response models (these would typically be in a separate responses module)
class PasswordResetRequestPostResponse(BaseResponse):
    message: str = Field(
        default="Password reset email sent successfully. Check you email for instructions."
    )


# /api/user/reset-password
class PasswordResetGetHTMLResponse(BaseResponse):
    message: str = Field(default="Password reset form displayed successfully.")
    _html: str = PrivateAttr()

    def __init__(self, token: str = None, success: bool = False, error: str = None):
        super().__init__()

        if success:
            self._html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Password Reset Successful</title>
                <style>
                    body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
                    .success { color: #4CAF50; text-align: center; }
                    .card { border: 1px solid #ddd; border-radius: 8px; padding: 30px; background: #f9f9f9; }
                </style>
            </head>
            <body>
                <div class="card">
                    <h2 class="success">Password Reset Successful!</h2>
                    <p>Your password has been updated successfully. You can now log in with your new password.</p>
                </div>
            </body>
            </html>
            """
        else:
            error_block = f'<p class="error">{error}</p>' if error else ""
            self._html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Reset Your Password</title>
                <style>
                    body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                    .form-group {{ margin-bottom: 15px; }}
                    label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
                    input[type="password"] {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
                    button {{ background-color: #4CAF50; color: white; padding: 12px 20px; border: none; border-radius: 4px; cursor: pointer; width: 100%; }}
                    button:hover {{ background-color: #45a049; }}
                    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 30px; background: #f9f9f9; }}
                    .error {{ color: #f44336; margin-bottom: 15px; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <h2>Reset Your Password</h2>
                    <form method="post" action="/api/user/reset-password/change">
                        <input type="hidden" name="token" value="{token}">

                        <div class="form-group">
                            <label for="new_password">New Password:</label>
                            <input type="password" id="new_password" name="new_password" required minlength="8">
                        </div>

                        <div class="form-group">
                            <label for="confirm_password">Confirm New Password:</label>
                            <input type="password" id="confirm_password" name="confirm_password" required minlength="8">
                        </div>

                        {error_block}

                        <button type="submit">Reset Password</button>
                    </form>
                </div>

                <script>
                    // Client-side password confirmation validation
                    document.querySelector('form').addEventListener('submit', function(e) {{
                        const newPassword = document.getElementById('new_password').value;
                        const confirmPassword = document.getElementById('confirm_password').value;

                        if (newPassword !== confirmPassword) {{
                            e.preventDefault();
                            alert('New password and confirmation do not match!');
                        }}
                    }});
                </script>
            </body>
            </html>
            """

    @property
    def html(self) -> str:
        return self._html


class InvalidOrExpiredResetToken(ErrorResponse):
    message: str = Field(default="Invalid or expired password reset token.")


class ErrorShowingPasswordResetForm(ErrorResponse):
    message: str = Field(default="Error showing password reset form.")


class PasswordResetError(ErrorResponse):
    message: str = Field(default="Server failed to reset the password.")


# /api/user/get
class GetUserError(ErrorResponse):
    message: str = Field(default="Server failed to retrieve user information.")


class GetUserGetResponse(BaseResponse):
    message: str = Field(default="User information retrieved successfully.")
    user: ResponseUser = Field(..., description="User details")
    config: str = Field(..., description="User's config HOCON string")
