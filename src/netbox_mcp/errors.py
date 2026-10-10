"""Exceptions whose messages are meant for the model. Subclassing ToolError makes FastMCP return
them as tool errors without logging a traceback."""

from __future__ import annotations

from fastmcp.exceptions import ToolError


class NetBoxError(ToolError):
    """An error talking to NetBox."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidRequest(ToolError):
    """The tool arguments were invalid (unknown object type, filter, path, ...)."""
