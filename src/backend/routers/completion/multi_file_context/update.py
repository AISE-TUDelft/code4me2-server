import logging
from copy import copy, deepcopy
from typing import Dict, List, Union

from fastapi import APIRouter, Cookie, Depends

from App import App
from backend.Responses import (
    ErrorResponse,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    MultiFileContextUpdateError,
    MultiFileContextUpdatePostResponse,
)
from Queries import ContextChangeType, FileContextChangeData, UpdateMultiFileContext
from utils import extract_secrets, redact_secrets

router = APIRouter()


def update_multi_file_context_in_session(
    existing_context: Dict[str, List[str]],
    context_update: Dict[str, List[FileContextChangeData]],
) -> Dict[str, List[str]]:
    """
    Update the context with the new content, given per-file line-level diffs.

    Parameters:
        existing_context: A mapping from file name to its content (list of lines).
        context_update: A mapping from file name to list of context change operations.

    Returns:
        A new context dict with the updates applied.
    """
    updated_contexts = deepcopy(existing_context)
    for file, context_changes in context_update.items():
        # Ensure updates don't affect later indices
        context_changes = sorted(
            context_changes, key=lambda c: c.start_line, reverse=True
        )
        if file not in updated_contexts:
            # If the file is new, create an empty list with sufficient length
            max_lines = max(
                map(
                    lambda x: x.end_line,
                    filter(
                        lambda c: c.change_type != ContextChangeType.insert,
                        context_changes,
                    ),
                ),
                default=0,
            )
            updated_context = [""] * max_lines
        else:
            updated_context = updated_contexts[file][:]  # shallow copy of lines
        for change in context_changes:
            if change.change_type == ContextChangeType.update:
                updated_context[change.start_line : change.end_line] = change.new_lines
            elif change.change_type == ContextChangeType.insert:
                updated_context[change.start_line : change.start_line] = (
                    change.new_lines
                )
            elif change.change_type == ContextChangeType.remove:
                del updated_context[
                    change.start_line : min(change.end_line, len(updated_context))
                ]
        if updated_context:
            updated_contexts[file] = updated_context
        else:
            del updated_contexts[file]  # Remove empty files
    return updated_contexts


def update_multi_file_context_changes_in_session(
    existing_context_changes: Dict[str, List[Dict[str, str]]],
    context_updates: Dict[str, List[FileContextChangeData]],
) -> Dict[str, List[Dict[str, str]]]:
    for file, context_changes in context_updates.items():
        existing_context_changes.setdefault(file, []).extend(
            [context_change.dict() for context_change in context_changes]
        )
    return existing_context_changes


@router.post(
    "",
    response_model=MultiFileContextUpdatePostResponse,
    responses={
        "200": {"model": MultiFileContextUpdatePostResponse},
        "401": {
            "model": Union[InvalidOrExpiredSessionToken, InvalidOrExpiredProjectToken]
        },
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": MultiFileContextUpdateError},
    },
)
def update_multi_file_context(
    context_update: UpdateMultiFileContext,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie(""),
    project_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Update the context for a specific query ID.
    """
    logging.info(f"Updating context for session: {session_token}")
    redis_manager = app.get_redis_manager()

    try:
        # Get session info from Redis
        session_info = redis_manager.get("session_token", session_token)
        if session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Get auth token from session info and then get user_id from auth token
        auth_token = session_info.get("auth_token")
        if not auth_token:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        auth_info = redis_manager.get("auth_token", auth_token)
        if auth_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        user_id = auth_info.get("user_id")
        if not user_id:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Validate project token
        project_info = redis_manager.get("project_token", project_token)
        if project_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Update the context with the new content
        existing_context = project_info.get("multi_file_contexts", {})
        updated_context = update_multi_file_context_in_session(
            existing_context, context_update.context_updates
        )

        # Redact secrets
        for file_name, lines in updated_context.items():
            redacted_lines = []
            secrets = extract_secrets(text="\n".join(lines), file_name=file_name)
            for line_number, line in enumerate(lines, start=1):
                line = redact_secrets(line, secrets)
                redacted_lines.append(line)
            updated_context[file_name] = redacted_lines
        # Remove the files that their new content are empty
        for file in copy(list(updated_context.keys())):
            if not updated_context[file]:
                logging.warning(f"Removing {file} context")
                del updated_context[file]

        # Store the updated context in the project_info
        project_info["multi_file_contexts"] = updated_context

        existing_context_changes = project_info.get("multi_file_context_changes", {})
        updated_context_changes = update_multi_file_context_changes_in_session(
            existing_context_changes, context_update.context_updates
        )
        project_info["multi_file_context_changes"] = updated_context_changes

        redis_manager.set("project_token", project_token, project_info)

        return JsonResponseWithStatus(
            status_code=200,
            content=MultiFileContextUpdatePostResponse(
                data=updated_context,
            ),
        )
    except Exception as e:
        logging.error(f"Error updating context: {e}")
        return JsonResponseWithStatus(
            status_code=500, content=MultiFileContextUpdateError()
        )
