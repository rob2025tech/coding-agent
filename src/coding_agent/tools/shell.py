"""``shell`` tool: tokenize with shlex, execute with ``shell=False`` (D5).

Authorization happens in the executor (policy + approver) *before* this runs.
The command is never passed to a shell: it is split with :func:`shlex.split` and
executed via ``create_subprocess_exec`` (argv form), pinned to ``repo_root``.
"""

from __future__ import annotations

import asyncio
import shlex

from coding_agent.contracts import (
    ErrorCode,
    PermissionClass,
    SideEffect,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from coding_agent.tools.base import Tool, ToolInvocation, error_result, success_result


class ShellTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shell",
            description=(
                "Run a shell command (argv form, no shell) in the workspace root. "
                "Read-only git commands are pre-allowed; everything else needs approval."
            ),
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            side_effect=SideEffect.PROCESS_EXECUTION,
            permission=PermissionClass.EXECUTE,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        command = str(invocation.args["command"])
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"unparseable command: {exc}"
            )
        if not argv:
            return error_result(
                invocation.tool_call_id, ErrorCode.INVALID_ARGUMENTS, "empty command"
            )

        timeout_ms = invocation.request.timeout_ms
        timeout_s = timeout_ms / 1000.0 if timeout_ms else None
        cwd = invocation.workspace.repo_root

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EXECUTION_FAILED,
                f"program not found: {argv[0]}",
            )
        except OSError as exc:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"execution failed: {exc}"
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return error_result(
                invocation.tool_call_id,
                ErrorCode.TIMEOUT,
                f"command timed out after {timeout_s}s",
                status=ToolResultStatus.TIMEOUT,
            )

        output = {
            "command": command,
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
        }
        if proc.returncode == 0:
            return success_result(invocation.tool_call_id, output)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            status=ToolResultStatus.FAILURE,
            output=output,
            error=ToolError(
                code=ErrorCode.EXECUTION_FAILED,
                message=f"command exited with code {proc.returncode}",
            ),
        )
