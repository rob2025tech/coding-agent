"""Verification tests (docs/contracts.md §35; D4/D12/D15, R3/R4)."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import py_compile
import shlex
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coding_agent.contracts import VerificationResult
from coding_agent.verification import (
    DEFAULT_VERIFY_TIMEOUT_S,
    TRUNCATION_MARKER,
    VERIFY_OUTPUT_MAX_CHARS,
    VerificationTracker,
    Verifier,
    truncate_output,
)


def _result(passed: bool) -> VerificationResult:
    return VerificationResult(
        passed=passed,
        command="x",
        output="",
        exit_code=0 if passed else 1,
        ran_at=dt.datetime(2026, 1, 1),
        timed_out=False,
    )


# --- centralized constants (D15, R3) --- #


def test_constants() -> None:
    assert VERIFY_OUTPUT_MAX_CHARS == 8000
    assert DEFAULT_VERIFY_TIMEOUT_S == 120.0
    assert TRUNCATION_MARKER == "...[truncated]..."
    assert Verifier("x", repo_root=".").timeout_s == 120.0


# --- deterministic truncation (R4) --- #


def test_truncate_short_unchanged() -> None:
    assert truncate_output("abc") == "abc"
    exact = "z" * VERIFY_OUTPUT_MAX_CHARS
    assert truncate_output(exact) == exact  # <= limit is untouched


def test_truncate_is_deterministic_head_plus_tail() -> None:
    text = "z" * (VERIFY_OUTPUT_MAX_CHARS + 1)
    out = truncate_output(text)
    assert out == truncate_output(text)  # deterministic
    assert TRUNCATION_MARKER in out
    # 4,000 head + marker + 4,000 tail (not exactly 8,000; marker adds chars)
    assert len(out) == 4000 + len(TRUNCATION_MARKER) + 4000
    assert out.startswith("z" * 4000)
    assert out.endswith("z" * 4000)


def test_truncate_keeps_head_and_tail_content() -> None:
    text = "H" * 5000 + "M" * 5000 + "T" * 5000
    out = truncate_output(text)
    assert out.startswith("H" * 4000)
    assert out.endswith("T" * 4000)
    assert TRUNCATION_MARKER in out


# --- Verifier --- #


def test_configured(repo: Path) -> None:
    assert Verifier("true", repo_root=str(repo)).configured is True
    assert Verifier(None, repo_root=str(repo)).configured is False
    assert Verifier("   ", repo_root=str(repo)).configured is False


async def test_not_configured_run_raises(repo: Path) -> None:
    with pytest.raises(RuntimeError):
        await Verifier(None, repo_root=str(repo)).run()


async def test_verifier_success(repo: Path, pycmd: Callable[[str], str]) -> None:
    verifier = Verifier(pycmd("print('PASS')"), repo_root=str(repo))
    result = await verifier.run()
    assert result.passed is True
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "PASS" in result.output


async def test_verifier_failure(repo: Path, pycmd: Callable[[str], str]) -> None:
    verifier = Verifier(
        pycmd("import sys; sys.stderr.write('boom'); sys.exit(2)"), repo_root=str(repo)
    )
    result = await verifier.run()
    assert result.passed is False
    assert result.exit_code == 2
    assert "boom" in result.output


async def test_verifier_timeout(repo: Path, sleep_cmd: str) -> None:
    verifier = Verifier(sleep_cmd, repo_root=str(repo), timeout_s=0.2)
    result = await verifier.run()
    assert result.passed is False
    assert result.timed_out is True


# --- subprocess lifecycle: cancellation, exceptions, timeout cleanup -------- #


def _pid_writer_code(pid_file: Path, *, seconds: float = 10.0) -> str:
    return (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        f"time.sleep({seconds})"
    )


async def _wait_for_pids(pid_file: Path, *, timeout_s: float = 2.0) -> list[int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pid_file.exists():
            values = pid_file.read_text(encoding="utf-8").split()
            if values:
                return [int(value) for value in values]
        await asyncio.sleep(0.02)
    raise AssertionError(f"verifier child did not record pids in {pid_file}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_dead(pid: int, *, timeout_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _read_pids(path: Path) -> list[int]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not text.endswith("\n"):
        return []
    pids: list[int] = []
    for value in text.split():
        try:
            pid = int(value)
        except ValueError:
            continue
        if pid > 1 and pid not in {os.getpid(), os.getppid()} and pid not in pids:
            pids.append(pid)
    return pids


async def _cleanup_pids(
    pid_file: Path, known: list[int], *, timeout_s: float = 0.5
) -> None:
    """Kill known and recorded PIDs, allowing a late atomic write to land."""
    deadline = time.monotonic() + timeout_s
    pids = [
        pid
        for pid in dict.fromkeys(known)
        if pid > 1 and pid not in {os.getpid(), os.getppid()}
    ]
    while True:
        for path in (pid_file, pid_file.with_suffix(".tmp")):
            for pid in _read_pids(path):
                if pid not in pids:
                    pids.append(pid)
        if len(pids) >= 2 or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.02)
    for pid in pids:
        _kill_pid(pid)


def _fail_first_wait(
    monkeypatch: pytest.MonkeyPatch, pid_file: Path, exc: BaseException
) -> Callable[..., Any]:
    """Make the verifier's first wait raise ``exc`` after its child records a pid."""
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


async def test_cancellation_kills_and_reaps_child(
    repo: Path, pycmd: Callable[[str], str]
) -> None:
    pid_file = repo / "cancel.pid"
    verifier = Verifier(
        pycmd(_pid_writer_code(pid_file)), repo_root=str(repo), timeout_s=30.0
    )
    task = asyncio.create_task(verifier.run())
    pids: list[int] = []
    try:
        pids = await _wait_for_pids(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_dead(pids[0])
    finally:
        for pid in pids:
            _kill_pid(pid)


async def test_keyboard_interrupt_kills_and_reaps_child(
    repo: Path, pycmd: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = repo / "interrupt.pid"
    verifier = Verifier(
        pycmd(_pid_writer_code(pid_file)), repo_root=str(repo), timeout_s=30.0
    )
    # Same 3.11 hazard as test_shell_keyboard_interrupt_kills_direct_child: an
    # outer asyncio.wait_for() wraps the call in a Task, and a KeyboardInterrupt
    # raised inside a Task is re-raised into the event loop, aborting the session.
    _fail_first_wait(monkeypatch, pid_file, KeyboardInterrupt())
    pids: list[int] = []
    try:
        with pytest.raises(KeyboardInterrupt):
            async with asyncio.timeout(5.0):
                await verifier.run()
        pids = await _wait_for_pids(pid_file)
        assert await _wait_dead(pids[0])
    finally:
        for pid in pids:
            _kill_pid(pid)


async def test_unexpected_exception_kills_and_reaps_child(
    repo: Path, pycmd: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = repo / "error.pid"
    verifier = Verifier(
        pycmd(_pid_writer_code(pid_file)), repo_root=str(repo), timeout_s=30.0
    )
    real_wait_for = _fail_first_wait(monkeypatch, pid_file, RuntimeError("verifier boom"))
    pids: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="verifier boom"):
            await real_wait_for(verifier.run(), timeout=5.0)
        pids = await _wait_for_pids(pid_file)
        assert await _wait_dead(pids[0])
    finally:
        for pid in pids:
            _kill_pid(pid)


async def test_repeated_cancellation_during_cleanup_supersedes_original(
    repo: Path, pycmd: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation arriving during cleanup propagates; the child is still killed."""
    pid_file = repo / "repeat-cancel.pid"
    verifier = Verifier(
        pycmd(_pid_writer_code(pid_file)), repo_root=str(repo), timeout_s=30.0
    )
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
            raise RuntimeError("verifier boom")
        cleanup_started.set()
        try:
            await asyncio.sleep(10)
        finally:
            aw.close()
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _fake)
    task = asyncio.create_task(verifier.run())
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 2.0
        while not cleanup_started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert cleanup_started.is_set(), "cleanup did not start"
        pids = await _wait_for_pids(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_dead(pids[0])
    finally:
        if not task.done():
            task.cancel()
        for pid in pids:
            _kill_pid(pid)


async def test_timeout_is_bounded_when_descendant_holds_pipes(
    repo: Path, pycmd: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = repo / "timeout-tree.pid"
    code = (
        "import os, pathlib, subprocess, time; "
        "child = subprocess.Popen(['sleep', '3']); "
        f"target = pathlib.Path({str(pid_file)!r}); "
        "tmp = target.with_suffix('.tmp'); "
        "tmp.write_text(f'{os.getpid()} {child.pid}\\n'); "
        "os.replace(tmp, target); "
        "time.sleep(30)"
    )
    verifier = Verifier(pycmd(code), repo_root=str(repo), timeout_s=0.2)
    real_wait_for = asyncio.wait_for
    timeout_started: list[float] = []
    calls = 0

    async def _after_setup(aw: Any, timeout: float) -> Any:
        # Start the verifier timeout only after the descendant is running, so slow
        # interpreter startup cannot consume the timeout before setup completes.
        nonlocal calls
        calls += 1
        if calls == 1:
            deadline = time.monotonic() + 2.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert pid_file.exists(), "verifier child did not finish setup"
            timeout_started.append(time.monotonic())
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _after_setup)
    pids: list[int] = []
    try:
        result = await real_wait_for(verifier.run(), timeout=5.0)
        elapsed = time.monotonic() - timeout_started[0]
        pids = await _wait_for_pids(pid_file)
        assert result.timed_out is True
        assert result.passed is False
        assert elapsed < 1.2
        assert await _wait_dead(pids[0])
    finally:
        await _cleanup_pids(pid_file, pids)


async def test_verifier_truncates_large_output(repo: Path, pycmd: Callable[[str], str]) -> None:
    verifier = Verifier(pycmd("print('y' * 9000)"), repo_root=str(repo))
    result = await verifier.run()
    assert result.passed is True
    assert TRUNCATION_MARKER in result.output
    assert len(result.output) == 4000 + len(TRUNCATION_MARKER) + 4000


async def test_verifier_runs_in_repo_root(repo: Path, pycmd: Callable[[str], str]) -> None:
    verifier = Verifier(pycmd("import pathlib; print(pathlib.Path('hello.py').exists())"),
                        repo_root=str(repo))
    result = await verifier.run()
    assert "True" in result.output


async def test_verifier_observes_current_source_not_stale_bytecode(repo: Path) -> None:
    """A Python verifier must observe the CURRENT source, never a stale ``.pyc``.

    Regression for the production defect where a same-byte-length source edit whose
    mtime second matches the one recorded in an existing timestamp-based ``.pyc``
    makes CPython treat the stale bytecode as valid — so verification kept failing
    against the corrected source. ``Verifier.run()`` must isolate bytecode caching so
    the result always reflects the current tree (D15). No ``sleep`` is used; the stale
    condition is arranged deterministically via ``os.utime``.
    """
    target = repo / "target.py"
    verify = repo / "verify.py"
    v1 = "def compute(a, b):\n    return a - b\n"
    v2 = "def compute(a, b):\n    return a + b\n"
    assert len(v1.encode()) == len(v2.encode())  # exactly the same byte length
    target.write_text(v1, encoding="utf-8")
    verify.write_text(
        "import target\nraise SystemExit(0 if target.compute(2, 1) == 3 else 1)\n",
        encoding="utf-8",
    )
    verifier = Verifier(f"{shlex.quote(sys.executable)} verify.py", repo_root=str(repo))

    # Run verification once so timestamp-based bytecode for v1 is produced. v1 gives
    # compute(2, 1) == 1, so this first run legitimately fails.
    first = await verifier.run()
    assert first.passed is False

    # Guarantee the timestamp-based .pyc for v1 exists in the workspace and capture the
    # mtime second it records. (The fix redirects the verifier's own bytecode cache, so
    # the artifact is planted explicitly to exercise the pre-existing-stale-.pyc path.)
    py_compile.compile(str(target))
    recorded_second = int(target.stat().st_mtime)
    assert list((repo / "__pycache__").glob("target*.pyc"))

    # Replace the source with different contents of exactly the same byte length, then
    # arrange the mtime so the existing timestamp-based .pyc would still look valid.
    target.write_text(v2, encoding="utf-8")
    os.utime(target, (recorded_second, recorded_second))
    assert int(target.stat().st_mtime) == recorded_second
    assert target.stat().st_size == len(v2.encode())

    # The second verification must observe the CURRENT source (v2 -> compute(2, 1) == 3)
    # and pass, rather than executing the stale v1 bytecode.
    second = await verifier.run()
    assert second.exit_code == 0
    assert second.passed is True


# --- staleness tracker (D12) --- #


def test_tracker_initial_state() -> None:
    tracker = VerificationTracker()
    assert tracker.ever_wrote is False
    assert tracker.dirty is False
    assert tracker.last_passed is None


def test_tracker_write_then_pass_clears_dirty() -> None:
    tracker = VerificationTracker()
    tracker.note_write()
    assert tracker.ever_wrote is True
    assert tracker.dirty is True
    tracker.note_result(_result(passed=True))
    assert tracker.dirty is False
    assert tracker.last_passed is not None


def test_tracker_write_after_pass_makes_stale() -> None:
    tracker = VerificationTracker()
    tracker.note_write()
    tracker.note_result(_result(passed=True))
    assert tracker.dirty is False
    tracker.note_write()  # a later write invalidates the pass (D12)
    assert tracker.dirty is True


def test_tracker_failed_verification_keeps_dirty() -> None:
    tracker = VerificationTracker()
    tracker.note_write()
    tracker.note_result(_result(passed=False))
    assert tracker.dirty is True
    assert tracker.last_passed is None
