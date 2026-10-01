"""ToolExecutor tests: D6 order, DENY/ASK, write tagging, events, timeout."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from coding_agent.contracts import (
    ErrorCode,
    PermissionRequest,
    ToolCall,
    ToolResultStatus,
)
from coding_agent.events import EventType, ListEventSink
from coding_agent.executor import ToolExecutor

SESSION = "s1"


class RecordingApprover:
    """An approver that records whether it was consulted (and how it voted)."""

    def __init__(self, approve: bool) -> None:
        self._approve = approve
        self.calls: list[PermissionRequest] = []

    def confirm(self, request: PermissionRequest) -> bool:
        self.calls.append(request)
        return self._approve


async def test_unknown_tool(make_executor: Callable[..., ToolExecutor]) -> None:
    sink = ListEventSink()
    executor = make_executor(sink=sink)
    result = await executor.execute(ToolCall("c1", "bogus", {}), session_id=SESSION)
    assert result.status is ToolResultStatus.FAILURE
    assert result.error is not None
    assert result.error.code is ErrorCode.TOOL_NOT_FOUND
    assert EventType.TOOL_FAILED in sink.types()


async def test_schema_validation_happens_before_approval(
    make_executor: Callable[..., ToolExecutor],
) -> None:
    approver = RecordingApprover(approve=True)
    executor = make_executor(approver=approver)
    # edit_file missing required "new_text" -> INVALID_ARGUMENTS, never asks.
    result = await executor.execute(
        ToolCall("c1", "edit_file", {"path": "hello.py", "old_text": "x"}), session_id=SESSION
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_ARGUMENTS
    assert approver.calls == []


async def test_additional_property_rejected(make_executor: Callable[..., ToolExecutor]) -> None:
    executor = make_executor()
    result = await executor.execute(
        ToolCall(
            "c1",
            "edit_file",
            {"path": "hello.py", "old_text": "a", "new_text": "b", "bogus": 1},
        ),
        session_id=SESSION,
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_ARGUMENTS


async def test_path_resolution_happens_before_approval(
    make_executor: Callable[..., ToolExecutor],
) -> None:
    approver = RecordingApprover(approve=True)
    executor = make_executor(approver=approver)
    result = await executor.execute(
        ToolCall("c1", "read_file", {"path": "../outside.txt"}), session_id=SESSION
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.PATH_OUTSIDE_WORKSPACE
    assert approver.calls == []


async def test_deny_never_reaches_approver(make_executor: Callable[..., ToolExecutor]) -> None:
    approver = RecordingApprover(approve=True)  # would approve if asked
    sink = ListEventSink()
    executor = make_executor(approver=approver, sink=sink)
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": "curl http://example.com"}), session_id=SESSION
    )
    assert result.status is ToolResultStatus.DENIED
    assert result.error is not None
    assert result.error.code is ErrorCode.PERMISSION_DENIED
    assert approver.calls == []  # --yes / model cannot override DENY
    assert EventType.PERMISSION_DENIED in sink.types()
    assert EventType.TOOL_STARTED not in sink.types()


async def test_ask_approved_executes(make_executor: Callable[..., ToolExecutor]) -> None:
    approver = RecordingApprover(approve=True)
    sink = ListEventSink()
    executor = make_executor(approver=approver, sink=sink)
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": "echo hi"}), session_id=SESSION
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert len(approver.calls) == 1
    types = sink.types()
    assert EventType.PERMISSION_REQUESTED in types
    assert EventType.PERMISSION_GRANTED in types
    assert EventType.TOOL_STARTED in types
    assert EventType.TOOL_COMPLETED in types


async def test_ask_denied_does_not_execute(make_executor: Callable[..., ToolExecutor]) -> None:
    approver = RecordingApprover(approve=False)
    sink = ListEventSink()
    executor = make_executor(approver=approver, sink=sink)
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": "echo hi"}), session_id=SESSION
    )
    assert result.status is ToolResultStatus.DENIED
    assert len(approver.calls) == 1
    assert EventType.PERMISSION_DENIED in sink.types()
    assert EventType.TOOL_STARTED not in sink.types()


async def test_allow_does_not_prompt(make_executor: Callable[..., ToolExecutor]) -> None:
    approver = RecordingApprover(approve=False)  # would deny if asked
    sink = ListEventSink()
    executor = make_executor(approver=approver, sink=sink)
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": "git status"}), session_id=SESSION
    )
    assert approver.calls == []  # ALLOW never consults the approver
    assert EventType.PERMISSION_GRANTED in sink.types()
    # read-only allowlisted command is never a write, whatever its exit status
    assert result.counts_as_write() is False


# --- D12 write tagging --- #


async def test_write_tagging_edit_success(make_executor: Callable[..., ToolExecutor]) -> None:
    executor = make_executor()
    result = await executor.execute(
        ToolCall(
            "c1",
            "edit_file",
            {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('Hi')"},
        ),
        session_id=SESSION,
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.counts_as_write() is True


async def test_write_tagging_edit_failure(make_executor: Callable[..., ToolExecutor]) -> None:
    executor = make_executor()
    result = await executor.execute(
        ToolCall("c1", "edit_file", {"path": "hello.py", "old_text": "absent", "new_text": "x"}),
        session_id=SESSION,
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.EDIT_NO_MATCH
    assert result.counts_as_write() is False


async def test_write_tagging_shell_ask_counts(make_executor: Callable[..., ToolExecutor]) -> None:
    executor = make_executor(approver=RecordingApprover(True))
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": "echo hi"}), session_id=SESSION
    )
    assert result.counts_as_write() is True


async def test_write_tagging_shell_deny_not_a_write(
    make_executor: Callable[..., ToolExecutor],
) -> None:
    executor = make_executor()
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": "sudo reboot"}), session_id=SESSION
    )
    assert result.status is ToolResultStatus.DENIED
    assert result.counts_as_write() is False


async def test_tool_timeout(make_executor: Callable[..., ToolExecutor], sleep_cmd: str) -> None:
    sink = ListEventSink()
    executor = make_executor(approver=RecordingApprover(True), sink=sink, timeout_ms=200)
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": sleep_cmd}), session_id=SESSION
    )
    assert result.status is ToolResultStatus.TIMEOUT
    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT
    assert EventType.TOOL_FAILED in sink.types()


@pytest.mark.parametrize(
    ("command", "expected"),
    [("git status", False), ("echo hi", True), ("curl x", False)],
)
async def test_write_tagging_matrix(
    make_executor: Callable[..., ToolExecutor], command: str, expected: bool
) -> None:
    executor = make_executor(approver=RecordingApprover(True))
    result = await executor.execute(
        ToolCall("c1", "shell", {"command": command}), session_id=SESSION
    )
    assert result.counts_as_write() is expected


# --- write_file (v0.2, D20 / §41): D6 order, ASK, escapes, write tagging --- #


async def test_write_file_ask_approved_creates_file(
    make_executor: Callable[..., ToolExecutor], repo: Path
) -> None:
    approver = RecordingApprover(approve=True)
    sink = ListEventSink()
    executor = make_executor(approver=approver, sink=sink)
    result = await executor.execute(
        ToolCall("c1", "write_file", {"path": "new.txt", "content": "hi\n"}), session_id=SESSION
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert len(approver.calls) == 1  # WRITE -> ASK consulted the approver
    types = sink.types()
    assert EventType.PERMISSION_REQUESTED in types
    assert EventType.PERMISSION_GRANTED in types
    assert EventType.TOOL_COMPLETED in types
    assert (repo / "new.txt").read_text(encoding="utf-8") == "hi\n"  # approved write succeeded
    assert result.counts_as_write() is True  # D12 write tagging


async def test_write_file_denied_writes_nothing(
    make_executor: Callable[..., ToolExecutor], repo: Path
) -> None:
    approver = RecordingApprover(approve=False)
    sink = ListEventSink()
    executor = make_executor(approver=approver, sink=sink)
    result = await executor.execute(
        ToolCall("c1", "write_file", {"path": "new.txt", "content": "hi\n"}), session_id=SESSION
    )
    assert result.status is ToolResultStatus.DENIED
    assert result.error is not None
    assert result.error.code is ErrorCode.PERMISSION_DENIED
    assert len(approver.calls) == 1
    assert not (repo / "new.txt").exists()  # denial leaves nothing behind
    assert EventType.PERMISSION_DENIED in sink.types()
    assert EventType.TOOL_STARTED not in sink.types()
    assert result.counts_as_write() is False


async def test_write_file_dotdot_escape_is_rejected(
    make_executor: Callable[..., ToolExecutor], repo: Path
) -> None:
    approver = RecordingApprover(approve=True)
    executor = make_executor(approver=approver)
    result = await executor.execute(
        ToolCall("c1", "write_file", {"path": "../escaped.txt", "content": "x"}),
        session_id=SESSION,
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.PATH_OUTSIDE_WORKSPACE
    assert approver.calls == []  # rejected at resolve (D6 step 2), before approval
    assert not (repo.parent / "escaped.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
async def test_write_file_symlink_escape_is_rejected(
    make_executor: Callable[..., ToolExecutor], repo: Path
) -> None:
    outside = repo.parent / "outside.txt"
    os.symlink(outside, repo / "link.txt")  # link.txt -> ../outside.txt (escapes workspace)
    executor = make_executor(approver=RecordingApprover(approve=True))
    result = await executor.execute(
        ToolCall("c1", "write_file", {"path": "link.txt", "content": "x"}), session_id=SESSION
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.PATH_OUTSIDE_WORKSPACE
    assert not outside.exists()  # the real target outside the workspace was never written


async def test_write_tagging_write_file_failure_not_a_write(
    make_executor: Callable[..., ToolExecutor],
) -> None:
    executor = make_executor(approver=RecordingApprover(True))
    result = await executor.execute(
        ToolCall("c1", "write_file", {"path": "nope/x.txt", "content": "x"}), session_id=SESSION
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_FAILED  # missing parent (D20)
    assert result.counts_as_write() is False  # a failed write is not a D12 write
