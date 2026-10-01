"""ContextBuilder chronology tests (docs/contracts.md §39; D11 failure path).

Correction 5: a verification result must be rendered immediately after the
final-answer turn that triggered it — not batched after all turns — so the model
sees the true causal order and a failed verification is visible to the next turn.
"""

from __future__ import annotations

import datetime as dt

from coding_agent.context import ContextBuilder
from coding_agent.contracts import (
    AgentSession,
    AgentTurn,
    MessageContent,
    ModelRef,
    ModelResponse,
    StopReason,
    ToolCall,
    ToolResult,
    ToolResultStatus,
    VerificationResult,
)


def _session(request: str = "fix the bug") -> AgentSession:
    return AgentSession(session_id="s1", request=request, repo_root="/r", working_dir="/r")


def _final_turn(turn_id: str, index: int, text: str) -> AgentTurn:
    """A final-answer turn (no tool calls) — the only kind that triggers verify."""
    return AgentTurn(
        turn_id=turn_id,
        session_id="s1",
        index=index,
        started_at=dt.datetime(2026, 1, 1),
        model_response=ModelResponse(
            response_id=turn_id,
            stop_reason=StopReason.COMPLETED,
            content=[MessageContent.text_block(text)],
        ),
        final_response=text,
    )


def _tool_turn(turn_id: str, index: int, marker: str) -> AgentTurn:
    """A tool-call turn (has tool calls) — never triggers verification."""
    call = ToolCall(tool_call_id="c" + turn_id, name="edit_file", arguments={"m": marker})
    result = ToolResult(
        tool_call_id=call.tool_call_id,
        status=ToolResultStatus.SUCCESS,
        output={"marker": marker},
    )
    return AgentTurn(
        turn_id=turn_id,
        session_id="s1",
        index=index,
        started_at=dt.datetime(2026, 1, 1),
        model_response=ModelResponse(
            response_id=turn_id, stop_reason=StopReason.TOOL_CALL, tool_calls=[call]
        ),
        tool_calls=[call],
        tool_results=[result],
    )


def _verification(passed: bool, marker: str) -> VerificationResult:
    return VerificationResult(
        passed=passed,
        command="pytest",
        output=marker,
        exit_code=0 if passed else 1,
        ran_at=dt.datetime(2026, 1, 1),
        timed_out=False,
    )


def _message_texts(session: AgentSession) -> list[str]:
    request = ContextBuilder(ModelRef(provider_id="mock", model_id="m")).build(session, [])
    return ["\n".join(block.text for block in message.content) for message in request.messages]


def _first(texts: list[str], marker: str) -> int:
    return next(i for i, text in enumerate(texts) if marker in text)


def _last(texts: list[str], marker: str) -> int:
    return max(i for i, text in enumerate(texts) if marker in text)


# --- required: failed-verification retry chronology --- #


def test_failed_verification_rendered_between_first_answer_and_next_turn() -> None:
    session = _session()
    session.history.append(_final_turn("t1", 0, "ATTEMPT_ONE"))
    session.history.append(_tool_turn("t2", 1, "DIAGNOSE_EDIT"))
    session.verification_history.append(_verification(passed=False, marker="VERIFY_FAILED_MARKER"))

    texts = _message_texts(session)
    attempt_idx = _last(texts, "ATTEMPT_ONE")
    verify_idx = _first(texts, "[verification FAILED]")
    diagnose_idx = _first(texts, "DIAGNOSE_EDIT")

    # Causal order: first final answer -> its failed verification -> second turn.
    assert attempt_idx < verify_idx < diagnose_idx
    # The failed verification is the immediate context for the next model turn.
    assert "VERIFY_FAILED_MARKER" in texts[verify_idx]
    # The user request still opens the conversation.
    assert texts[0] == "fix the bug"


def test_verification_rendered_exactly_once() -> None:
    session = _session()
    session.history.append(_final_turn("t1", 0, "ATTEMPT_ONE"))
    session.history.append(_tool_turn("t2", 1, "DIAGNOSE_EDIT"))
    session.verification_history.append(_verification(passed=False, marker="VERIFY_FAILED_MARKER"))
    texts = _message_texts(session)
    # No duplication: the single verification appears exactly once.
    assert sum("[verification FAILED]" in text for text in texts) == 1


def test_multiple_verifications_align_with_their_own_turns() -> None:
    session = _session()
    session.history.append(_final_turn("t1", 0, "ATTEMPT_ONE"))
    session.history.append(_tool_turn("t2", 1, "FIX_ONE"))
    session.history.append(_final_turn("t3", 2, "ATTEMPT_TWO"))
    session.verification_history.append(_verification(passed=False, marker="FIRST_FAIL"))
    session.verification_history.append(_verification(passed=True, marker="SECOND_PASS"))

    texts = _message_texts(session)
    # First failure sits after ATTEMPT_ONE and before the FIX_ONE turn.
    assert _first(texts, "ATTEMPT_ONE") < _first(texts, "[verification FAILED]")
    assert _first(texts, "[verification FAILED]") < _first(texts, "FIX_ONE")
    # Second (passing) verification sits after ATTEMPT_TWO.
    assert _first(texts, "ATTEMPT_TWO") < _first(texts, "[verification PASSED]")
    assert "SECOND_PASS" in texts[_first(texts, "[verification PASSED]")]


def test_tool_call_turn_never_renders_a_verification() -> None:
    session = _session()
    session.history.append(_tool_turn("t1", 0, "ONLY_TOOL"))
    session.verification_history.append(_verification(passed=False, marker="SHOULD_NOT_APPEAR"))
    texts = _message_texts(session)
    # No final-answer turn => nothing triggers verification => none is rendered.
    assert all("[verification" not in text for text in texts)
