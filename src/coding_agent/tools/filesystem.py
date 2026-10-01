"""File-system tools: ``list_files``, ``read_file``, ``edit_file``.

``edit_file`` implements the finalized contract (docs/contracts.md §38, D3/D17):
exact-unique match, UTF-8 only, preserve line endings + trailing newline, atomic
write (temp + rename), and a short unified diff in the output.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from typing import Any

from coding_agent.contracts import (
    ErrorCode,
    PermissionClass,
    SideEffect,
    ToolDefinition,
    ToolResult,
)
from coding_agent.tools.base import Tool, ToolInvocation, error_result, success_result


def _atomic_write(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + rename), preserving mode."""
    directory = os.path.dirname(path) or "."
    try:
        mode = os.stat(path).st_mode
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".edit_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ListFilesTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_files",
            description="List files and directories at a path within the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
            side_effect=SideEffect.NONE,
            permission=PermissionClass.READ,
        )

    @property
    def path_arguments(self) -> tuple[str, ...]:
        return ("path",)

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        display = str(invocation.args.get("path", "."))
        target = invocation.paths.get("path") or invocation.workspace.repo_root
        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    entries.append(
                        {"name": entry.name, "type": "directory" if entry.is_dir() else "file"}
                    )
        except FileNotFoundError:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"no such directory: {display}"
            )
        except NotADirectoryError:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"not a directory: {display}"
            )
        entries.sort(key=lambda item: (item["type"] != "directory", item["name"]))
        return success_result(invocation.tool_call_id, {"path": display, "entries": entries})


class ReadFileTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file within the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            side_effect=SideEffect.NONE,
            permission=PermissionClass.READ,
        )

    @property
    def path_arguments(self) -> tuple[str, ...]:
        return ("path",)

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        display = str(invocation.args["path"])
        path = invocation.paths["path"]
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EXECUTION_FAILED,
                f"file does not exist: {display}",
            )
        except IsADirectoryError:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"is a directory: {display}"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EXECUTION_FAILED,
                f"file is not valid UTF-8: {display}",
            )
        return success_result(invocation.tool_call_id, {"path": display, "content": text})


class EditFileTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        # Verbatim from docs/contracts.md §38.
        return ToolDefinition(
            name="edit_file",
            description=(
                "Replace an exact, unique substring in an existing UTF-8 text file "
                "within the workspace."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            side_effect=SideEffect.FILESYSTEM_WRITE,
            permission=PermissionClass.WRITE,
        )

    @property
    def path_arguments(self) -> tuple[str, ...]:
        return ("path",)

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        display = str(invocation.args["path"])
        path = invocation.paths["path"]
        old_text = str(invocation.args["old_text"])
        new_text = str(invocation.args["new_text"])

        if old_text == "":
            return error_result(
                invocation.tool_call_id, ErrorCode.INVALID_ARGUMENTS, "old_text must be non-empty"
            )

        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EXECUTION_FAILED,
                f"file does not exist: {display}",
            )
        except IsADirectoryError:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"is a directory: {display}"
            )

        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EDIT_NOT_UTF8,
                f"file is not valid UTF-8: {display}",
            )

        occurrences = content.count(old_text)
        if occurrences == 0:
            return error_result(
                invocation.tool_call_id, ErrorCode.EDIT_NO_MATCH, "old_text not found in file"
            )
        if occurrences > 1:
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EDIT_AMBIGUOUS,
                f"old_text matches {occurrences} times; it must be unique",
            )

        # Single, exact replacement on the decoded text preserves line endings and
        # trailing-newline state (no split/rejoin of the file body).
        new_content = content.replace(old_text, new_text, 1)
        diff = "\n".join(
            difflib.unified_diff(
                content.splitlines(),
                new_content.splitlines(),
                fromfile=display,
                tofile=display,
                lineterm="",
            )
        )

        try:
            _atomic_write(path, new_content.encode("utf-8"))
        except OSError as exc:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"write failed: {exc}"
            )

        return success_result(invocation.tool_call_id, {"path": display, "diff": diff})
