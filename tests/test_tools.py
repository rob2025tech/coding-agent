"""Tool behavior tests (docs/contracts.md §28, §38, §20; D3/D5/D17)."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from coding_agent.contracts import ErrorCode, ToolResult, ToolResultStatus
from coding_agent.tools.filesystem import EditFileTool, ListFilesTool, ReadFileTool, WriteFileTool
from coding_agent.tools.search import SearchTool
from coding_agent.tools.shell import ShellTool

Invoke = Callable[..., Awaitable[ToolResult]]


# --- list_files / read_file / search --- #


async def test_list_files(invoke_tool: Invoke) -> None:
    result = await invoke_tool(ListFilesTool(), {"path": "."})
    assert result.status is ToolResultStatus.SUCCESS
    names = {entry["name"] for entry in result.output["entries"]}
    assert {"hello.py", "data"} <= names


async def test_read_file(invoke_tool: Invoke) -> None:
    result = await invoke_tool(ReadFileTool(), {"path": "hello.py"})
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["content"] == "print('Hello')\n"
    assert result.output["path"] == "hello.py"


async def test_read_file_missing(invoke_tool: Invoke) -> None:
    result = await invoke_tool(ReadFileTool(), {"path": "nope.txt"})
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED


async def test_read_file_binary(invoke_tool: Invoke, repo: Path) -> None:
    (repo / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    result = await invoke_tool(ReadFileTool(), {"path": "bin.dat"})
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED


async def test_search_finds_match(invoke_tool: Invoke) -> None:
    result = await invoke_tool(SearchTool(), {"pattern": "alpha"})
    assert result.status is ToolResultStatus.SUCCESS
    paths = {match["path"] for match in result.output["matches"]}
    assert os.path.join("data", "a.txt") in paths


async def test_search_invalid_regex(invoke_tool: Invoke) -> None:
    result = await invoke_tool(SearchTool(), {"pattern": "([unclosed"})
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_ARGUMENTS


# --- edit_file (D3 / §38) --- #


async def test_edit_file_success_and_diff(invoke_tool: Invoke, repo: Path) -> None:
    result = await invoke_tool(
        EditFileTool(),
        {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('Hello, Rob.')"},
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["path"] == "hello.py"
    assert result.output["diff"] == (
        "--- hello.py\n"
        "+++ hello.py\n"
        "@@ -1 +1 @@\n"
        "-print('Hello')\n"
        "+print('Hello, Rob.')"
    )
    assert (repo / "hello.py").read_text(encoding="utf-8") == "print('Hello, Rob.')\n"


async def test_edit_file_empty_old_text(invoke_tool: Invoke) -> None:
    result = await invoke_tool(
        EditFileTool(), {"path": "hello.py", "old_text": "", "new_text": "x"}
    )
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_ARGUMENTS


async def test_edit_file_no_match(invoke_tool: Invoke) -> None:
    result = await invoke_tool(
        EditFileTool(), {"path": "hello.py", "old_text": "absent", "new_text": "x"}
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.EDIT_NO_MATCH


async def test_edit_file_ambiguous(invoke_tool: Invoke, repo: Path) -> None:
    (repo / "dup.txt").write_text("foo\nfoo\n", encoding="utf-8")
    result = await invoke_tool(
        EditFileTool(), {"path": "dup.txt", "old_text": "foo", "new_text": "bar"}
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.EDIT_AMBIGUOUS


async def test_edit_file_not_utf8(invoke_tool: Invoke, repo: Path) -> None:
    (repo / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    result = await invoke_tool(
        EditFileTool(), {"path": "bin.dat", "old_text": "x", "new_text": "y"}
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.EDIT_NOT_UTF8


async def test_edit_file_missing(invoke_tool: Invoke) -> None:
    result = await invoke_tool(
        EditFileTool(), {"path": "ghost.py", "old_text": "a", "new_text": "b"}
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED


async def test_edit_file_preserves_crlf(invoke_tool: Invoke, repo: Path) -> None:
    (repo / "crlf.txt").write_bytes(b"a = 1\r\nb = 2\r\n")
    result = await invoke_tool(
        EditFileTool(), {"path": "crlf.txt", "old_text": "a = 1", "new_text": "a = 99"}
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert (repo / "crlf.txt").read_bytes() == b"a = 99\r\nb = 2\r\n"


async def test_edit_file_preserves_missing_trailing_newline(
    invoke_tool: Invoke, repo: Path
) -> None:
    (repo / "noeol.txt").write_text("x = 1", encoding="utf-8")
    await invoke_tool(
        EditFileTool(), {"path": "noeol.txt", "old_text": "x = 1", "new_text": "x = 2"}
    )
    assert (repo / "noeol.txt").read_bytes() == b"x = 2"


async def test_edit_file_atomic_preserves_mode_and_leaves_no_temp(
    invoke_tool: Invoke, repo: Path
) -> None:
    target = repo / "moded.txt"
    target.write_text("a\n", encoding="utf-8")
    os.chmod(target, 0o600)
    result = await invoke_tool(
        EditFileTool(), {"path": "moded.txt", "old_text": "a", "new_text": "b"}
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    leftovers = [name for name in os.listdir(repo) if name.startswith(".edit_")]
    assert leftovers == []


# --- write_file (v0.2, D20 / §41) --- #


async def test_write_file_creates_new_file_with_exact_bytes(
    invoke_tool: Invoke, repo: Path
) -> None:
    result = await invoke_tool(
        WriteFileTool(), {"path": "new.txt", "content": "héllo\nline2\n"}
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["path"] == "new.txt"
    assert result.output["created"] is True
    written = (repo / "new.txt").read_bytes()
    assert written == "héllo\nline2\n".encode()  # exact UTF-8 bytes (é -> 0xc3 0xa9)
    assert result.output["bytes_written"] == len(written)


async def test_write_file_overwrites_existing_and_reports_created_false(
    invoke_tool: Invoke, repo: Path
) -> None:
    result = await invoke_tool(
        WriteFileTool(), {"path": "hello.py", "content": "print('Replaced')\n"}
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["created"] is False
    assert (repo / "hello.py").read_text(encoding="utf-8") == "print('Replaced')\n"


async def test_write_file_missing_parent_dir_is_execution_failed(
    invoke_tool: Invoke, repo: Path
) -> None:
    result = await invoke_tool(WriteFileTool(), {"path": "nope_dir/x.txt", "content": "x"})
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED
    assert not (repo / "nope_dir").exists()  # parent directory not implicitly created


async def test_write_file_is_atomic_preserves_mode_and_leaves_no_temp(
    invoke_tool: Invoke, repo: Path
) -> None:
    target = repo / "moded.txt"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o600)
    result = await invoke_tool(WriteFileTool(), {"path": "moded.txt", "content": "new\n"})
    assert result.status is ToolResultStatus.SUCCESS
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert [name for name in os.listdir(repo) if name.startswith(".edit_")] == []


async def test_edit_file_still_does_not_create_files(invoke_tool: Invoke, repo: Path) -> None:
    # Boundary check: edit_file stays surgical and must NOT create a missing file —
    # creating files is write_file's job (D20). Guards the edit_file/write_file split.
    result = await invoke_tool(
        EditFileTool(), {"path": "brand_new.txt", "old_text": "a", "new_text": "b"}
    )
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED
    assert not (repo / "brand_new.txt").exists()


# --- shell (D5: shlex + shell=False) --- #


async def test_shell_success(invoke_tool: Invoke) -> None:
    result = await invoke_tool(ShellTool(), {"command": "echo hi"})
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["stdout"].strip() == "hi"
    assert result.output["exit_code"] == 0


async def test_shell_is_not_run_through_a_shell(invoke_tool: Invoke, repo: Path) -> None:
    # With shell=False the redirection is passed as literal argv to echo.
    result = await invoke_tool(ShellTool(), {"command": "echo hello > escaped.txt"})
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["stdout"].strip() == "hello > escaped.txt"
    assert not (repo / "escaped.txt").exists()


async def test_shell_nonzero_exit(invoke_tool: Invoke, pycmd: Callable[[str], str]) -> None:
    result = await invoke_tool(ShellTool(), {"command": pycmd("import sys; sys.exit(3)")})
    assert result.status is ToolResultStatus.FAILURE
    assert result.output["exit_code"] == 3
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED


async def test_shell_program_not_found(invoke_tool: Invoke) -> None:
    result = await invoke_tool(ShellTool(), {"command": "definitely_not_a_real_program_zzz"})
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED


async def test_shell_timeout(invoke_tool: Invoke, sleep_cmd: str) -> None:
    result = await invoke_tool(ShellTool(), {"command": sleep_cmd}, timeout_ms=200)
    assert result.status is ToolResultStatus.TIMEOUT
    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-focused suite")
async def test_shell_cwd_is_repo_root(invoke_tool: Invoke, repo: Path) -> None:
    result = await invoke_tool(ShellTool(), {"command": "pwd"})
    assert result.status is ToolResultStatus.SUCCESS
    assert Path(result.output["stdout"].strip()).samefile(repo)
