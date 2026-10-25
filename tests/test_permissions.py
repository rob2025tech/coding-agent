"""PermissionPolicy + Approver tests (docs/contracts.md §16, §37; D7)."""

from __future__ import annotations

from coding_agent.contracts import (
    PermissionClass,
    PermissionDecision,
    PermissionDecisionValue,
    PermissionRequest,
    RiskLevel,
    SideEffect,
    ToolCall,
    ToolDefinition,
)
from coding_agent.permissions import (
    AutoApprover,
    CliApprover,
    DenyAllApprover,
    PermissionPolicy,
    build_permission_request,
)
from coding_agent.tools.registry import default_registry

ALLOW = PermissionDecisionValue.ALLOW
ASK = PermissionDecisionValue.ASK
DENY = PermissionDecisionValue.DENY


def _definitions() -> dict[str, ToolDefinition]:
    return {tool.definition.name: tool.definition for tool in default_registry()}


def _request(tool: str = "shell") -> PermissionRequest:
    return PermissionRequest(
        permission_request_id="p1",
        tool_call_id="c1",
        tool_name=tool,
        permission_class=PermissionClass.EXECUTE,
        reason="needs approval",
        risk=RiskLevel.MEDIUM,
    )


def test_read_tools_are_allowed() -> None:
    policy = PermissionPolicy()
    defs = _definitions()
    for name in ("list_files", "read_file", "search"):
        decision = policy.check(defs[name], ToolCall("c1", name, {}))
        assert decision.decision is ALLOW


def test_edit_file_is_ask() -> None:
    policy = PermissionPolicy()
    defs = _definitions()
    call = ToolCall("c1", "edit_file", {"path": "x", "old_text": "a", "new_text": "b"})
    assert policy.check(defs["edit_file"], call).decision is ASK


def test_write_file_is_ask() -> None:
    # write_file is a WRITE-class tool (D20 / §41) -> ASK, the same gate as edit_file.
    policy = PermissionPolicy()
    defs = _definitions()
    assert defs["write_file"].permission is PermissionClass.WRITE
    call = ToolCall("c1", "write_file", {"path": "x.txt", "content": "hi"})
    assert policy.check(defs["write_file"], call).decision is ASK


def test_delete_file_is_ask() -> None:
    # delete_file is a DESTRUCTIVE-class tool (D25 / §43) -> ASK via the policy
    # class mapping, not a name hardcode (§13: capability-based decisions).
    policy = PermissionPolicy()
    defs = _definitions()
    assert defs["delete_file"].permission is PermissionClass.DESTRUCTIVE
    assert defs["delete_file"].side_effect is SideEffect.DESTRUCTIVE
    decision = policy.check(defs["delete_file"], ToolCall("c1", "delete_file", {"path": "x"}))
    assert decision.decision is ASK
    assert decision.reason == "destructive operation requires approval"


def test_shell_delegates_to_classifier() -> None:
    policy = PermissionPolicy()
    shell = _definitions()["shell"]
    assert policy.check(shell, ToolCall("c", "shell", {"command": "git status"})).decision is ALLOW
    assert policy.check(shell, ToolCall("c", "shell", {"command": "curl x"})).decision is DENY
    assert policy.check(shell, ToolCall("c", "shell", {"command": "ls"})).decision is ASK


def test_auto_and_deny_approvers() -> None:
    assert AutoApprover().confirm(_request()) is True
    assert DenyAllApprover().confirm(_request()) is False


def test_cli_approver_yes_never_prompts() -> None:
    prompted: list[str] = []
    approver = CliApprover(
        auto_yes=True,
        input_fn=lambda p: prompted.append(p) or "y",
        output_fn=lambda m: None,
        is_tty_fn=lambda: True,
    )
    assert approver.confirm(_request()) is True
    assert prompted == []


def test_cli_approver_non_interactive_denies_with_message() -> None:
    out: list[str] = []
    approver = CliApprover(
        auto_yes=False, input_fn=lambda p: "y", output_fn=out.append, is_tty_fn=lambda: False
    )
    assert approver.confirm(_request()) is False
    assert any("non-interactive" in message for message in out)


def test_cli_approver_interactive_prompts() -> None:
    yes = CliApprover(input_fn=lambda p: "y", output_fn=lambda m: None, is_tty_fn=lambda: True)
    no = CliApprover(input_fn=lambda p: "n", output_fn=lambda m: None, is_tty_fn=lambda: True)
    assert yes.confirm(_request()) is True
    assert no.confirm(_request()) is False


def test_build_permission_request_maps_risk() -> None:
    defs = _definitions()
    call = ToolCall("c1", "edit_file", {"path": "x", "old_text": "a", "new_text": "b"})
    request = build_permission_request(
        defs["edit_file"], call, PermissionDecision(ASK, "write needs approval"), "perm1"
    )
    assert request.tool_name == "edit_file"
    assert request.permission_class is PermissionClass.WRITE
    assert request.risk is RiskLevel.MEDIUM
    assert request.reason == "write needs approval"
