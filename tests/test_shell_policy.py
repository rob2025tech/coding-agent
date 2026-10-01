"""Shell authorization policy tests (D5/D16, R1/R2/R5; architecture §4.8).

These assert the *decision* only; execution with ``shell=False`` is covered in
``test_tools.py``.
"""

from __future__ import annotations

import pytest

from coding_agent.contracts import PermissionDecisionValue
from coding_agent.permissions import classify_shell_command

ALLOW = PermissionDecisionValue.ALLOW
ASK = PermissionDecisionValue.ASK
DENY = PermissionDecisionValue.DENY


def decision(command: str) -> PermissionDecisionValue:
    return classify_shell_command(command).decision


# --- exact-argv read-only allowlist (R1) --- #


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status --short",
        "git diff",
        "git diff --stat",
        "git log --oneline -n 20",
    ],
)
def test_allowlist_exact_argv(command: str) -> None:
    assert decision(command) is ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "git status --extra",  # not the exact allowlisted argv
        "git diff HEAD",
        "git log --oneline -n 50",
        "git push origin main",
        "git commit -m x",
    ],
)
def test_allowlist_is_exact_only(command: str) -> None:
    assert decision(command) is ASK


# --- metacharacters -> ASK --- #


@pytest.mark.parametrize(
    "command",
    [
        "git status | grep foo",
        "echo a && echo b",
        "cat foo; cat bar",
        "ls > out.txt",
        "ls < in.txt",
        "echo $(whoami)",
        "echo `id`",
        "run a (b)",
    ],
)
def test_metacharacters_force_ask(command: str) -> None:
    assert decision(command) is ASK


# --- DENY: network + privilege tools (D16) --- #


@pytest.mark.parametrize(
    "command",
    ["curl http://x", "wget http://x", "ssh host", "scp a b", "nc host 80", "ncat host 80"],
)
def test_network_tools_denied(command: str) -> None:
    assert decision(command) is DENY


@pytest.mark.parametrize("command", ["sudo ls", "su root", "/usr/bin/sudo -i"])
def test_privilege_tools_denied(command: str) -> None:
    assert decision(command) is DENY


def test_deny_takes_precedence_over_metacharacters() -> None:
    # curl is DENY even though the command also has a pipe (which alone -> ASK).
    assert decision("curl http://x | grep y") is DENY


# --- DENY: recursive + force rm (D16, R2) --- #


@pytest.mark.parametrize(
    "command",
    ["rm -rf /tmp/x", "rm -fr build", "rm -r -f build", "rm -Rf x", "rm --recursive --force x"],
)
def test_rm_recursive_and_force_denied(command: str) -> None:
    assert decision(command) is DENY


@pytest.mark.parametrize(
    "command", ["rm -r build", "rm -f file.txt", "rm file.txt", "rm --force x"]
)
def test_rm_without_both_flags_is_ask(command: str) -> None:
    # R2: rm -r alone or rm -f alone is NOT deny -> falls through to ASK.
    assert decision(command) is ASK


# --- DENY: credential files (D16, R5) --- #


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "cat app/.env.local",
        "less ~/.ssh/id_rsa",
        "cat .aws/credentials",
        "vim .netrc",
        "cat .git-credentials",
        "tar czf x.tgz .ssh",
    ],
)
def test_credential_access_denied(command: str) -> None:
    assert decision(command) is DENY


def test_credential_list_is_not_over_expanded() -> None:
    # R5: the list stays as enumerated for v0.1; unrelated dotted files are not DENY.
    assert decision("cat notes.txt") is ASK
    assert decision("cat config.json") is ASK


# --- everything else -> ASK --- #


@pytest.mark.parametrize(
    "command", ["ls -la", "pytest", "python x.py", "make build", "npm install"]
)
def test_non_allowlisted_is_ask(command: str) -> None:
    assert decision(command) is ASK


# --- degenerate inputs --- #


def test_empty_command_is_ask() -> None:
    assert decision("") is ASK
    assert decision("   ") is ASK


def test_unparseable_command_is_ask() -> None:
    # Unbalanced quote -> shlex fails -> cannot allowlist -> ASK.
    assert decision('echo "unclosed') is ASK
