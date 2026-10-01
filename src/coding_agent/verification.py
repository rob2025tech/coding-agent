"""Runtime-owned verification (docs/contracts.md §35, D4/D12/D14/D15, R3/R4).

The verify command is **human-supplied** (``--verify``) and pre-authorized; it is
never model-supplied and a model ``shell`` call never counts as verification. It
runs as a single argv (``shlex.split`` + ``shell=False``) pinned to ``repo_root``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import shlex
import tempfile

from coding_agent.contracts import VerificationResult

# --- centralized verifier constants (D15, R4) --- #
VERIFY_OUTPUT_MAX_CHARS = 8000
_HEAD_CHARS = 4000
_TAIL_CHARS = 4000
TRUNCATION_MARKER = "...[truncated]..."
DEFAULT_VERIFY_TIMEOUT_S = 120.0


def truncate_output(text: str) -> str:
    """Deterministic truncation (R4).

    Output longer than :data:`VERIFY_OUTPUT_MAX_CHARS` keeps up to 4,000 head
    chars and 4,000 tail chars joined by ``...[truncated]...``. The rendered
    string is **not** exactly 8,000 chars — the marker adds characters.
    """
    if len(text) <= VERIFY_OUTPUT_MAX_CHARS:
        return text
    return f"{text[:_HEAD_CHARS]}{TRUNCATION_MARKER}{text[-_TAIL_CHARS:]}"


class Verifier:
    """Runs the human-configured verify command and captures a structured result."""

    def __init__(
        self,
        command: str | None,
        *,
        repo_root: str,
        timeout_s: float = DEFAULT_VERIFY_TIMEOUT_S,
    ) -> None:
        self.command = command
        self.repo_root = repo_root
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return self.command is not None and self.command.strip() != ""

    async def run(self) -> VerificationResult:
        command = self.command
        if command is None:
            raise RuntimeError("verifier is not configured")
        ran_at = dt.datetime.now()

        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return VerificationResult(
                passed=False,
                command=command,
                output=f"verify command is not a valid argv: {exc}",
                exit_code=None,
                ran_at=ran_at,
                timed_out=False,
            )
        if not argv:
            return VerificationResult(
                passed=False,
                command=command,
                output="verify command is empty",
                exit_code=None,
                ran_at=ran_at,
                timed_out=False,
            )

        # Isolate bytecode caching for the verifier subprocess. A fresh, empty
        # ``PYTHONPYCACHEPREFIX`` per run guarantees a cache miss, so a Python verifier
        # recompiles from the CURRENT source instead of trusting a stale timestamp-based
        # ``.pyc`` in the workspace — which CPython treats as valid when a same-size edit
        # lands in the same mtime second. Redirecting the cache also keeps ``__pycache__``
        # out of the repo and is inert for non-Python commands. Only the child environment
        # changes; argv, cwd, timeout, truncation and exit-code semantics (§35) are intact.
        with tempfile.TemporaryDirectory(prefix="coding-agent-pycache-") as pycache_prefix:
            env = {**os.environ, "PYTHONPYCACHEPREFIX": pycache_prefix}
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=self.repo_root,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, OSError) as exc:
                return VerificationResult(
                    passed=False,
                    command=command,
                    output=f"verify command failed to start: {exc}",
                    exit_code=None,
                    ran_at=ran_at,
                    timed_out=False,
                )

            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_s
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                stdout_b, stderr_b = b"", b""
                timed_out = True

        combined = stdout_b.decode("utf-8", errors="replace") + stderr_b.decode(
            "utf-8", errors="replace"
        )
        if timed_out:
            combined = f"[timed out after {self.timeout_s}s]\n{combined}"
        exit_code = proc.returncode
        passed = not timed_out and exit_code == 0
        return VerificationResult(
            passed=passed,
            command=command,
            output=truncate_output(combined),
            exit_code=exit_code,
            ran_at=ran_at,
            timed_out=timed_out,
        )


class VerificationTracker:
    """Tracks the D12 write/staleness state used to decide the completion branch.

    A **write** is a successful ``edit_file`` or any non-allowlisted ``shell``
    execution. ``dirty`` is true when a write occurred since the last passing
    verification; a pass clears it.
    """

    def __init__(self) -> None:
        self.ever_wrote: bool = False
        self.dirty: bool = False
        self.last_passed: VerificationResult | None = None

    def note_write(self) -> None:
        self.ever_wrote = True
        self.dirty = True

    def note_result(self, result: VerificationResult) -> None:
        if result.passed:
            self.dirty = False
            self.last_passed = result
