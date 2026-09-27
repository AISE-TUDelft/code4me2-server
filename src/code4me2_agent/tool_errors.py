"""Typed, model-facing tool failures.

Every ``ToolError`` carries a stable ``code`` that the agent loop copies into the
tool result as ``error_code`` so the model can react to the failure class, while
``str(exc)`` is the human/model readable message. Errors that correspond to a
builtin also inherit from it so existing ``except PermissionError`` /
``except FileNotFoundError`` sites keep working.
"""

from __future__ import annotations

from typing import Any


class ToolError(RuntimeError):
    code: str = "tool_error"

    def __init__(self, message: str, *, code: str | None = None, **details: Any) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details)


class ToolArgumentError(ToolError):
    """A tool call carried a missing, mistyped or out-of-range argument."""

    code = "invalid_arguments"

    def __init__(self, message: str, *, field: str | None = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.field = field


class WorkspaceBoundaryError(ToolError, PermissionError):
    code = "outside_workspace"


class ToolFileNotFoundError(ToolError, FileNotFoundError):
    code = "file_not_found"


class ToolFileExistsError(ToolError, FileExistsError):
    code = "file_exists"


class NotTextFileError(ToolError):
    code = "not_text_file"


class FileTooLargeError(ToolError):
    code = "file_too_large"


class DirectoryNotEmptyError(ToolError):
    code = "directory_not_empty"


class EditMatchError(ToolError):
    """``old_text`` matched zero times (``edit_no_match``) or several (``edit_ambiguous``)."""

    code = "edit_no_match"

    def __init__(
        self,
        message: str,
        *,
        code: str,
        match_count: int,
        edit_index: int = 0,
        edit_count: int = 1,
    ) -> None:
        super().__init__(
            message,
            code=code,
            match_count=match_count,
            edit_index=edit_index,
            edit_count=edit_count,
        )
        self.match_count = match_count
        self.edit_index = edit_index
        self.edit_count = edit_count


class CommandNotFoundError(ToolError):
    code = "command_not_found"
