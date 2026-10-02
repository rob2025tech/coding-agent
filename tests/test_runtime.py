"""AgentRuntime tests: D11 scenarios, D12 staleness, D15 limits, event order."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from coding_agent.context import ContextBuilder
from coding_agent.contracts import (
    AgentSession,
    ErrorCode,
    Limits,
    ModelCapabilities,
    ModelDescriptor,
    ModelRef,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorCode,
    SessionState,
    StopReason,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from coding_agent.errors import ProviderExecutionError
from coding_agent.events import EventSink, EventType, ListEventSink
from coding_agent.executor import ToolExecutor
from coding_agent.permissions import Approver, AutoApprover, DenyAllApprover, PermissionPolicy
from coding_agent.providers.base import ModelProvider
from coding_agent.providers.mock import (
    MockModelProvider,
    final_response,
    tool_call_response,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.tools.registry import ToolRegistry, default_registry
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
    executor: ToolExecutor | None = None,
) -> tuple[AgentRuntime, AgentSession, ListEventSink, VerificationTracker]:
    registry = default_registry()
    sink = ListEventSink()
    tracker = VerificationTracker()
    verifier = Verifier(verify, repo_root=workspace.repo_root, timeout_s=verify_timeout)
    context_builder = ContextBuilder(ModelRef.from_descriptor(provider.describe()))
    executor = executor or ToolExecutor(
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


async def test_write_file_triggers_runtime_verification(workspace: Workspace, pycmd) -> None:
    # A successful write_file is a D12 write, so the runtime runs the configured
    # verification on the completion signal and records the result.
    verify = pycmd(
        "import pathlib,sys; sys.exit(0 if pathlib.Path('new.txt').read_text() == 'hi' else 1)"
    )
    provider = MockModelProvider(
        [
            tool_call_response("write_file", {"path": "new.txt", "content": "hi"}),
            final_response("done"),
        ]
    )
    runtime, session, sink, tracker = _build(workspace, provider, verify=verify)
    final = await runtime.run(session)
    assert tracker.ever_wrote is True
    assert tracker.dirty is False  # the pass cleared staleness (D12)
    assert final.state is SessionState.COMPLETED
    assert final.completed_status == "verified"
    assert len(final.verification_history) == 1
    assert final.verification_history[0].passed is True
    assert EventType.VERIFICATION_PASSED in sink.types()


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


# --- D22 structural backstop: invalid ToolCalls never reach the executor ---- #


class _SpyExecutor(ToolExecutor):
    """Records every ``execute`` call, then delegates to the real executor."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        workspace: Workspace,
        policy: PermissionPolicy,
        approver: Approver,
        event_sink: EventSink,
        default_timeout_ms: int = 30_000,
    ) -> None:
        super().__init__(
            registry=registry,
            workspace=workspace,
            policy=policy,
            approver=approver,
            event_sink=event_sink,
            default_timeout_ms=default_timeout_ms,
        )
        self.calls: list[ToolCall] = []

    async def execute(
        self,
        call: ToolCall,
        *,
        session_id: str,
        turn_id: str | None = None,
    ) -> ToolResult:
        self.calls.append(call)
        return await super().execute(call, session_id=session_id, turn_id=turn_id)


def _spy(workspace: Workspace) -> _SpyExecutor:
    return _SpyExecutor(
        registry=default_registry(),
        workspace=workspace,
        policy=PermissionPolicy(),
        approver=AutoApprover(),
        event_sink=ListEventSink(),
        default_timeout_ms=30_000,
    )


@pytest.mark.parametrize(
    "bad_call",
    [
        ToolCall(tool_call_id="", name="read_file", arguments={}),
        ToolCall(tool_call_id="c1", name="", arguments={}),
        ToolCall(tool_call_id="c1", name="read_file", arguments=cast(Any, "oops")),
    ],
)
async def test_invalid_tool_call_never_reaches_executor(
    workspace: Workspace, bad_call: ToolCall
) -> None:
    provider = MockModelProvider(
        [
            ModelResponse(
                response_id="r1",
                stop_reason=StopReason.TOOL_CALL,
                content=[],
                tool_calls=[bad_call],
            )
        ]
    )
    spy = _spy(workspace)
    runtime, session, sink, _t = _build(workspace, provider, executor=spy)
    final = await runtime.run(session)
    assert spy.calls == []  # the executor was never reached
    assert final.state is SessionState.FAILED
    assert (final.completed_status or "").startswith(
        "invalid tool call reached runtime boundary"
    )
    assert EventType.TOOL_REQUESTED not in sink.types()
    assert EventType.SESSION_FAILED in sink.types()
    # D22: the backstop is not classified as a provider error.
    assert "provider_error" not in sink.of_type(EventType.SESSION_FAILED)[0].payload
    assert final.tool_call_history == []


async def test_valid_tool_call_reaches_executor(workspace: Workspace) -> None:
    provider = MockModelProvider(
        [tool_call_response("read_file", {"path": "hello.py"}), final_response("done")]
    )
    spy = _spy(workspace)
    runtime, session, sink, _t = _build(workspace, provider, executor=spy)
    final = await runtime.run(session)
    assert len(spy.calls) == 1  # the structurally valid call reached the executor
    assert spy.calls[0].name == "read_file"
    assert EventType.TOOL_REQUESTED in sink.types()
    assert final.state is SessionState.COMPLETED


async def test_valid_tool_call_with_empty_arguments_reaches_executor(
    workspace: Workspace,
) -> None:
    # D22 (audit F-1): arguments={} is structurally valid (S3, contracts §14), so it
    # must pass the runtime backstop into the executor, preserving the normal flow.
    provider = MockModelProvider(
        [tool_call_response("read_file", {}), final_response("done")]
    )
    spy = _spy(workspace)
    runtime, session, sink, _t = _build(workspace, provider, executor=spy)
    final = await runtime.run(session)
    assert len(spy.calls) == 1  # the executor was reached
    assert spy.calls[0].tool_call_id  # non-empty string tool_call_id
    assert spy.calls[0].name == "read_file"  # non-empty string name
    assert spy.calls[0].arguments == {}  # {} passed through untouched
    assert EventType.TOOL_REQUESTED in sink.types()  # the backstop did not fire
    assert final.state is SessionState.COMPLETED  # existing success behavior


# --- D24 capability gate: `tool_calling` is the only mandatory capability --- #


class _CapabilityProvider(ModelProvider):
    """A minimal provider advertising a chosen ``ModelCapabilities``.

    D24 requires the runtime to gate on the neutral ``ModelDescriptor.capabilities``
    rather than on provider identity, so an arbitrary ``provider_id``/``model_id``
    shows the check is descriptor-driven. ``generate`` records each request so a
    test can assert it was never awaited, and ``describe`` counts calls so a test
    can pin D24's once-per-``run()`` guarantee.
    """

    def __init__(
        self,
        capabilities: ModelCapabilities,
        *,
        responses: list[ModelResponse] | None = None,
        provider_id: str = "fake",
        model_id: str = "fake-m1",
    ) -> None:
        self._capabilities = capabilities
        self._provider_id = provider_id
        self._model_id = model_id
        self._script: list[ModelResponse] = list(responses or [])
        self.requests: list[ModelRequest] = []
        self.describe_calls = 0

    def describe(self) -> ModelDescriptor:
        self.describe_calls += 1
        return ModelDescriptor(
            provider_id=self._provider_id,
            model_id=self._model_id,
            capabilities=self._capabilities,
            context_window=1000,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._script:
            return self._script.pop(0)
        return final_response("done")


_TOOL_CAPABLE = ModelCapabilities(
    text=True, tool_calling=True, streaming=False, vision=False, reasoning=False
)
_NO_TOOL_CALLING = ModelCapabilities(
    text=True, tool_calling=False, streaming=False, vision=False, reasoning=False
)
_CAPABILITY_REASON = "model capability unavailable: tool_calling is required for the agent loop"


async def test_tool_calling_capability_allows_normal_execution(workspace: Workspace) -> None:
    # A: with the mandatory capability present the ordinary loop still runs end to
    # end. Both shipped providers report tool_calling=True, so this is a no-op for
    # them; the gate must not disturb normal execution.
    provider = _CapabilityProvider(
        _TOOL_CAPABLE,
        responses=[
            tool_call_response("read_file", {"path": "hello.py"}),
            final_response("done"),
        ],
    )
    spy = _spy(workspace)
    runtime, session, sink, _t = _build(workspace, provider, executor=spy)
    # _build() reads the descriptor to construct the ModelRef; reset so the count
    # below measures AgentRuntime.run() alone.
    provider.describe_calls = 0
    final = await runtime.run(session)
    assert final.state is SessionState.COMPLETED
    assert len(provider.requests) == 2  # generate() was awaited as usual
    # Audit G-1: the gate consults describe() exactly once per run(), not once per
    # turn — this session spans two model turns, so a per-turn check would see 2.
    assert provider.describe_calls == 1
    assert len(spy.calls) == 1  # the executor was reached
    assert EventType.TOOL_REQUESTED in sink.types()
    assert EventType.SESSION_COMPLETED in sink.types()


async def test_missing_tool_calling_capability_fails_session(workspace: Workspace) -> None:
    # B: tool_calling=False is rejected before the first model turn (D24).
    provider = _CapabilityProvider(_NO_TOOL_CALLING)
    spy = _spy(workspace)
    runtime, session, sink, _t = _build(workspace, provider, executor=spy)
    final = await runtime.run(session)

    assert final.state is SessionState.FAILED
    assert final.completed_status == _CAPABILITY_REASON
    # Started, then failed: no model turn, no tool turn, no verification.
    assert sink.types() == [EventType.SESSION_STARTED, EventType.SESSION_FAILED]
    assert final.history == []  # no AgentTurn was created
    assert final.tool_call_history == []
    assert provider.requests == []  # generate() was never awaited
    assert spy.calls == []  # the executor was never invoked
    # Not a provider failure (§25): no provider_error, and not MODEL_UNAVAILABLE.
    payload = sink.of_type(EventType.SESSION_FAILED)[0].payload
    assert "provider_error" not in payload
    assert payload["state"] == SessionState.FAILED.value
    assert payload["reason"] == _CAPABILITY_REASON
    assert "provider error:" not in (final.completed_status or "")
    assert "MODEL_UNAVAILABLE" not in (final.completed_status or "")


@pytest.mark.parametrize("provider_id", ["fake-provider", "another-vendor"])
async def test_capability_gate_is_descriptor_driven_not_provider_specific(
    workspace: Workspace, provider_id: str
) -> None:
    # E: identical descriptors are rejected identically whatever the provider
    # identity, and the diagnostic names the capability, never the provider.
    provider = _CapabilityProvider(_NO_TOOL_CALLING, provider_id=provider_id, model_id="m-9")
    runtime, session, _sink, _t = _build(workspace, provider)
    final = await runtime.run(session)
    assert final.state is SessionState.FAILED
    assert final.completed_status == _CAPABILITY_REASON
    assert provider_id not in (final.completed_status or "")
    assert "m-9" not in (final.completed_status or "")


@pytest.mark.parametrize(
    "capabilities",
    [
        ModelCapabilities(text=True, tool_calling=True, streaming=False),
        ModelCapabilities(text=True, tool_calling=True, streaming=True),
        ModelCapabilities(
            text=True, tool_calling=True, streaming=False, vision=True, reasoning=True
        ),
    ],
)
async def test_only_tool_calling_is_gated(
    workspace: Workspace, capabilities: ModelCapabilities
) -> None:
    # C: D24 gates `tool_calling` alone. `streaming` is explicitly not required —
    # both shipped providers report streaming=False — nor are vision/reasoning.
    provider = _CapabilityProvider(capabilities)
    runtime, session, sink, _t = _build(workspace, provider)
    final = await runtime.run(session)
    assert final.state is SessionState.COMPLETED
    assert "model capability unavailable" not in (final.completed_status or "")
    assert EventType.SESSION_FAILED not in sink.types()
    assert len(provider.requests) == 1


async def test_describe_failure_still_normalizes_as_provider_error(workspace: Workspace) -> None:
    # D: describe() raising is a genuine provider failure (§25) and keeps the
    # existing normalization path; it must not be read as a capability mismatch.
    class _DescribeRaiser(ModelProvider):
        def describe(self) -> ModelDescriptor:
            raise RuntimeError("describe boom")

        async def generate(self, request: ModelRequest) -> ModelResponse:
            return final_response("done")  # unreachable: the gate fails first

    spy = _spy(workspace)
    sink = ListEventSink()
    # _build() calls describe() itself, so construct the runtime directly here.
    runtime = AgentRuntime(
        provider=_DescribeRaiser(),
        registry=default_registry(),
        executor=spy,
        context_builder=ContextBuilder(ModelRef(provider_id="raiser", model_id="r1")),
        verifier=Verifier(None, repo_root=workspace.repo_root),
        event_sink=sink,
    )
    session = AgentSession(
        session_id="s-test",
        request="complete the task",
        repo_root=workspace.repo_root,
        working_dir=workspace.repo_root,
    )
    final = await runtime.run(session)  # must return, not raise
    assert final.state is SessionState.FAILED
    assert sink.types() == [EventType.SESSION_STARTED, EventType.SESSION_FAILED]
    assert (final.completed_status or "").startswith("provider error: PROVIDER_ERROR")
    payload = sink.of_type(EventType.SESSION_FAILED)[0].payload
    assert payload["provider_error"]["code"] == "PROVIDER_ERROR"
    assert payload["provider_error"]["metadata"]["exception_type"] == "RuntimeError"
    assert "model capability unavailable" not in (final.completed_status or "")
    assert spy.calls == []
    assert final.history == []


# --- D23 follow-up: runtime-level DENIED refusal continuation (§27, §31, §34) --- #


async def test_denied_refusal_is_appended_and_session_continues(workspace: Workspace) -> None:
    # D23 / §27 / §34: a refusal is appended as a DENIED ToolResult and the loop
    # continues to the model instead of ending the session. DenyAllApprover turns the
    # edit_file ASK into a denial, so no write occurs and the next turn can complete.
    provider = MockModelProvider(
        [
            tool_call_response(
                "edit_file",
                {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('Hi')"},
            ),
            final_response("done"),
        ]
    )
    runtime, session, sink, tracker = _build(
        workspace, provider, verify=None, approver=DenyAllApprover()
    )
    final = await runtime.run(session)

    # The refusal did not terminate the session: the model was reached again and the
    # session then completed normally.
    assert final.state is SessionState.COMPLETED
    assert provider.call_count == 2  # the refused turn, then the final-answer turn

    # The DENIED result was appended to its turn and the call to the session history.
    assert len(final.history) == 2  # the refused tool turn + the final-answer turn
    denied = final.history[0].tool_results[0]
    assert denied.status is ToolResultStatus.DENIED
    assert denied.error is not None
    assert denied.error.code is ErrorCode.PERMISSION_DENIED
    assert len(final.tool_call_history) == 1
    assert final.tool_call_history[0].name == "edit_file"

    # The edit was refused, so nothing was written and nothing is stale.
    assert tracker.ever_wrote is False

    types = sink.types()
    assert EventType.PERMISSION_DENIED in types
    assert EventType.SESSION_COMPLETED in types


async def test_repeated_denied_refusals_trip_max_repeated_failures(workspace: Workspace) -> None:
    # D23 / §34: a refusal counts against max_repeated_failures, so repeated denials
    # drive the session to FAILED through the existing limit (implementation unchanged).
    edits = [
        tool_call_response(
            "edit_file",
            {"path": "hello.py", "old_text": "print('Hello')", "new_text": "print('Hi')"},
        )
        for _ in range(3)
    ]
    provider = MockModelProvider(edits)
    runtime, session, sink, _t = _build(
        workspace,
        provider,
        limits=Limits(max_repeated_failures=2),
        approver=DenyAllApprover(),
    )
    final = await runtime.run(session)

    assert final.state is SessionState.FAILED
    assert final.completed_status == "max_repeated_failures exceeded"
    assert EventType.SESSION_FAILED in sink.types()
    # Every recorded tool result was a DENIED refusal, not an execution failure.
    results = [r for turn in final.history for r in turn.tool_results]
    assert results
    assert all(r.status is ToolResultStatus.DENIED for r in results)
