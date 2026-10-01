"""AgentRuntime tests: D11 scenarios, D12 staleness, D15 limits, event order."""

from __future__ import annotations

import json

from coding_agent.context import ContextBuilder
from coding_agent.contracts import (
    AgentSession,
    Limits,
    ModelCapabilities,
    ModelDescriptor,
    ModelRef,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorCode,
    SessionState,
    ToolResultStatus,
)
from coding_agent.errors import ProviderExecutionError
from coding_agent.events import EventType, ListEventSink
from coding_agent.executor import ToolExecutor
from coding_agent.permissions import Approver, AutoApprover, PermissionPolicy
from coding_agent.providers.base import ModelProvider
from coding_agent.providers.mock import (
    MockModelProvider,
    final_response,
    tool_call_response,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.tools.registry import default_registry
from coding_agent.verification import (
    DEFAULT_VERIFY_TIMEOUT_S,
    VerificationTracker,
    Verifier,
)
from coding_agent.workspace import Workspace


def _build(
    workspace: Workspace,
    provider: ModelProvider,
    *,
    verify: str | None = None,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT_S,
    limits: Limits | None = None,
    approver: Approver | None = None,
) -> tuple[AgentRuntime, AgentSession, ListEventSink, VerificationTracker]:
    registry = default_registry()
    sink = ListEventSink()
    tracker = VerificationTracker()
    verifier = Verifier(verify, repo_root=workspace.repo_root, timeout_s=verify_timeout)
    context_builder = ContextBuilder(ModelRef.from_descriptor(provider.describe()))
    executor = ToolExecutor(
        registry=registry,
        workspace=workspace,
        policy=PermissionPolicy(),
        approver=approver or AutoApprover(),
        event_sink=sink,
        default_timeout_ms=30_000,
    )
    runtime = AgentRuntime(
        provider=provider,
        registry=registry,
        executor=executor,
        context_builder=context_builder,
        verifier=verifier,
        event_sink=sink,
        tracker=tracker,
    )
    session = AgentSession(
        session_id="s-test",
        request="complete the task",
        repo_root=workspace.repo_root,
        working_dir=workspace.repo_root,
        limits=limits or Limits(),
    )
    return runtime, session, sink, tracker


def _reads(n: int) -> list:
    return [tool_call_response("read_file", {"path": "hello.py"}) for _ in range(n)]


# --- D11 happy path: read -> edit[approved] -> completion -> verification passes --- #


async def test_happy_path_verified(workspace: Workspace, pycmd) -> None:
    check = "import pathlib,sys; sys.exit(0 if 'Hi' in pathlib.Path('hello.py').read_text() else 1)"
    verify = pycmd(check)
    provider = MockModelProvider(
        [
            tool_call_response("read_file", {"path": "hello.py"}),
            tool_call_response(
                "edit_file",
                {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('Hi')"},
            ),
            final_response("all done"),
        ]
    )
    runtime, session, sink, tracker = _build(workspace, provider, verify=verify)
    final = await runtime.run(session)

    assert final.state is SessionState.COMPLETED
    assert final.completed_status == "verified"
    assert tracker.ever_wrote is True
    assert tracker.dirty is False  # the pass cleared staleness (D12)
    assert len(final.verification_history) == 1
    assert final.verification_history[0].passed is True
    types = sink.types()
    assert EventType.VERIFICATION_PASSED in types
    assert EventType.SESSION_COMPLETED in types


# --- D11 failure path: verification fails -> diagnose -> edit -> verification passes --- #


async def test_failure_path_then_verified(workspace: Workspace, pycmd) -> None:
    verify = pycmd(
        "import pathlib,sys; sys.exit(0 if 'TARGET' in pathlib.Path('hello.py').read_text() else 1)"
    )
    provider = MockModelProvider(
        [
            # First attempt: an edit that will NOT satisfy verification.
            tool_call_response(
                "edit_file",
                {"path": "hello.py", "old_text": "print('Hello')", "new_text": "WRONG"},
            ),
            final_response("first attempt"),
            # Verification failed -> model diagnoses and corrects.
            tool_call_response(
                "edit_file", {"path": "hello.py", "old_text": "WRONG", "new_text": "TARGET"}
            ),
            final_response("second attempt"),
        ]
    )
    runtime, session, sink, tracker = _build(workspace, provider, verify=verify)
    final = await runtime.run(session)

    assert final.state is SessionState.COMPLETED
    assert final.completed_status == "verified"
    # Two verifications ran: the first failed, the second passed.
    assert [v.passed for v in final.verification_history] == [False, True]
    assert tracker.dirty is False
    types = sink.types()
    assert EventType.VERIFICATION_FAILED in types
    assert EventType.VERIFICATION_PASSED in types


# --- completion branches when no verification is configured --- #


async def test_completed_unverified_when_wrote_without_verify(workspace: Workspace) -> None:
    provider = MockModelProvider(
        [
            tool_call_response(
                "edit_file",
                {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('Hi')"},
            ),
            final_response("done"),
        ]
    )
    runtime, session, _sink, tracker = _build(workspace, provider, verify=None)
    final = await runtime.run(session)
    assert final.state is SessionState.COMPLETED_UNVERIFIED
    assert tracker.ever_wrote is True
    assert final.verification_history == []


async def test_completed_when_no_write_and_no_verify(workspace: Workspace) -> None:
    provider = MockModelProvider([final_response("nothing to change")])
    runtime, session, _sink, tracker = _build(workspace, provider, verify=None)
    final = await runtime.run(session)
    assert final.state is SessionState.COMPLETED
    assert tracker.ever_wrote is False


async def test_configured_but_clean_does_not_rerun_verification(
    workspace: Workspace, pycmd
) -> None:
    # Only a read-only tool call: nothing is written, so verification is not run.
    verify = pycmd("print('ok')")
    provider = MockModelProvider(
        [tool_call_response("read_file", {"path": "hello.py"}), final_response("done")]
    )
    runtime, session, _sink, tracker = _build(workspace, provider, verify=verify)
    final = await runtime.run(session)
    assert final.state is SessionState.COMPLETED
    assert final.completed_status == "completed"
    assert tracker.dirty is False
    assert final.verification_history == []


# --- D12 staleness: which tool calls make verification necessary --- #


async def test_non_allowlisted_shell_write_triggers_verification(
    workspace: Workspace, pycmd
) -> None:
    verify = pycmd("print('ok')")
    provider = MockModelProvider(
        [tool_call_response("shell", {"command": "echo hi"}), final_response("done")]
    )
    runtime, session, _sink, tracker = _build(workspace, provider, verify=verify)
    final = await runtime.run(session)
    assert tracker.ever_wrote is True  # a non-allowlisted shell run is a write (D12)
    assert final.state is SessionState.COMPLETED
    assert len(final.verification_history) == 1


async def test_readonly_allowlisted_shell_is_not_a_write(workspace: Workspace, pycmd) -> None:
    # "git status" is on the exact-argv read-only allowlist -> ALLOW -> never a write,
    # so no verification is triggered even though a verify command is configured.
    verify = pycmd("print('ok')")
    provider = MockModelProvider(
        [tool_call_response("shell", {"command": "git status"}), final_response("done")]
    )
    runtime, session, _sink, tracker = _build(workspace, provider, verify=verify)
    final = await runtime.run(session)
    assert tracker.ever_wrote is False
    assert final.verification_history == []
    assert final.state is SessionState.COMPLETED


# --- D15 runtime limits --- #


async def test_limit_max_turns_interrupted(workspace: Workspace) -> None:
    provider = MockModelProvider(_reads(5))
    runtime, session, sink, _t = _build(
        workspace, provider, limits=Limits(max_turns=2)
    )
    final = await runtime.run(session)
    assert final.state is SessionState.INTERRUPTED
    assert final.completed_status == "max_turns exceeded"
    assert EventType.SESSION_INTERRUPTED in sink.types()


async def test_limit_max_tool_calls_interrupted(workspace: Workspace) -> None:
    provider = MockModelProvider(_reads(5))
    runtime, session, _sink, _t = _build(
        workspace, provider, limits=Limits(max_tool_calls=1)
    )
    final = await runtime.run(session)
    assert final.state is SessionState.INTERRUPTED
    assert final.completed_status == "max_tool_calls exceeded"
    assert len(final.tool_call_history) == 1


async def test_limit_max_repeated_failures_failed(workspace: Workspace) -> None:
    missing = [tool_call_response("read_file", {"path": "nope.txt"}) for _ in range(5)]
    provider = MockModelProvider(missing)
    runtime, session, sink, _t = _build(
        workspace, provider, limits=Limits(max_repeated_failures=2)
    )
    final = await runtime.run(session)
    assert final.state is SessionState.FAILED
    assert final.completed_status == "max_repeated_failures exceeded"
    assert EventType.SESSION_FAILED in sink.types()
    # Every recorded tool result was a failure.
    assert all(r.status is ToolResultStatus.FAILURE for t in final.history for r in t.tool_results)


async def test_limit_max_time_zero_interrupts_before_any_model_call(
    workspace: Workspace,
) -> None:
    provider = MockModelProvider(_reads(5))
    runtime, session, _sink, _t = _build(
        workspace, provider, limits=Limits(max_time_s=0.0)
    )
    final = await runtime.run(session)
    assert final.state is SessionState.INTERRUPTED
    assert final.completed_status == "max_time_s exceeded"
    assert provider.call_count == 0  # the wall-time limit fired before turn 0


# --- event ordering --- #


async def test_event_order_happy_path(workspace: Workspace, pycmd) -> None:
    verify = pycmd("print('ok')")
    provider = MockModelProvider(
        [
            tool_call_response(
                "edit_file",
                {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('Hi')"},
            ),
            final_response("done"),
        ]
    )
    runtime, session, sink, _t = _build(workspace, provider, verify=verify)
    await runtime.run(session)
    types = sink.types()
    assert types[0] is EventType.SESSION_STARTED
    assert types[-1] is EventType.SESSION_COMPLETED
    assert types.index(EventType.VERIFICATION_PASSED) < types.index(
        EventType.SESSION_COMPLETED
    )
    # A write was approved and executed before completion.
    assert EventType.PERMISSION_GRANTED in types
    assert EventType.TOOL_COMPLETED in types


# --- correction 2: provider failures are normalized at the runtime boundary --- #


class _RaisingProvider(ModelProvider):
    """A provider whose ``generate`` always raises the given exception."""

    def __init__(self, exc: BaseException, provider_id: str = "raiser") -> None:
        self._exc = exc
        self._provider_id = provider_id

    def describe(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider_id=self._provider_id,
            model_id="r1",
            capabilities=ModelCapabilities(text=True, tool_calling=True),
            context_window=1000,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise self._exc


async def test_provider_failure_does_not_escape_and_fails_session(
    workspace: Workspace,
) -> None:
    provider = _RaisingProvider(RuntimeError("boom"))
    runtime, session, sink, _t = _build(workspace, provider)
    final = await runtime.run(session)  # must return, not raise
    assert final.state is SessionState.FAILED
    assert final.completed_status is not None
    assert final.completed_status.startswith("provider error: PROVIDER_ERROR")
    assert EventType.SESSION_FAILED in sink.types()


async def test_provider_failure_event_carries_normalized_error(workspace: Workspace) -> None:
    provider = _RaisingProvider(RuntimeError("boom"))
    runtime, session, sink, _t = _build(workspace, provider)
    await runtime.run(session)
    failed = sink.of_type(EventType.SESSION_FAILED)
    assert len(failed) == 1
    payload = failed[0].payload
    assert payload["provider_error"]["code"] == "PROVIDER_ERROR"
    assert payload["provider_error"]["provider_id"] == "raiser"
    assert payload["provider_error"]["metadata"]["exception_type"] == "RuntimeError"
    json.dumps(payload)  # plain data only -> JSON serializable (no leaked class)


async def test_normalized_provider_error_is_preserved(workspace: Workspace) -> None:
    error = ProviderError(
        code=ProviderErrorCode.RATE_LIMITED,
        message="slow down",
        retryable=True,
        retry_after_ms=5000,
        provider_id="raiser",
    )
    provider = _RaisingProvider(ProviderExecutionError(error))
    runtime, session, sink, _t = _build(workspace, provider)
    final = await runtime.run(session)
    assert final.state is SessionState.FAILED
    assert "RATE_LIMITED" in (final.completed_status or "")
    payload = sink.of_type(EventType.SESSION_FAILED)[0].payload
    assert payload["provider_error"]["code"] == "RATE_LIMITED"
    assert payload["provider_error"]["retryable"] is True
    assert payload["provider_error"]["retry_after_ms"] == 5000


async def test_provider_specific_exception_type_does_not_leak(workspace: Workspace) -> None:
    class VendorSDKError(Exception):
        """Simulates a provider-specific exception class."""

    provider = _RaisingProvider(VendorSDKError("secret vendor detail"))
    runtime, session, sink, _t = _build(workspace, provider)
    final = await runtime.run(session)
    assert final.state is SessionState.FAILED
    payload = sink.of_type(EventType.SESSION_FAILED)[0].payload
    # Normalized to the generic code; only the type *name* (a str) is retained.
    assert payload["provider_error"]["code"] == "PROVIDER_ERROR"
    assert payload["provider_error"]["metadata"]["exception_type"] == "VendorSDKError"
    assert isinstance(payload["provider_error"]["metadata"]["exception_type"], str)
    assert "VendorSDKError" not in (final.completed_status or "")
