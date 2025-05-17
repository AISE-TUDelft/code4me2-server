import logging
from copy import copy

from fastapi import APIRouter, Cookie, Depends

from App import App
from backend.Responses import (
    ErrorResponse,
    InvalidSessionToken,
    JsonResponseWithStatus,
    MultiFileContextUpdateError,
    MultiFileContextUpdatePostResponse,
)
from Queries import UpdateMultiFileContext

router = APIRouter()


@router.post(
    "/",
    response_model=MultiFileContextUpdatePostResponse,
    responses={
        "200": {"model": MultiFileContextUpdatePostResponse},
        "401": {"model": InvalidSessionToken},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": MultiFileContextUpdateError},
    },
)
def update_multi_file_context(
    context_update: UpdateMultiFileContext,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Update the context for a specific query ID.
    """
    logging.log(logging.INFO, f"Updating context for session: {session_token}")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()

    try:
        # Check if user is authenticated
        user_dict = session_manager.get_session(session_token)
        if session_token is None or user_dict is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidSessionToken(),
            )

        # Update the context with the new content
        # TODO: if a big file changes frequently, we should consider updating it in a different way
        existing_context = user_dict["data"].get("context", {})
        updated_context = existing_context | context_update.context_updates

        # Remove the files that their new content are empty
        for file in copy(list(updated_context.keys())):
            if not updated_context[file]:
                logging.log(logging.WARNING, f"Removing {file} context")
                del updated_context[file]

        # Store the updated context in the session
        user_dict["data"]["context"] = updated_context
        session_manager.update_session(session_token, user_dict)

        return JsonResponseWithStatus(
            status_code=200,
            content=MultiFileContextUpdatePostResponse(
                data=updated_context,
            ),
        )
    except Exception as e:
        logging.log(logging.ERROR, f"Error updating context: {e}")
        return JsonResponseWithStatus(
            status_code=500, content=MultiFileContextUpdateError()
        )
