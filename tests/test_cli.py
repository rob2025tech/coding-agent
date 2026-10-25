"""CLI tests: arg parsing, limit resolution, runtime assembly, exit codes (§42)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.cli import (
    _build_provider,
    _exit_code,
    _resolve_limits,
    build_arg_parser,
    build_runtime,
    main,
)
from coding_agent.contracts import Limits, SessionState
from coding_agent.events import NullEventSink
from coding_agent.providers.http import HttpModelProvider
from coding_agent.providers.mock import MockModelProvider
from coding_agent.runtime import AgentRuntime
from coding_agent.verification import DEFAULT_VERIFY_TIMEOUT_S

# --- argument parsing --- #


def test_defaults() -> None:
    args = build_arg_parser().parse_args([])
    assert args.request == ""
    assert args.repo == "."
    assert args.provider == "mock"
    assert args.verify is None
    assert args.verify_timeout == DEFAULT_VERIFY_TIMEOUT_S == 120.0
    assert args.yes is False
    # Limit overrides default to None so _resolve_limits falls back to Limits().
    assert args.max_turns is None
    assert args.max_tool_calls is None
    assert args.max_time_s is None


def test_verify_flags_parse() -> None:
    args = build_arg_parser().parse_args(
        ["--verify", "pytest -q", "--verify-timeout", "30", "--yes", "fix it"]
    )
    assert args.verify == "pytest -q"
    assert args.verify_timeout == 30.0
    assert args.yes is True
    assert args.request == "fix it"


def test_provider_choices_reject_unknown() -> None:
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--provider", "openai", "x"])


def test_build_provider_mock_and_unknown() -> None:
    assert isinstance(_build_provider("mock"), MockModelProvider)
    with pytest.raises(SystemExit):
        _build_provider("nope")


def test_provider_http_choice_is_accepted() -> None:
    args = build_arg_parser().parse_args(["--provider", "http", "x"])
    assert args.provider == "http"


def test_build_provider_http_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_ENDPOINT", "https://provider.invalid/v1/chat/completions")
    monkeypatch.setenv("CODING_AGENT_MODEL", "some-model")
    provider = _build_provider("http")
    assert isinstance(provider, HttpModelProvider)
    assert provider.describe().model_id == "some-model"


def test_build_provider_http_requires_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODING_AGENT_ENDPOINT", raising=False)
    monkeypatch.delenv("CODING_AGENT_MODEL", raising=False)
    with pytest.raises(SystemExit):
        _build_provider("http")


# --- limit resolution (D15: centralized defaults, no scattered magic numbers) --- #


def test_resolve_limits_defaults() -> None:
    args = build_arg_parser().parse_args([])
    assert _resolve_limits(args) == Limits()
    assert _resolve_limits(args) == Limits(
        max_turns=20, max_tool_calls=50, max_time_s=600.0, max_repeated_failures=3
    )


def test_resolve_limits_overrides() -> None:
    args = build_arg_parser().parse_args(
        ["--max-turns", "5", "--max-tool-calls", "9", "--max-time", "12.5", "x"]
    )
    resolved = _resolve_limits(args)
    assert resolved.max_turns == 5
    assert resolved.max_tool_calls == 9
    assert resolved.max_time_s == 12.5
    # Not overridable from the CLI; always the centralized default.
    assert resolved.max_repeated_failures == Limits().max_repeated_failures


# --- runtime assembly --- #


def test_build_runtime_assembles_session(repo: Path) -> None:
    args = build_arg_parser().parse_args(
        ["--repo", str(repo), "--verify", "true", "--verify-timeout", "5", "--yes", "task"]
    )
    runtime, session = build_runtime(args, event_sink=NullEventSink())
    assert isinstance(runtime, AgentRuntime)
    assert Path(session.repo_root).samefile(repo)
    assert Path(session.working_dir).samefile(repo)
    assert session.request == "task"
    assert session.limits == Limits()
    assert session.state is SessionState.CREATED


def test_build_runtime_accepts_injected_provider(repo: Path) -> None:
    args = build_arg_parser().parse_args(["--repo", str(repo), "task"])
    provider = MockModelProvider([])
    runtime, _session = build_runtime(
        args, event_sink=NullEventSink(), provider=provider
    )
    assert isinstance(runtime, AgentRuntime)


# --- exit-code mapping --- #


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (SessionState.COMPLETED, 0),
        (SessionState.COMPLETED_UNVERIFIED, 0),
        (SessionState.INTERRUPTED, 1),
        (SessionState.FAILED, 1),
    ],
)
def test_exit_code_mapping(state: SessionState, code: int) -> None:
    assert _exit_code(state) == code


# --- end-to-end through main() (deterministic mock; no network/API key) --- #


def test_main_noop_completes_zero(repo: Path) -> None:
    # Empty mock script -> immediate final answer, no writes -> COMPLETED -> exit 0.
    assert main(["--repo", str(repo), "--quiet", "do nothing"]) == 0


def test_main_time_limit_exits_nonzero(repo: Path) -> None:
    # --max-time 0 trips the wall-time limit before any model call -> INTERRUPTED -> 1.
    assert main(["--repo", str(repo), "--quiet", "--max-time", "0", "task"]) == 1


def test_main_prints_final_answer_to_stdout(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CLI-boundary regression: a successful run must surface the model's final
    # answer on stdout, while stderr keeps carrying only event/status telemetry.
    from coding_agent.providers.mock import final_response

    def _scripted(_name: str) -> MockModelProvider:
        return MockModelProvider([final_response("FINAL-ANSWER-OK")])

    monkeypatch.setattr("coding_agent.cli._build_provider", _scripted)
    assert main(["--repo", str(repo), "task"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "FINAL-ANSWER-OK"
    # Telemetry stays on stderr and the answer is never duplicated into it.
    assert "FINAL-ANSWER-OK" not in captured.err
    assert "COMPLETED" in captured.err


def test_main_recovery_flow_prints_only_corrected_answer(
    repo: Path,
    pycmd,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deterministic CLI recovery acceptance: wrong edit -> verify FAIL -> failure
    # feedback reaches the next model request -> corrective edit -> verify PASS ->
    # stdout carries ONLY the corrected final answer (kills forward traversal of
    # _final_answer(); the intermediate answer must never surface).
    from coding_agent.providers.mock import final_response, tool_call_response

    provider = MockModelProvider(
        [
            tool_call_response(
                "edit_file",
                {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('WRONG')"},
            ),
            final_response("first attempt: WRONG-INTERMEDIATE"),
            tool_call_response(
                "edit_file",
                {"path": "hello.py", "old_text": "print('WRONG')", "new_text": "print('TARGET')"},
            ),
            final_response("CORRECTED-FINAL-ANSWER"),
        ]
    )
    monkeypatch.setattr("coding_agent.cli._build_provider", lambda _name: provider)
    verify = pycmd(
        "import pathlib,sys; sys.exit(0 if 'TARGET' in pathlib.Path('hello.py').read_text() else 1)"
    )

    assert main(["--repo", str(repo), "--yes", "--verify", verify, "fix hello"]) == 0
    captured = capsys.readouterr()
    # Exactly the corrected final answer on stdout; the intermediate one never appears.
    assert captured.out.strip() == "CORRECTED-FINAL-ANSWER"
    assert "WRONG-INTERMEDIATE" not in captured.out
    # Telemetry stays on stderr; the answer is not duplicated into it.
    assert "CORRECTED-FINAL-ANSWER" not in captured.err
    assert "verified" in captured.err
    # Both verification outcomes are observable through the real CLI event stream.
    assert "VerificationFailed" in captured.err
    assert "VerificationPassed" in captured.err
    # Four model turns: bad edit, first answer, corrective edit, corrected answer.
    assert len(provider.requests) == 4
    # The request following the failed verification carries the rendered failure
    # feedback (ContextBuilder's existing representation) back to the model.
    feedback = "".join(
        block.text for msg in provider.requests[2].messages for block in msg.content
    )
    assert "[verification FAILED]" in feedback


def test_main_end_to_end_coding_workflow(
    repo: Path,
    pycmd,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Practical coding-workflow acceptance, one CLI invocation, offline and
    # deterministic: inspect (search) -> edit the located source -> authoritative
    # verification FAILS on the premature answer -> failure feedback reaches the
    # model -> corrective edit -> verification PASSES -> only the corrected final
    # answer surfaces. Verified terminal representation in this repo's reserved
    # §40 vocabulary is COMPLETED + completed_status "verified" (runtime §27);
    # there is no COMPLETED_VERIFIED member and this test does not invent one.
    from coding_agent.providers.mock import final_response, tool_call_response

    provider = MockModelProvider(
        [
            tool_call_response("search", {"pattern": "Hello"}),
            tool_call_response(
                "edit_file",
                {
                    "path": "hello.py",
                    "old_text": "print('Hello')",
                    "new_text": "print('Half-fixed')",
                },
            ),
            final_response("attempt 1: PARTIAL-INTERMEDIATE-ANSWER"),
            tool_call_response(
                "edit_file",
                {
                    "path": "hello.py",
                    "old_text": "print('Half-fixed')",
                    "new_text": "print('Fully-fixed')",
                },
            ),
            final_response("E2E-FIX-COMPLETED"),
        ]
    )
    monkeypatch.setattr("coding_agent.cli._build_provider", lambda _name: provider)
    verify = pycmd(
        "import pathlib,sys; "
        "sys.exit(0 if 'Fully-fixed' in pathlib.Path('hello.py').read_text() else 1)"
    )

    # Turn 1 locates the code, turn 2 edits it, turn 3 answers prematurely and
    # fails verification, turn 4 corrects, turn 5 answers and verifies clean.
    assert main(["--repo", str(repo), "--yes", "--verify", verify, "fix the greeting"]) == 0
    captured = capsys.readouterr()

    # The source file on disk was actually changed by the workflow.
    content = (repo / "hello.py").read_text()
    assert "Fully-fixed" in content
    assert "Hello" not in content

    # stdout carries exactly the corrected final answer; the intermediate never leaks.
    assert captured.out.strip() == "E2E-FIX-COMPLETED"
    assert "PARTIAL-INTERMEDIATE-ANSWER" not in captured.out

    # The verification gate failed first, then passed, in that order, and the
    # session ended as a verified completion.
    assert "VerificationFailed" in captured.err
    assert "VerificationPassed" in captured.err
    assert captured.err.index("VerificationFailed") < captured.err.index("VerificationPassed")
    assert "COMPLETED" in captured.err
    assert "verified" in captured.err

    # Five model turns; the inspection result and the verification failure were
    # both fed back to the model before it performed the corrective edit.
    assert len(provider.requests) == 5
    inspect_feedback = "".join(
        block.text for msg in provider.requests[1].messages for block in msg.content
    )
    assert "hello.py" in inspect_feedback
    failure_feedback = "".join(
        block.text for msg in provider.requests[3].messages for block in msg.content
    )
    assert "[verification FAILED]" in failure_feedback
