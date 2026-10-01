"""Permission policy, shell authorization, and the Approver seam.

Implements the finalized shell policy (D5/D16, R1/R2/R5) and the permission
classes (docs/contracts.md §13, §15, §16, §37; architecture §4.8).

Pure and synchronous (D1). ``DENY`` is final and cannot be overridden by
``--yes`` or by model output — the approver is only ever consulted for ``ASK``.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from typing import Protocol

from coding_agent.contracts import (
    PermissionClass,
    PermissionDecision,
    PermissionDecisionValue,
    PermissionRequest,
    RiskLevel,
    ToolCall,
    ToolDefinition,
)

# --------------------------------------------------------------------------- #
# Shell policy constants (D5 / D16). Do not broaden the allowlist.
# --------------------------------------------------------------------------- #

#: Exact-argv read-only allowlist, matched against the *full* tokenized argv.
#: ``git status`` / ``git diff`` are exact-argv only (R1) — no extra arguments.
SHELL_ALLOWLIST: frozenset[tuple[str, ...]] = frozenset(
    {
        ("git", "status"),
        ("git", "status", "--short"),
        ("git", "diff"),
        ("git", "diff", "--stat"),
        ("git", "log", "--oneline", "-n", "20"),
    }
)

#: ``argv[0]`` basenames that are always DENY (network tools + privilege tools).
SHELL_DENY_PROGRAMS: frozenset[str] = frozenset(
    {"curl", "wget", "ssh", "scp", "nc", "ncat", "sudo", "su"}
)

#: Credential paths/names; any argument naming one is DENY (D16, kept as-is by R5).
CREDENTIAL_NAMES: tuple[str, ...] = (
    ".env",
    ".ssh",
    ".aws",
    ".netrc",
    "id_rsa",
    ".git-credentials",
)

#: Shell metacharacters (D5). Presence in the raw command forces ASK.
SHELL_METACHARACTERS: frozenset[str] = frozenset("|;&><`$()\n")


def _basename(program: str) -> str:
    """Last path component of ``program`` (``/usr/bin/curl`` -> ``curl``)."""
    return program.rsplit("/", 1)[-1]


def _rm_is_recursive_and_force(argv: list[str]) -> bool:
    """True iff an ``rm`` invocation carries *both* recursive and force flags.

    Handles short flags in any combination (``-rf``, ``-fr``, ``-r -f``, ``-Rf``)
    and long flags (``--recursive --force``). ``rm -r`` or ``rm -f`` alone is
    **not** DENY (R2) — it falls through to ASK.
    """
    has_recursive = False
    has_force = False
    for arg in argv[1:]:
        if arg == "--":
            break
        if arg.startswith("--"):
            if arg == "--recursive":
                has_recursive = True
            elif arg == "--force":
                has_force = True
        elif arg.startswith("-") and len(arg) > 1:
            flags = arg[1:]
            if "r" in flags or "R" in flags:
                has_recursive = True
            if "f" in flags:
                has_force = True
    return has_recursive and has_force


def classify_shell_command(command: str) -> PermissionDecision:
    """Authorize a raw shell command string (D5/D16).

    Decision order: **DENY** (final) -> metacharacters (**ASK**) -> exact-argv
    allowlist (**ALLOW**) -> everything else (**ASK**). The command is tokenized
    with :mod:`shlex` and is always executed with ``shell=False``.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:  # unbalanced quotes etc. -> cannot classify safely
        return PermissionDecision(
            decision=PermissionDecisionValue.ASK,
            reason=f"unparseable command ({exc}); manual approval required",
        )

    if not argv:
        return PermissionDecision(decision=PermissionDecisionValue.ASK, reason="empty command")

    program = _basename(argv[0])

    # --- DENY (advisory, defense in depth, final; not overridable) ---
    if program in SHELL_DENY_PROGRAMS:
        return PermissionDecision(
            decision=PermissionDecisionValue.DENY,
            reason=f"program '{program}' is denied (network/privilege tool)",
        )
    if program == "rm" and _rm_is_recursive_and_force(argv):
        return PermissionDecision(
            decision=PermissionDecisionValue.DENY,
            reason="recursive force-delete (rm with both recursive and force) is denied",
        )
    for arg in argv:
        for credential in CREDENTIAL_NAMES:
            if credential in arg:
                return PermissionDecision(
                    decision=PermissionDecisionValue.DENY,
                    reason=f"argument names a credential path ('{credential}'); denied",
                )

    # --- metacharacters -> ASK (can never be allowlisted) ---
    if any(char in command for char in SHELL_METACHARACTERS):
        return PermissionDecision(
            decision=PermissionDecisionValue.ASK,
            reason="command contains shell metacharacters; manual approval required",
        )

    # --- exact-argv read-only allowlist -> ALLOW ---
    if tuple(argv) in SHELL_ALLOWLIST:
        return PermissionDecision(
            decision=PermissionDecisionValue.ALLOW, reason="read-only allowlisted command"
        )

    # --- everything else -> ASK ---
    return PermissionDecision(
        decision=PermissionDecisionValue.ASK,
        reason="command is not allowlisted; manual approval required",
    )


def risk_for_permission_class(permission_class: PermissionClass) -> RiskLevel:
    if permission_class is PermissionClass.READ:
        return RiskLevel.LOW
    if permission_class in (PermissionClass.WRITE, PermissionClass.EXECUTE):
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH  # NETWORK, DESTRUCTIVE


class PermissionPolicy:
    """Maps a tool call to a permission decision (docs/contracts.md §16)."""

    def check(self, tool: ToolDefinition, call: ToolCall) -> PermissionDecision:
        if tool.name == "shell":
            command = str(call.arguments.get("command", ""))
            return classify_shell_command(command)
        return self._by_permission_class(tool)

    def _by_permission_class(self, tool: ToolDefinition) -> PermissionDecision:
        permission_class = tool.permission
        if permission_class is PermissionClass.READ:
            return PermissionDecision(PermissionDecisionValue.ALLOW, "read-only tool")
        if permission_class is PermissionClass.WRITE:
            return PermissionDecision(
                PermissionDecisionValue.ASK, "filesystem write requires approval"
            )
        if permission_class is PermissionClass.EXECUTE:
            return PermissionDecision(
                PermissionDecisionValue.ASK, "process execution requires approval"
            )
        if permission_class is PermissionClass.NETWORK:
            return PermissionDecision(
                PermissionDecisionValue.DENY, "network access is denied in v0.1"
            )
        return PermissionDecision(
            PermissionDecisionValue.ASK, "destructive operation requires approval"
        )


def build_permission_request(
    tool: ToolDefinition,
    call: ToolCall,
    decision: PermissionDecision,
    request_id: str,
) -> PermissionRequest:
    return PermissionRequest(
        permission_request_id=request_id,
        tool_call_id=call.tool_call_id,
        tool_name=tool.name,
        permission_class=tool.permission,
        reason=decision.reason or f"{tool.name} requires approval",
        risk=risk_for_permission_class(tool.permission),
    )


# --------------------------------------------------------------------------- #
# Approver seam (docs/contracts.md §37, D7)
# --------------------------------------------------------------------------- #


class Approver(Protocol):
    """Single approval seam. Consulted for ASK only; never for DENY."""

    def confirm(self, request: PermissionRequest) -> bool: ...


class AutoApprover:
    """``--yes``: auto-approves every ASK that reaches it (DENY never does)."""

    def confirm(self, request: PermissionRequest) -> bool:
        return True


class DenyAllApprover:
    """Non-interactive without ``--yes``: ASK is treated as denied (D7)."""

    def confirm(self, request: PermissionRequest) -> bool:
        return False


class CliApprover:
    """Interactive approver.

    * ``auto_yes`` (``--yes``) approves ASK without prompting.
    * Without a TTY and without ``--yes``, ASK is denied with a clear message.
    * Otherwise prompts on stdin.
    """

    def __init__(
        self,
        *,
        auto_yes: bool = False,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
        is_tty_fn: Callable[[], bool] = lambda: sys.stdin.isatty(),
    ) -> None:
        self._auto_yes = auto_yes
        self._input_fn = input_fn
        self._output_fn = output_fn
        self._is_tty_fn = is_tty_fn

    def confirm(self, request: PermissionRequest) -> bool:
        if self._auto_yes:
            return True
        if not self._is_tty_fn():
            self._output_fn(
                f"[denied] {request.tool_name}: {request.reason} "
                "(non-interactive; pass --yes to auto-approve ASK)"
            )
            return False
        self._output_fn(
            f"[approval required] {request.tool_name} "
            f"({request.permission_class.value}, risk={request.risk.value}): {request.reason}"
        )
        answer = self._input_fn("Approve? [y/N] ").strip().lower()
        return answer in {"y", "yes"}
