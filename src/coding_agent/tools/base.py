"""Tool interface, invocation context, and minimal input-schema validation.

The :class:`Tool` seam is executed only by the ``ToolExecutor`` (the single
chokepoint for side effects). Path arguments are resolved by the executor
(D6 steps 2 and 5) and handed to the tool via :class:`ToolInvocation`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from coding_agent.contracts import (
    ErrorCode,
    ToolDefinition,
    ToolError,
    ToolExecutionRequest,
    ToolResult,
    ToolResultStatus,
)
from coding_agent.workspace import Workspace

_JSON_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


@dataclass(frozen=True)
class ToolInvocation:
    """Everything a tool needs to run, prepared by the executor."""

    args: dict[str, Any]
    request: ToolExecutionRequest
    workspace: Workspace
    #: Resolved absolute paths for each entry in ``Tool.path_arguments``.
    paths: dict[str, str] = field(default_factory=dict)

    @property
    def tool_call_id(self) -> str:
        return self.request.tool_call_id


class Tool(ABC):
    """A v0.1 tool. ``execute`` runs only after policy + approval (D6)."""

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """The tool's stable definition (name, schema, side effect, permission)."""

    @property
    def path_arguments(self) -> tuple[str, ...]:
        """Argument keys that are workspace-relative paths to resolve/validate."""
        return ()

    @abstractmethod
    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        """Perform the operation and return a structured result."""


def validate_arguments(schema: dict[str, Any], args: Any) -> str | None:
    """Validate ``args`` against a tool ``input_schema``.

    Returns an error message string on failure, or ``None`` when valid. Supports
    the JSON-Schema subset used by the v0.1 tools: ``type=object``, ``properties``
    (scalar/array/object types), ``required``, and ``additionalProperties=false``.
    """
    if not isinstance(args, dict):
        return "arguments must be an object"

    schema_type = schema.get("type")
    if schema_type not in (None, "object"):
        return None  # non-object root schemas are not used by v0.1 tools

    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    additional_allowed = schema.get("additionalProperties", True)

    for key in required:
        if key not in args:
            return f"missing required argument: {key}"

    if additional_allowed is False:
        for key in args:
            if key not in properties:
                return f"unexpected argument: {key}"

    for key, value in args.items():
        prop_schema = properties.get(key)
        if not isinstance(prop_schema, dict):
            continue
        expected = prop_schema.get("type")
        checker = _JSON_TYPE_CHECKS.get(expected) if isinstance(expected, str) else None
        if checker is not None and not checker(value):
            return f"argument '{key}' must be of type {expected}"

    return None


def success_result(tool_call_id: str, output: Any, **metadata: Any) -> ToolResult:
    """Build a successful :class:`ToolResult`."""
    return ToolResult(
        tool_call_id=tool_call_id,
        status=ToolResultStatus.SUCCESS,
        output=output,
        metadata=dict(metadata),
    )


def error_result(
    tool_call_id: str,
    code: ErrorCode,
    message: str,
    *,
    status: ToolResultStatus = ToolResultStatus.FAILURE,
    retryable: bool = False,
    **details: Any,
) -> ToolResult:
    """Build a failed :class:`ToolResult` carrying a structured :class:`ToolError`."""
    return ToolResult(
        tool_call_id=tool_call_id,
        status=status,
        error=ToolError(code=code, message=message, retryable=retryable, details=dict(details)),
    )
