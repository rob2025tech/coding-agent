"""Exception hierarchy for the coding-agent runtime.

Tools generally *return* structured ``ToolResult`` objects rather than raising;
these exceptions are used for internal control flow that the executor catches
and converts into structured results (e.g. workspace escape).
"""

from __future__ import annotations

from coding_agent.contracts import ErrorCode


class AgentError(Exception):
    """Base class for all runtime errors."""


class ToolNotFoundError(AgentError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(f"unknown tool: {tool_name}")
        self.tool_name = tool_name
        self.code = ErrorCode.TOOL_NOT_FOUND


class PathOutsideWorkspaceError(AgentError):
    """Raised by ``Workspace.resolve`` when a path escapes the workspace."""

    def __init__(self, path: str) -> None:
        super().__init__(f"path escapes workspace: {path}")
        self.path = path
        self.code = ErrorCode.PATH_OUTSIDE_WORKSPACE


class LimitExceededError(AgentError):
    """Raised internally when a runtime limit is breached."""

    def __init__(self, limit: str) -> None:
        super().__init__(f"limit exceeded: {limit}")
        self.limit = limit
