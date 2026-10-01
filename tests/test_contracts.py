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
    SessionState,
    StopReason,
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
