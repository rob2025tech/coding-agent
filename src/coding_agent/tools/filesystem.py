"""File-system tools: ``list_files``, ``read_file``, ``edit_file``, ``write_file``.

``edit_file`` implements the finalized contract (docs/contracts.md §38, D3/D17):
exact-unique match, UTF-8 only, preserve line endings + trailing newline, atomic
write (temp + rename), and a short unified diff in the output.

``write_file`` (v0.2; docs/contracts.md §41, D20) atomically creates a new UTF-8
file or replaces an existing one with the exact bytes of ``content``. It reuses
``_atomic_write`` and never creates missing parent directories implicitly (a
missing parent is a structured ``EXECUTION_FAILED``).
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


class WriteFileTool(Tool):
    """Atomically create or replace a whole UTF-8 file (v0.2; contracts §41, D20).

    Unlike ``edit_file`` (surgical, exact-unique replacement in an EXISTING file),
    ``write_file`` writes the entire file: creating it when absent, or replacing it
    atomically when present. Missing parent directories are NOT created implicitly
    (D20); a missing parent yields a structured ``EXECUTION_FAILED``. Path
    resolution/containment and the ASK gate are enforced by the executor (D6); a
    successful write is a D12 write that invalidates a passing verification.
    """

    @property
    def definition(self) -> ToolDefinition:
        # Verbatim from docs/contracts.md §41.
        return ToolDefinition(
            name="write_file",
            description=(
                "Create a new UTF-8 text file, or atomically replace an existing one, "
                "with exactly `content`. The parent directory must already exist "
                "(missing directories are not created). Path must stay in the workspace."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
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
        content = invocation.args["content"]
        if not isinstance(content, str):
            return error_result(
                invocation.tool_call_id, ErrorCode.INVALID_ARGUMENTS, "content must be a string"
            )

        # D20: never create missing parent directories implicitly. ``_atomic_write``
        # uses ``mkstemp(dir=parent)``, which requires the parent to exist; check it
        # explicitly so a missing parent is a structured EXECUTION_FAILED, not a crash.
        directory = os.path.dirname(path) or "."
        if not os.path.isdir(directory):
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EXECUTION_FAILED,
                f"parent directory does not exist: {display}",
            )

        created = not os.path.exists(path)
        try:
            _atomic_write(path, content.encode("utf-8"))
        except OSError as exc:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"write failed: {exc}"
            )

        return success_result(
            invocation.tool_call_id,
            {"path": display, "created": created, "bytes_written": len(content.encode("utf-8"))},
        )


class DeleteFileTool(Tool):
    """Delete a single existing file (v0.2; contracts §43, D25).

    Files only — a directory or a missing target yields a structured
    ``EXECUTION_FAILED``; recursive deletion is out of scope (D25). An
    in-workspace symlink is removed as a link, never through to its target.
    Path resolution/containment (including the §17 step-5 execution-time
    re-check) and the ASK gate are enforced by the executor (D6); a successful
    delete is a D12 write that invalidates a passing verification.
    """

    @property
    def definition(self) -> ToolDefinition:
        # Verbatim from docs/contracts.md §43.
        return ToolDefinition(
            name="delete_file",
            description="Delete a file from the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            side_effect=SideEffect.DESTRUCTIVE,
            permission=PermissionClass.DESTRUCTIVE,
        )

    @property
    def path_arguments(self) -> tuple[str, ...]:
        return ("path",)

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        display = str(invocation.args["path"])
        path = invocation.paths["path"]
        # lexists (not exists): a dangling in-workspace symlink still exists
        # as a link and is deletable; only a truly absent path fails.
        if not os.path.lexists(path):
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EXECUTION_FAILED,
                f"file does not exist: {display}",
            )
        # files only: a real directory is never deleted (D25). An in-workspace
        # symlink is removed as the link itself, even if it points at a file.
        if not os.path.islink(path) and os.path.isdir(path):
            return error_result(
                invocation.tool_call_id,
                ErrorCode.EXECUTION_FAILED,
                f"is a directory; delete_file removes files only: {display}",
            )
        try:
            os.remove(path)
        except OSError as exc:
            return error_result(
                invocation.tool_call_id, ErrorCode.EXECUTION_FAILED, f"delete failed: {exc}"
            )

        return success_result(invocation.tool_call_id, {"path": display})
