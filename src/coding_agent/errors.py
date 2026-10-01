"""Exception hierarchy for the coding-agent runtime.

Tools generally *return* structured ``ToolResult`` objects rather than raising;
these exceptions are used for internal control flow that the executor catches
and converts into structured results (e.g. workspace escape).
"""

from __future__ import annotations

from dataclasses import replace

from coding_agent.contracts import ErrorCode, ProviderError, ProviderErrorCode


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


class ProviderExecutionError(AgentError):
    """A provider failure carrying an already-normalized §25 :class:`ProviderError`.

    Concrete providers raise this so the core runtime never depends on
    provider-specific exception classes (module-boundary / no-SDK-leakage rule).
    """

    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.message)
        self.error = error


def normalize_provider_failure(exc: BaseException, *, provider_id: str = "") -> ProviderError:
    """Normalize any provider-boundary exception into a §25 :class:`ProviderError`.

    * An already-normalized :class:`ProviderExecutionError` is preserved (its
      ``provider_id`` filled in when missing).
    * Any other exception becomes ``PROVIDER_ERROR``, recording the original
      exception *type name* in ``metadata`` — never the class itself, so no
      provider-specific type leaks into the core runtime.

    v0.1 performs no retries; ``retryable`` / ``retry_after_ms`` are advisory.
    """
    if isinstance(exc, ProviderExecutionError):
        error = exc.error
        return error if error.provider_id else replace(error, provider_id=provider_id)
    return ProviderError(
        code=ProviderErrorCode.PROVIDER_ERROR,
        message=str(exc) or type(exc).__name__,
        retryable=False,
        provider_id=provider_id,
        metadata={"exception_type": type(exc).__name__},
    )
