"""PermissionPolicy + Approver tests (docs/contracts.md §16, §37; D7)."""

from __future__ import annotations

import pytest

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


def _ask_request(name: str, arguments: dict) -> PermissionRequest:
    definitions = _definitions()
    call = ToolCall("c1", name, arguments)
    decision = PermissionPolicy().check(definitions[name], call)
    assert decision.decision is ASK
    return build_permission_request(definitions[name], call, decision, "perm1")


def test_build_permission_request_maps_risk() -> None:
    defs = _definitions()
    call = ToolCall("c1", "edit_file", {"path": "x", "old_text": "a", "new_text": "b"})
    request = build_permission_request(
        defs["edit_file"], call, PermissionDecision(ASK, "write needs approval"), "perm1"
    )
    assert request.tool_name == "edit_file"
    assert request.permission_class is PermissionClass.WRITE
    assert request.risk is RiskLevel.MEDIUM
    assert request.reason == "write needs approval [path: 'x']"


def test_permission_request_shows_shell_command() -> None:
    request = _ask_request("shell", {"command": "pytest -q tests"})
    assert "command: 'pytest -q tests'" in request.reason


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("edit_file", {"path": "src/app.py", "old_text": "a", "new_text": "b"}),
        ("write_file", {"path": "src/new.py", "content": "new contents"}),
        ("delete_file", {"path": "src/old.py"}),
    ],
)
def test_permission_request_shows_target_path(name: str, arguments: dict) -> None:
    request = _ask_request(name, arguments)
    assert f"path: {arguments['path']!r}" in request.reason


@pytest.mark.parametrize(
    ("name", "arguments", "secrets"),
    [
        (
            "edit_file",
            {"path": "src/app.py", "old_text": "OLD_SECRET", "new_text": "NEW_SECRET"},
            ("OLD_SECRET", "NEW_SECRET"),
        ),
        (
            "write_file",
            {"path": "src/new.py", "content": "CONTENT_SECRET"},
            ("CONTENT_SECRET",),
        ),
    ],
)
def test_permission_request_never_shows_file_contents(
    name: str, arguments: dict, secrets: tuple[str, ...]
) -> None:
    request = _ask_request(name, arguments)
    assert arguments["path"] in request.reason
    for secret in secrets:
        assert secret not in request.reason


def test_permission_request_escapes_control_characters() -> None:
    command = "npm test\x1b[31m\nrm -rf /tmp\x00\u202e"
    reason = _ask_request("shell", {"command": command}).reason
    assert "\x1b" not in reason
    assert "\n" not in reason
    assert "\x00" not in reason
    assert "\u202e" not in reason
    assert "\\x1b[31m" in reason
    assert "\\n" in reason
    assert "\\x00" in reason
    assert "\\u202e" in reason


@pytest.mark.parametrize(
    ("character", "escape"),
    [
        ("\u034f", "\\u034f"),  # COMBINING GRAPHEME JOINER (zero-width)
        ("\ufe0f", "\\ufe0f"),  # VARIATION SELECTOR-16 (invisible)
        ("\u3164", "\\u3164"),  # HANGUL FILLER (blank)
    ],
)
def test_invisible_unicode_is_escaped_in_request_and_prompt(
    character: str, escape: str
) -> None:
    # These survive repr() raw, so approval must escape them explicitly to keep
    # the displayed target from being visually disguised.
    request = _ask_request("delete_file", {"path": f"important{character}.txt"})
    assert character not in request.reason
    assert escape in request.reason

    out: list[str] = []
    approver = CliApprover(input_fn=lambda p: "n", output_fn=out.append, is_tty_fn=lambda: True)
    assert approver.confirm(request) is False
    rendered = "\n".join(out)
    assert character not in rendered
    assert escape in rendered


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("shell", {"command": "pytest -q tests"}, "pytest -q tests"),
        (
            "edit_file",
            {"path": "src/app.py", "old_text": "OLD_SECRET", "new_text": "NEW_SECRET"},
            "src/app.py",
        ),
        ("write_file", {"path": "notes.txt", "content": "CONTENT_SECRET"}, "notes.txt"),
        ("delete_file", {"path": "old.txt"}, "old.txt"),
    ],
)
def test_cli_approver_prompt_shows_action_without_contents(
    name: str, arguments: dict, expected: str
) -> None:
    definitions = _definitions()
    call = ToolCall("c1", name, arguments)
    decision = PermissionPolicy().check(definitions[name], call)
    request = build_permission_request(definitions[name], call, decision, "perm1")
    out: list[str] = []
    approver = CliApprover(input_fn=lambda p: "n", output_fn=out.append, is_tty_fn=lambda: True)
    assert approver.confirm(request) is False
    rendered = "\n".join(out)
    assert expected in rendered
    for secret in ("OLD_SECRET", "NEW_SECRET", "CONTENT_SECRET"):
        assert secret not in rendered
