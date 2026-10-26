"""``shell`` tool: tokenize with shlex, execute with ``shell=False`` (D5).

Authorization happens in the executor (policy + approver) *before* this runs.
The command is never passed to a shell: it is split with :func:`shlex.split` and
executed via ``create_subprocess_exec`` (argv form), pinned to ``repo_root``.

Completion follows the direct child, not the output pipes: when it exits, the
tool drains stdout/stderr for a bounded grace period and returns its exit status
with the output captured so far. A descendant that inherits the pipes can
therefore truncate output, but cannot turn a finished command into a timeout.

The tool owns its direct subprocess: on timeout, cancellation,
``KeyboardInterrupt``, or an unexpected exception it kills the direct child and
bounds the reap wait. Descendants are not terminated.
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

_CLEANUP_GRACE_S = 0.5
# After the direct child exits, inherited pipe ends may stay open. Capture output
# for this long, then drop the readers; anything written later is truncated.
_DRAIN_GRACE_S = 0.5
# ``Process.wait()`` cannot report the exit while pipes are open, so exit is
# observed by polling the returncode.
_EXIT_POLL_INTERVAL_S = 0.01
_READ_CHUNK_SIZE = 65536


async def _wait_for_exit(proc: asyncio.subprocess.Process) -> None:
    """Wait for the direct child to exit, independently of its output pipes.

    asyncio resolves ``Process.wait()`` only after every pipe reaches EOF, so a
    descendant holding stdout/stderr open would hide the direct child's exit.
    ``returncode`` is set as soon as the child is reaped.
    """
    while proc.returncode is None:
        await asyncio.sleep(_EXIT_POLL_INTERVAL_S)


async def _drain_into(stream: asyncio.StreamReader, buffer: bytearray) -> None:
    """Read ``stream`` to EOF into ``buffer``, keeping earlier chunks on cancel."""
    while chunk := await stream.read(_READ_CHUNK_SIZE):
        buffer.extend(chunk)


async def _observe_readers(readers: list[asyncio.Task[None]]) -> None:
    """Wait for cancelled readers; a cancellation propagates only after they finish.

    Shielding the wait keeps further cancellations from abandoning the readers,
    which would otherwise be left pending and unobserved when cleanup returns.
    """
    combined = asyncio.gather(*readers, return_exceptions=True)
    cancellation: asyncio.CancelledError | None = None
    while not combined.done():
        try:
            await asyncio.shield(combined)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        raise cancellation


async def _release_readers(
    readers: list[asyncio.Task[None]], transport: asyncio.BaseTransport
) -> None:
    """Cancel the readers and close the pipe transports, even under cancellation.

    Output not yet copied into the buffers is truncated. Closing the transports
    is required because a descendant holding the inherited pipe ends would
    otherwise leave them open indefinitely.
    """
    for reader in readers:
        reader.cancel()
    try:
        await _observe_readers(readers)
    finally:
        if not transport.is_closing():
            transport.close()


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill the direct child and bound the reap wait.

    The bound keeps pipe-holding descendants from blocking cleanup. Ordinary
    cleanup failures are suppressed. A cancellation delivered during the bounded
    wait is not suppressed; it propagates and supersedes the exception being
    handled, after the child has been killed.
    """
    try:
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_GRACE_S)
    except Exception:
        # Do not let ordinary cleanup errors replace the exception being propagated.
        pass


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

        assert proc.stdout is not None
        assert proc.stderr is not None
        # ``asyncio.subprocess.Process`` exposes no public handle for its pipe
        # transports. Retain it up front so cleanup can release pipes that a
        # descendant keeps open; every supported version stores it here.
        transport: asyncio.BaseTransport = proc._transport  # type: ignore[attr-defined]
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        readers = [
            asyncio.create_task(_drain_into(proc.stdout, stdout_buf)),
            asyncio.create_task(_drain_into(proc.stderr, stderr_buf)),
        ]

        try:
            await asyncio.wait_for(_wait_for_exit(proc), timeout=timeout_s)
        except TimeoutError:
            try:
                await _terminate_process(proc)
            finally:
                await _release_readers(readers, transport)
            return error_result(
                invocation.tool_call_id,
                ErrorCode.TIMEOUT,
                f"command timed out after {timeout_s}s",
                status=ToolResultStatus.TIMEOUT,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, Exception):
            try:
                await _terminate_process(proc)
            finally:
                await _release_readers(readers, transport)
            raise

        try:
            await asyncio.wait_for(asyncio.gather(*readers), timeout=_DRAIN_GRACE_S)
        except TimeoutError:
            pass
        finally:
            await _release_readers(readers, transport)

        output = {
            "command": command,
            "stdout": stdout_buf.decode("utf-8", errors="replace"),
            "stderr": stderr_buf.decode("utf-8", errors="replace"),
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
