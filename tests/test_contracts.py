"""Contract data-shape tests (docs/contracts.md §31–§35, D15)."""

from __future__ import annotations

import datetime as dt
import json

from coding_agent.contracts import (
    TERMINAL_STATES,
    AgentSession,
    AgentTurn,
    ErrorCode,
    Limits,
    MessageContent,
    ModelRef,
    ModelRequest,
    ModelResponse,
    SessionState,
    StopReason,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)


def test_limits_defaults_are_finalized_values() -> None:
    limits = Limits()
    assert limits.max_turns == 20
    assert limits.max_tool_calls == 50
    assert limits.max_time_s == 600.0
    assert limits.max_repeated_failures == 3


def test_terminal_states() -> None:
    assert TERMINAL_STATES == frozenset(
        {
            SessionState.COMPLETED,
            SessionState.COMPLETED_UNVERIFIED,
            SessionState.INTERRUPTED,
            SessionState.FAILED,
        }
    )
    # TOOL_DENIED is explicitly *not* terminal (contracts §31).
    assert SessionState.TOOL_DENIED not in TERMINAL_STATES


def test_d23_reserved_states_are_nonterminal_vocabulary() -> None:
    # D23 reserves EXECUTING_TOOL, WAITING_FOR_USER, and TOOL_DENIED as conceptual
    # sub-states of the PERMISSION_CHECK phase that the v0.2 runtime never assigns.
    # Pin their membership, exact .value identity strings, and non-terminality (§31).
    reserved = {
        SessionState.EXECUTING_TOOL: "EXECUTING_TOOL",
        SessionState.WAITING_FOR_USER: "WAITING_FOR_USER",
        SessionState.TOOL_DENIED: "TOOL_DENIED",
    }
    for state, value in reserved.items():
        assert state in SessionState  # member exists
        assert state.value == value  # exact identity string
        assert state not in TERMINAL_STATES  # non-terminal (contracts §31, D4)


def test_string_enums_serialize_by_value() -> None:
    assert StopReason.TOOL_CALL == "tool_call"
    assert StopReason.TOOL_CALL.value == "tool_call"
    assert SessionState.COMPLETED.value == "COMPLETED"
    assert SessionState.COMPLETED_UNVERIFIED.value == "COMPLETED_UNVERIFIED"
    assert ToolResultStatus.SUCCESS == "success"
    assert ErrorCode.EDIT_NOT_UTF8.value == "EDIT_NOT_UTF8"


def test_tool_result_write_flag() -> None:
    write = ToolResult(
        tool_call_id="c", status=ToolResultStatus.SUCCESS, metadata={"counts_as_write": True}
    )
    assert write.counts_as_write() is True
    assert write.is_success is True
    non_write = ToolResult(tool_call_id="c", status=ToolResultStatus.SUCCESS)
    assert non_write.counts_as_write() is False


def test_session_and_turn_to_dict_are_json_serializable() -> None:
    session = AgentSession(session_id="s1", request="do it", repo_root="/r", working_dir="/r")
    turn = AgentTurn(turn_id="t1", session_id="s1", index=0, started_at=dt.datetime(2026, 1, 1))
    session.history.append(turn)

    payload = json.dumps(session.to_dict())  # must not raise
    assert "s1" in payload
    assert session.to_dict()["state"] == "CREATED"
    assert session.to_dict()["history"][0]["turn_id"] == "t1"
    assert session.to_dict()["history"][0]["started_at"] == "2026-01-01T00:00:00"


# --- corrections 3/4: to_dict includes model_request/model_response/tool_call_history --- #


def _sample_request() -> ModelRequest:
    return ModelRequest(
        request_id="q1",
        session_id="s1",
        turn_id="t1",
        model=ModelRef(provider_id="mock", model_id="mock-v1"),
        system_instructions="sys",
    )


def _sample_response() -> ModelResponse:
    return ModelResponse(
        response_id="r1",
        stop_reason=StopReason.COMPLETED,
        content=[MessageContent.text_block("hi")],
    )


def test_turn_to_dict_includes_model_request_and_response() -> None:
    turn = AgentTurn(
        turn_id="t1",
        session_id="s1",
        index=0,
        started_at=dt.datetime(2026, 1, 1),
        model_request=_sample_request(),
        model_response=_sample_response(),
        final_response="hi",
    )
    d = turn.to_dict()
    assert d["model_request"]["request_id"] == "q1"
    assert d["model_request"]["model"] == {"provider_id": "mock", "model_id": "mock-v1"}
    assert d["model_response"]["stop_reason"] == "completed"
    assert d["model_response"]["content"] == [{"type": "text", "text": "hi"}]
    json.dumps(d)  # must not raise


def test_turn_to_dict_model_fields_present_when_unset() -> None:
    turn = AgentTurn(turn_id="t", session_id="s", index=0, started_at=dt.datetime(2026, 1, 1))
    d = turn.to_dict()
    assert "model_request" in d
    assert d["model_request"] is None
    assert "model_response" in d
    assert d["model_response"] is None


def test_session_to_dict_includes_tool_call_history() -> None:
    call = ToolCall(tool_call_id="c1", name="read_file", arguments={"path": "a"})
    session = AgentSession(session_id="s1", request="do", repo_root="/r", working_dir="/r")
    session.tool_call_history.append(call)
    d = session.to_dict()
    assert d["tool_call_history"] == [
        {"tool_call_id": "c1", "name": "read_file", "arguments": {"path": "a"}}
    ]
    json.dumps(d)  # must not raise


def test_fully_populated_session_to_dict_is_json_serializable() -> None:
    call = ToolCall(tool_call_id="c1", name="edit_file", arguments={"path": "a"})
    turn = AgentTurn(
        turn_id="t1",
        session_id="s1",
        index=0,
        started_at=dt.datetime(2026, 1, 1),
        model_request=_sample_request(),
        model_response=_sample_response(),
        tool_calls=[call],
        final_response="hi",
        ended_at=dt.datetime(2026, 1, 1, 0, 1),
    )
    session = AgentSession(session_id="s1", request="do", repo_root="/r", working_dir="/r")
    session.history.append(turn)
    session.tool_call_history.append(call)
    payload = json.dumps(session.to_dict())  # must not raise
    assert "tool_call_history" in payload
    assert "model_request" in payload
    assert "model_response" in payload
