"""Tool behavior tests (docs/contracts.md §28, §38, §20; D3/D5/D17)."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from coding_agent.contracts import ErrorCode, ToolResult, ToolResultStatus
from coding_agent.tools.filesystem import (
    DeleteFileTool,
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from coding_agent.tools.search import SearchTool
from coding_agent.tools.shell import ShellTool
from proc_helpers import (
    cleanup_pids,
    descendant_writer_code,
    kill_pid,
    pid_alive,
    pid_writer_code,
    wait_dead,
    wait_for_pids,
)

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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
async def test_search_skips_symlink_escaping_workspace(
    invoke_tool: Invoke, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_SECRET_MARKER\n", encoding="utf-8")
    os.symlink(str(outside), str(repo / "leak.txt"))

    result = await invoke_tool(SearchTool(), {"pattern": "OUTSIDE_SECRET_MARKER"})
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["matches"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
async def test_search_skips_symlink_chain_escaping_workspace(
    invoke_tool: Invoke, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("CHAIN_SECRET_MARKER\n", encoding="utf-8")
    os.symlink(str(outside), str(repo / "first_link.txt"))
    os.symlink(str(repo / "first_link.txt"), str(repo / "second_link.txt"))

    result = await invoke_tool(SearchTool(), {"pattern": "CHAIN_SECRET_MARKER"})
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["matches"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
async def test_search_reads_symlink_to_file_inside_workspace(
    invoke_tool: Invoke, repo: Path
) -> None:
    os.symlink(str(repo / "data" / "a.txt"), str(repo / "alias.txt"))

    result = await invoke_tool(SearchTool(), {"pattern": "alpha"})
    assert result.status is ToolResultStatus.SUCCESS
    paths = {match["path"] for match in result.output["matches"]}
    assert "alias.txt" in paths


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
async def test_search_does_not_follow_symlinked_directory_outside_workspace(
    invoke_tool: Invoke, repo: Path, tmp_path: Path
) -> None:
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("DIR_SECRET_MARKER\n", encoding="utf-8")
    os.symlink(str(outside_dir), str(repo / "outside_dir_link"))

    result = await invoke_tool(SearchTool(), {"pattern": "DIR_SECRET_MARKER"})
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["matches"] == []


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


# --- delete_file (v0.2, D25 / §43) --- #


async def test_delete_file_removes_existing_file(invoke_tool: Invoke, repo: Path) -> None:
    result = await invoke_tool(DeleteFileTool(), {"path": "hello.py"})
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output == {"path": "hello.py"}
    assert not (repo / "hello.py").exists()


async def test_delete_file_nonexistent_is_execution_failed(invoke_tool: Invoke) -> None:
    result = await invoke_tool(DeleteFileTool(), {"path": "ghost.txt"})
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED


async def test_delete_file_directory_is_rejected(invoke_tool: Invoke, repo: Path) -> None:
    # files only (D25): a directory target is a structured failure, never a recursion.
    (repo / "adir").mkdir()
    (repo / "adir" / "inner.txt").write_text("x\n", encoding="utf-8")
    result = await invoke_tool(DeleteFileTool(), {"path": "adir"})
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED
    assert (repo / "adir" / "inner.txt").exists()  # nothing under it was touched


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
async def test_delete_file_removes_internal_symlink_not_its_target(
    invoke_tool: Invoke, repo: Path
) -> None:
    # §43: an in-workspace symlink is removed as a link; the target survives.
    os.symlink("hello.py", repo / "link.py")
    result = await invoke_tool(DeleteFileTool(), {"path": "link.py"})
    assert result.status is ToolResultStatus.SUCCESS
    assert not (repo / "link.py").exists()  # the link is gone...
    assert (repo / "hello.py").read_text(encoding="utf-8") == "print('Hello')\n"  # target safe


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


# --- shell subprocess lifecycle: bounded direct-child cleanup --- #


def _fail_first_wait(
    monkeypatch: pytest.MonkeyPatch, pid_file: Path, exc: BaseException
) -> Callable[..., Any]:
    """Make the shell's communicate wait raise ``exc`` after the child records a pid."""
    real_wait_for = asyncio.wait_for
    calls = 0

    async def _fake(aw: Any, timeout: float) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            deadline = time.monotonic() + 2.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            aw.close()
            raise exc
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _fake)
    return real_wait_for


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness checks")
async def test_shell_timeout_kills_direct_child(
    invoke_tool: Invoke,
    repo: Path,
    pycmd: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = repo / "shell-timeout.pid"
    real_wait_for = asyncio.wait_for
    calls = 0

    async def _after_setup(aw: Any, timeout: float) -> Any:
        # Start the shell timeout only after the child is running, so slow
        # interpreter startup cannot consume the timeout before setup completes.
        nonlocal calls
        calls += 1
        if calls == 1:
            deadline = time.monotonic() + 2.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert pid_file.exists(), "shell child did not finish setup"
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _after_setup)
    pids: list[int] = []
    try:
        result = await real_wait_for(
            invoke_tool(
                ShellTool(), {"command": pycmd(pid_writer_code(pid_file))}, timeout_ms=1000
            ),
            timeout=5.0,
        )
        pids = await wait_for_pids(pid_file)
        assert result.status is ToolResultStatus.TIMEOUT
        assert result.error is not None
        assert result.error.code is ErrorCode.TIMEOUT
        assert await wait_dead(pids[0])
    finally:
        for pid in pids:
            kill_pid(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness checks")
async def test_shell_timeout_is_bounded_when_descendant_holds_pipes(
    invoke_tool: Invoke,
    repo: Path,
    pycmd: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = repo / "shell-timeout-tree.pid"
    real_wait_for = asyncio.wait_for
    timeout_started: list[float] = []
    calls = 0

    async def _after_setup(aw: Any, timeout: float) -> Any:
        # Start the shell timeout only after the descendant is running, so slow
        # interpreter startup cannot consume the timeout before setup completes.
        nonlocal calls
        calls += 1
        if calls == 1:
            deadline = time.monotonic() + 2.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert pid_file.exists(), "shell child did not finish setup"
            timeout_started.append(time.monotonic())
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _after_setup)
    pids: list[int] = []
    try:
        result = await real_wait_for(
            invoke_tool(
                ShellTool(), {"command": pycmd(descendant_writer_code(pid_file))}, timeout_ms=200
            ),
            timeout=5.0,
        )
        elapsed = time.monotonic() - timeout_started[0]
        pids = await wait_for_pids(pid_file)
        assert result.status is ToolResultStatus.TIMEOUT
        assert result.error is not None
        assert result.error.code is ErrorCode.TIMEOUT
        assert elapsed < 1.5
        assert await wait_dead(pids[0])
        # Descendants are not terminated; this is a documented limitation.
        assert pid_alive(pids[1])
    finally:
        await cleanup_pids(pid_file, pids)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness checks")
async def test_shell_cancellation_kills_direct_child(
    invoke_tool: Invoke, repo: Path, pycmd: Callable[[str], str]
) -> None:
    pid_file = repo / "shell-cancel.pid"
    task = asyncio.create_task(
        invoke_tool(
            ShellTool(), {"command": pycmd(pid_writer_code(pid_file))}, timeout_ms=30_000
        )
    )
    pids: list[int] = []
    try:
        pids = await wait_for_pids(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await wait_dead(pids[0])
    finally:
        if not task.done():
            task.cancel()
        for pid in pids:
            kill_pid(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness checks")
async def test_shell_keyboard_interrupt_kills_direct_child(
    invoke_tool: Invoke,
    repo: Path,
    pycmd: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = repo / "shell-interrupt.pid"
    # Keep the injected KeyboardInterrupt in this task: on 3.11 an outer
    # asyncio.wait_for() wraps the tool call in a Task, and a KeyboardInterrupt
    # raised inside a Task is re-raised into the event loop, aborting the session
    # even though pytest.raises matches it (3.12 removed the task wrapping).
    _fail_first_wait(monkeypatch, pid_file, KeyboardInterrupt())
    pids: list[int] = []
    try:
        with pytest.raises(KeyboardInterrupt):
            async with asyncio.timeout(5.0):
                await invoke_tool(
                    ShellTool(), {"command": pycmd(pid_writer_code(pid_file))}, timeout_ms=30_000
                )
        pids = await wait_for_pids(pid_file)
        assert await wait_dead(pids[0])
    finally:
        for pid in pids:
            kill_pid(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness checks")
async def test_shell_unexpected_exception_kills_direct_child(
    invoke_tool: Invoke,
    repo: Path,
    pycmd: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = repo / "shell-error.pid"
    real_wait_for = _fail_first_wait(monkeypatch, pid_file, RuntimeError("shell boom"))
    pids: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="shell boom"):
            await real_wait_for(
                invoke_tool(
                    ShellTool(), {"command": pycmd(pid_writer_code(pid_file))}, timeout_ms=30_000
                ),
                timeout=5.0,
            )
        pids = await wait_for_pids(pid_file)
        assert await wait_dead(pids[0])
    finally:
        for pid in pids:
            kill_pid(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness checks")
async def test_shell_repeated_cancellation_during_cleanup_supersedes_original(
    invoke_tool: Invoke,
    repo: Path,
    pycmd: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation arriving during cleanup propagates; the child is still killed."""
    pid_file = repo / "shell-repeat-cancel.pid"
    real_wait_for = asyncio.wait_for
    cleanup_started = asyncio.Event()
    calls = 0

    async def _fake(aw: Any, timeout: float) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            deadline = time.monotonic() + 2.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            aw.close()
            raise RuntimeError("shell boom")
        cleanup_started.set()
        try:
            await asyncio.sleep(10)
        finally:
            aw.close()
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _fake)
    task = asyncio.create_task(
        invoke_tool(
            ShellTool(), {"command": pycmd(pid_writer_code(pid_file))}, timeout_ms=30_000
        )
    )
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 2.0
        while not cleanup_started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert cleanup_started.is_set(), "cleanup did not start"
        pids = await wait_for_pids(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await wait_dead(pids[0])
    finally:
        if not task.done():
            task.cancel()
        for pid in pids:
            kill_pid(pid)
