"""POSIX process helpers shared by subprocess-lifecycle tests (test-only)."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path


def pid_writer_code(pid_file: Path, *, seconds: float = 10.0) -> str:
    """Python source that records its own pid atomically, then sleeps."""
    return (
        "import os, pathlib, time; "
        f"target = pathlib.Path({str(pid_file)!r}); "
        "tmp = target.with_suffix('.tmp'); "
        "tmp.write_text(str(os.getpid()) + '\\n'); "
        "os.replace(tmp, target); "
        f"time.sleep({seconds})"
    )


def descendant_writer_code(pid_file: Path, *, seconds: float = 30.0) -> str:
    """Python source that spawns a pipe-inheriting descendant and records both pids."""
    return (
        "import os, pathlib, subprocess, time; "
        "child = subprocess.Popen(['sleep', '10']); "
        f"target = pathlib.Path({str(pid_file)!r}); "
        "tmp = target.with_suffix('.tmp'); "
        "tmp.write_text(f'{os.getpid()} {child.pid}\\n'); "
        "os.replace(tmp, target); "
        f"time.sleep({seconds})"
    )


async def wait_for_pids(pid_file: Path, *, timeout_s: float = 2.0) -> list[int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pid_file.exists():
            values = pid_file.read_text(encoding="utf-8").split()
            if values:
                return [int(value) for value in values]
        await asyncio.sleep(0.02)
    raise AssertionError(f"subprocess did not record pids in {pid_file}")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user and cannot be signaled.
        return True
    return True


async def wait_dead(pid: int, *, timeout_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


def kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Not killed: the process exists but is not ours to signal. Cleanup
        # continues so one unsignalable pid cannot abort the remaining kills.
        pass


def read_pids(path: Path) -> list[int]:
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


async def cleanup_pids(pid_file: Path, known: list[int], *, timeout_s: float = 0.5) -> None:
    """Kill known and recorded PIDs, allowing a late atomic write to land."""
    deadline = time.monotonic() + timeout_s
    pids = [
        pid
        for pid in dict.fromkeys(known)
        if pid > 1 and pid not in {os.getpid(), os.getppid()}
    ]
    while True:
        for path in (pid_file, pid_file.with_suffix(".tmp")):
            for pid in read_pids(path):
                if pid not in pids:
                    pids.append(pid)
        if len(pids) >= 2 or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.02)
    for pid in pids:
        kill_pid(pid)
