"""ToolExecutor: the single chokepoint for side effects (docs/contracts.md §17, D6).

Execution order (D6):
  1. schema validation
  2. workspace resolve (file tools)
  3. policy decision
  4. approval (if ASK)
  5. re-validate path at execution time (symlink / TOCTOU)
  6. timeout-bounded run
  7. structured ToolResult
  8. events

DENY is final and never reaches the approver; ``--yes`` can only auto-approve ASK.
After a run the executor tags ``counts_as_write`` (D12) so the runtime can decide
verification staleness.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from coding_agent.contracts import (
    ErrorCode,
    PermissionDecision,
    PermissionDecisionValue,
    ToolCall,
    ToolError,
    ToolExecutionRequest,
    ToolResult,
    ToolResultStatus,
)
from coding_agent.errors import PathOutsideWorkspaceError
from coding_agent.events import AgentEvent, EventSink, EventType
from coding_agent.permissions import Approver, PermissionPolicy, build_permission_request
from coding_agent.tools.base import Tool, ToolInvocation, validate_arguments
from coding_agent.tools.registry import ToolRegistry
from coding_agent.workspace import Workspace

DEFAULT_TOOL_TIMEOUT_MS = 30_000


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        workspace: Workspace,
        policy: PermissionPolicy,
        approver: Approver,
        event_sink: EventSink,
        default_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS,
    ) -> None:
        self._registry = registry
        self._workspace = workspace
        self._policy = policy
        self._approver = approver
        self._event_sink = event_sink
        self._default_timeout_ms = default_timeout_ms

    async def execute(
        self, call: ToolCall, *, session_id: str, turn_id: str | None = None
    ) -> ToolResult:
        # 0. resolve the tool
        tool = self._registry.get(call.name)
        if tool is None:
            self._emit(EventType.TOOL_FAILED, session_id, turn_id, tool_name=call.name,
                       code=ErrorCode.TOOL_NOT_FOUND.value)
            return self._error(
                call.tool_call_id, ErrorCode.TOOL_NOT_FOUND, f"unknown tool: {call.name}"
            )

        definition = tool.definition

        # 1. schema validation
        schema_error = validate_arguments(definition.input_schema, call.arguments)
        if schema_error is not None:
            self._emit(EventType.TOOL_FAILED, session_id, turn_id, tool_name=call.name,
                       code=ErrorCode.INVALID_ARGUMENTS.value)
            return self._error(call.tool_call_id, ErrorCode.INVALID_ARGUMENTS, schema_error)

        # 2. workspace resolve (file tools)
        try:
            paths = self._resolve_paths(tool, call.arguments)
        except PathOutsideWorkspaceError as exc:
            self._emit(EventType.TOOL_FAILED, session_id, turn_id, tool_name=call.name,
                       code=ErrorCode.PATH_OUTSIDE_WORKSPACE.value)
            return self._error(
                call.tool_call_id, ErrorCode.PATH_OUTSIDE_WORKSPACE, str(exc), path=exc.path
            )

        # 3. policy decision
        decision = self._policy.check(definition, call)

        # 4. approval (DENY is final; ASK consults the approver; ALLOW is granted)
        if decision.decision is PermissionDecisionValue.DENY:
            self._emit(EventType.PERMISSION_DENIED, session_id, turn_id, tool_name=call.name,
                       reason=decision.reason)
            return self._error(
                call.tool_call_id,
                ErrorCode.PERMISSION_DENIED,
                decision.reason or "denied by policy",
                status=ToolResultStatus.DENIED,
            )
        if decision.decision is PermissionDecisionValue.ASK:
            request = build_permission_request(
                definition, call, decision, f"perm_{uuid.uuid4().hex[:8]}"
            )
            self._emit(EventType.PERMISSION_REQUESTED, session_id, turn_id, tool_name=call.name,
                       permission_class=definition.permission.value, reason=request.reason)
            if not self._approver.confirm(request):
                self._emit(EventType.PERMISSION_DENIED, session_id, turn_id, tool_name=call.name,
                           reason="denied by approver")
                return self._error(
                    call.tool_call_id,
                    ErrorCode.PERMISSION_DENIED,
                    "denied by approver",
                    status=ToolResultStatus.DENIED,
                )
            self._emit(EventType.PERMISSION_GRANTED, session_id, turn_id, tool_name=call.name,
                       reason="approved by approver")
        else:
            self._emit(EventType.PERMISSION_GRANTED, session_id, turn_id, tool_name=call.name,
                       reason=decision.reason or "allowed by policy")

        # 5. re-validate path at execution time (symlink / TOCTOU)
        try:
            paths = self._resolve_paths(tool, call.arguments)
        except PathOutsideWorkspaceError as exc:
            self._emit(EventType.TOOL_FAILED, session_id, turn_id, tool_name=call.name,
                       code=ErrorCode.PATH_OUTSIDE_WORKSPACE.value)
            return self._error(
                call.tool_call_id, ErrorCode.PATH_OUTSIDE_WORKSPACE, str(exc), path=exc.path
            )

        # 6. timeout-bounded run
        execution_request = ToolExecutionRequest(
            execution_id=f"exec_{uuid.uuid4().hex[:8]}",
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            arguments=dict(call.arguments),
            workspace=self._workspace.repo_root,
            timeout_ms=self._default_timeout_ms,
        )
        invocation = ToolInvocation(
            args=dict(call.arguments),
            request=execution_request,
            workspace=self._workspace,
            paths=paths,
        )
        self._emit(EventType.TOOL_STARTED, session_id, turn_id, tool_name=call.name,
                   execution_id=execution_request.execution_id)
        timeout_s = execution_request.timeout_ms / 1000.0
        try:
            result = await asyncio.wait_for(tool.execute(invocation), timeout=timeout_s)
        except TimeoutError:
            self._emit(EventType.TOOL_FAILED, session_id, turn_id, tool_name=call.name,
                       code=ErrorCode.TIMEOUT.value)
            return self._error(
                call.tool_call_id,
                ErrorCode.TIMEOUT,
                f"tool timed out after {timeout_s}s",
                status=ToolResultStatus.TIMEOUT,
            )
        except Exception as exc:  # convert any unexpected tool bug into a structured result
            self._emit(EventType.TOOL_FAILED, session_id, turn_id, tool_name=call.name,
                       code=ErrorCode.INTERNAL_ERROR.value)
            return self._error(call.tool_call_id, ErrorCode.INTERNAL_ERROR, f"tool error: {exc}")

        # 7. structured result + D12 write tagging
        result.metadata["counts_as_write"] = self._counts_as_write(call.name, decision, result)

        # 8. events
        if result.status is ToolResultStatus.SUCCESS:
            self._emit(EventType.TOOL_COMPLETED, session_id, turn_id, tool_name=call.name)
        else:
            code = result.error.code.value if result.error else result.status.value
            self._emit(EventType.TOOL_FAILED, session_id, turn_id, tool_name=call.name, code=code)
        return result

    # -- helpers ------------------------------------------------------------- #

    def _resolve_paths(self, tool: Tool, arguments: dict[str, Any]) -> dict[str, str]:
        paths: dict[str, str] = {}
        for key in tool.path_arguments:
            value = arguments.get(key)
            if value is not None:
                paths[key] = self._workspace.resolve(str(value))
        return paths

    @staticmethod
    def _counts_as_write(
        tool_name: str, decision: PermissionDecision, result: ToolResult
    ) -> bool:
        """D12: a write is a successful edit_file OR a non-allowlisted shell run."""
        ran = result.status not in (ToolResultStatus.DENIED, ToolResultStatus.CANCELLED)
        if tool_name == "edit_file":
            return result.status is ToolResultStatus.SUCCESS
        if tool_name == "shell":
            # ALLOW == the read-only allowlist; anything else that ran is a potential write.
            return ran and decision.decision is not PermissionDecisionValue.ALLOW
        return False

    @staticmethod
    def _error(
        tool_call_id: str,
        code: ErrorCode,
        message: str,
        *,
        status: ToolResultStatus = ToolResultStatus.FAILURE,
        **details: Any,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            status=status,
            error=ToolError(code=code, message=message, details=dict(details)),
        )

    def _emit(
        self,
        event_type: EventType,
        session_id: str,
        turn_id: str | None,
        **payload: Any,
    ) -> None:
        self._event_sink.emit(
            AgentEvent(type=event_type, session_id=session_id, turn_id=turn_id, payload=payload)
        )
