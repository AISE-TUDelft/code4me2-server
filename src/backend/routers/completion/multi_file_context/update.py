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
    Update the multi-file context content in a session by applying line-level diffs.

    Args:
        existing_context: Dictionary mapping filenames to their content as lists of lines.
        context_update: Dictionary mapping filenames to lists of FileContextChangeData,
                        representing diffs (insert, update, remove) at line granularity.

    Returns:
        A new dictionary representing the updated file contexts after applying all diffs.

    Notes:
        - Changes are applied in reverse order by start line to avoid offset issues.
        - Files with no content after updates are removed from the context.
    """
    # Deep copy to avoid mutating the original context
    updated_contexts = deepcopy(existing_context)

    for file, context_changes in context_update.items():
        # Sort changes descending by start line so applying does not affect later line indices
        context_changes = sorted(
            context_changes, key=lambda c: c.start_line, reverse=True
        )

        if file not in updated_contexts:
            # For new files, create an empty context list with length to cover updates/removes
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
            # Existing file content - make a shallow copy of lines to update
            updated_context = updated_contexts[file][:]

        for change in context_changes:
            if change.change_type == ContextChangeType.update:
                # Replace lines in the specified range
                updated_context[change.start_line : change.end_line] = change.new_lines
            elif change.change_type == ContextChangeType.insert:
                # Insert new lines at start_line
                updated_context[change.start_line : change.start_line] = (
                    change.new_lines
                )
            elif change.change_type == ContextChangeType.remove:
                # Remove lines in the specified range (clamped to length)
                del updated_context[
                    change.start_line : min(change.end_line, len(updated_context))
                ]

        if updated_context:
            updated_contexts[file] = updated_context
        else:
            # Remove file if its content is empty after updates
            del updated_contexts[file]

    return updated_contexts


def update_multi_file_context_changes_in_session(
    existing_context_changes: Dict[str, List[Dict[str, str]]],
    context_updates: Dict[str, List[FileContextChangeData]],
) -> Dict[str, List[Dict[str, str]]]:
    """
    Append new context changes to the existing context change log per file.

    Args:
        existing_context_changes: Dictionary mapping filenames to lists of previous change dicts.
        context_updates: Dictionary mapping filenames to lists of new FileContextChangeData.

    Returns:
        The updated context changes dictionary with appended change dicts.
    """
    for file, context_changes in context_updates.items():
        # Append new changes serialized as dicts to the existing list
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
    Endpoint to update multi-file context content for a given project and session.

    Steps:
    - Validate session token and retrieve session info from Redis.
    - Validate project token and retrieve project info from Redis.
    - Apply line-level context updates to the existing multi-file contexts.
    - Redact secrets in the updated content lines.
    - Remove any files with empty content.
    - Update the context change logs accordingly.
    - Store the updated project info back in Redis.
    - Return the updated context in the response.

    Args:
        context_update: UpdateMultiFileContext model containing the changes to apply.
        app: The application instance (dependency-injected).
        session_token: Session token from cookies.
        project_token: Project token from cookies.

    Returns:
        JsonResponseWithStatus containing the updated multi-file context or an error response.
    """
    logging.info(f"Updating context for session: {session_token}")
    redis_manager = app.get_redis_manager()

    try:
        # Validate session token and get session info
        session_info = redis_manager.get("session_token", session_token)
        if session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Validate auth token and user_id from session info
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

        # Validate project token and get project info
        project_info = redis_manager.get("project_token", project_token)
        if project_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Retrieve existing multi-file contexts and apply updates
        existing_context = project_info.get("multi_file_contexts", {})
        updated_context = update_multi_file_context_in_session(
            existing_context, context_update.context_updates
        )

        # Redact any secrets in the updated content line-by-line
        for file_name, lines in updated_context.items():
            redacted_lines = []
            secrets = extract_secrets(text="\n".join(lines), file_name=file_name)
            for line_number, line in enumerate(lines, start=1):
                line = redact_secrets(line, secrets)
                redacted_lines.append(line)
            updated_context[file_name] = redacted_lines

        # Remove files that became empty after updates
        for file in copy(list(updated_context.keys())):
            if not updated_context[file]:
                logging.warning(f"Removing {file} context")
                del updated_context[file]

        # Store updated contexts back into project info
        project_info["multi_file_contexts"] = updated_context

        # Append the new context changes to the existing change log
        existing_context_changes = project_info.get("multi_file_context_changes", {})
        updated_context_changes = update_multi_file_context_changes_in_session(
            existing_context_changes, context_update.context_updates
        )
        project_info["multi_file_context_changes"] = updated_context_changes

        # Save updated project info in Redis
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
