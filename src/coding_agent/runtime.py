"""The agent loop (architecture §5, docs/contracts.md §27).

Orchestrates: build context -> model generate -> tool calls (via the executor) ->
final answer -> runtime-owned VERIFICATION_CHECK -> terminal state. Enforces the
finalized limits (D15) and the D12 write/staleness rule.

Limit -> terminal-state mapping (an implementation choice within §34's
"INTERRUPTED or FAILED"): turns / tool-calls / wall-time -> INTERRUPTED;
repeated failures -> FAILED.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any

from coding_agent.context import ContextBuilder
from coding_agent.contracts import (
    AgentSession,
    AgentTurn,
    ProviderError,
    SessionState,
    ToolCall,
    ToolResultStatus,
)
from coding_agent.errors import normalize_provider_failure
from coding_agent.events import AgentEvent, EventSink, EventType
from coding_agent.executor import ToolExecutor
from coding_agent.providers.base import ModelProvider
from coding_agent.tools.registry import ToolRegistry
from coding_agent.verification import VerificationTracker, Verifier


class AgentRuntime:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        context_builder: ContextBuilder,
        verifier: Verifier,
        event_sink: EventSink,
        tracker: VerificationTracker | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._executor = executor
        self._context_builder = context_builder
        self._verifier = verifier
        self._event_sink = event_sink
        self._tracker = tracker or VerificationTracker()

    async def run(self, session: AgentSession) -> AgentSession:
        tracker = self._tracker
        limits = session.limits
        session.state = SessionState.RUNNING
        self._emit(session, EventType.SESSION_STARTED, None, request=session.request)

        started = time.monotonic()
        turn_index = 0
        tool_call_count = 0
        consecutive_failures = 0

        while True:
            # --- limits (checked before each model turn) ---
            if turn_index >= limits.max_turns:
                return self._terminate(session, SessionState.INTERRUPTED, "max_turns exceeded")
            if tool_call_count >= limits.max_tool_calls:
                return self._terminate(session, SessionState.INTERRUPTED, "max_tool_calls exceeded")
            if (time.monotonic() - started) >= limits.max_time_s:
                return self._terminate(session, SessionState.INTERRUPTED, "max_time_s exceeded")
            if consecutive_failures >= limits.max_repeated_failures:
                return self._terminate(
                    session, SessionState.FAILED, "max_repeated_failures exceeded"
                )

            # --- model turn ---
            session.state = SessionState.MODEL_THINKING
            request = self._context_builder.build(session, self._registry.definitions())
            turn_id = request.turn_id
            turn = AgentTurn(
                turn_id=turn_id,
                session_id=session.session_id,
                index=turn_index,
                started_at=dt.datetime.now(),
                model_request=request,
            )
            self._emit(session, EventType.MODEL_REQUESTED, turn_id, request_id=request.request_id)
            try:
                response = await self._provider.generate(request)
            except Exception as exc:  # provider failures must not escape as raw exceptions
                provider_error = normalize_provider_failure(
                    exc, provider_id=self._provider_id()
                )
                return self._terminate(
                    session,
                    SessionState.FAILED,
                    f"provider error: {provider_error.code.value}: {provider_error.message}",
                    turn_id=turn_id,
                    provider_error=provider_error,
                )
            turn.model_response = response
            turn_index += 1
            self._emit(
                session,
                EventType.MODEL_RESPONDED,
                turn_id,
                stop_reason=response.stop_reason.value,
                tool_calls=len(response.tool_calls),
            )

            # --- tool-call turn ---
            if response.tool_calls:
                session.state = SessionState.TOOL_REQUESTED
                for call in response.tool_calls:
                    # Structural backstop (D22): enforce S1-S3 for every provider so
                    # an invalid ToolCall never reaches the executor. Provider-neutral
                    # and not a PROVIDER_ERROR (the runtime cannot attribute the
                    # defect); terminate via the existing failure path with a clear
                    # boundary diagnostic.
                    invalid = _invalid_tool_call_reason(call)
                    if invalid is not None:
                        return self._finish_turn(
                            session, turn, turn_id, SessionState.FAILED, invalid
                        )
                    self._emit(
                        session, EventType.TOOL_REQUESTED, turn_id,
                        tool_name=call.name, tool_call_id=call.tool_call_id,
                    )
                    session.state = SessionState.PERMISSION_CHECK
                    result = await self._executor.execute(
                        call, session_id=session.session_id, turn_id=turn_id
                    )
                    session.state = SessionState.TOOL_RESULT
                    turn.tool_calls.append(call)
                    turn.tool_results.append(result)
                    session.tool_call_history.append(call)
                    tool_call_count += 1
                    if result.counts_as_write():
                        tracker.note_write()
                    if result.status is ToolResultStatus.SUCCESS:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1

                    if tool_call_count >= limits.max_tool_calls:
                        return self._finish_turn(
                            session, turn, turn_id, SessionState.INTERRUPTED,
                            "max_tool_calls exceeded",
                        )
                    if consecutive_failures >= limits.max_repeated_failures:
                        return self._finish_turn(
                            session, turn, turn_id, SessionState.FAILED,
                            "max_repeated_failures exceeded",
                        )

                turn.ended_at = dt.datetime.now()
                session.history.append(turn)
                self._touch(session)
                continue

            # --- final answer -> VERIFICATION_CHECK ---
            turn.final_response = response.text()
            session.state = SessionState.VERIFICATION_CHECK

            if self._verifier.configured and tracker.dirty:
                self._emit(
                    session, EventType.VERIFICATION_STARTED, turn_id, command=self._verifier.command
                )
                verification = await self._verifier.run()
                session.verification_history.append(verification)
                tracker.note_result(verification)
                if verification.passed:
                    self._emit(
                        session, EventType.VERIFICATION_PASSED, turn_id,
                        command=verification.command,
                    )
                    return self._finish_turn(
                        session, turn, turn_id, SessionState.COMPLETED, "verified"
                    )
                self._emit(
                    session, EventType.VERIFICATION_FAILED, turn_id,
                    command=verification.command, exit_code=verification.exit_code,
                )
                # Verification failed: hand the result back to the model to diagnose
                # (bounded by limits). The turn (with its final_response) is kept.
                turn.ended_at = dt.datetime.now()
                session.history.append(turn)
                self._touch(session)
                continue

            # No verification run this turn.
            if self._verifier.configured:
                # Configured but not dirty: nothing written since the last pass.
                return self._finish_turn(
                    session, turn, turn_id, SessionState.COMPLETED, "completed"
                )
            final_state = (
                SessionState.COMPLETED_UNVERIFIED if tracker.ever_wrote else SessionState.COMPLETED
            )
            return self._finish_turn(
                session, turn, turn_id, final_state, "completed (no verify command)"
            )

    # -- helpers ------------------------------------------------------------- #

    def _finish_turn(
        self,
        session: AgentSession,
        turn: AgentTurn,
        turn_id: str,
        state: SessionState,
        reason: str,
    ) -> AgentSession:
        turn.ended_at = dt.datetime.now()
        session.history.append(turn)
        return self._terminate(session, state, reason, turn_id=turn_id)

    def _terminate(
        self,
        session: AgentSession,
        state: SessionState,
        reason: str,
        *,
        turn_id: str | None = None,
        provider_error: ProviderError | None = None,
    ) -> AgentSession:
        session.state = state
        session.completed_status = reason
        self._touch(session)
        if state in (SessionState.COMPLETED, SessionState.COMPLETED_UNVERIFIED):
            event = EventType.SESSION_COMPLETED
        elif state is SessionState.INTERRUPTED:
            event = EventType.SESSION_INTERRUPTED
        else:
            event = EventType.SESSION_FAILED
        payload: dict[str, Any] = {"state": state.value, "reason": reason}
        if provider_error is not None:
            payload["provider_error"] = provider_error.to_dict()
        self._emit(session, event, turn_id, **payload)
        return session

    @staticmethod
    def _touch(session: AgentSession) -> None:
        session.updated_at = dt.datetime.now()

    def _provider_id(self) -> str:
        """Best-effort provider id for §25 normalization (never raises)."""
        try:
            return self._provider.describe().provider_id
        except Exception:
            return ""

    def _emit(
        self,
        session: AgentSession,
        event_type: EventType,
        turn_id: str | None,
        **payload: Any,
    ) -> None:
        self._event_sink.emit(
            AgentEvent(
                type=event_type,
                session_id=session.session_id,
                turn_id=turn_id,
                payload=payload,
            )
        )


def _invalid_tool_call_reason(call: ToolCall) -> str | None:
    """Structural ToolCall validation (contracts §14, architecture §4.6, D22).

    Returns a boundary diagnostic when ``call`` violates S1-S3 — ``tool_call_id``
    and ``name`` must be non-empty strings and ``arguments`` must be a ``dict`` —
    or ``None`` when structurally valid. Tool existence and argument-schema checks
    are the executor's responsibility, not this guard's.
    """
    boundary = "invalid tool call reached runtime boundary"
    if not isinstance(call.tool_call_id, str) or not call.tool_call_id:
        return f"{boundary}: tool_call_id must be a non-empty string"
    if not isinstance(call.name, str) or not call.name:
        return f"{boundary}: name must be a non-empty string"
    if not isinstance(call.arguments, dict):
        return f"{boundary}: arguments must be a dict"
    return None


def new_session_id() -> str:
    return f"session_{uuid.uuid4().hex[:12]}"
