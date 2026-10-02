"""HTTP provider tests — deterministic, offline, no credentials (D21; §3.1/§25).

Uses an injectable fake transport, so there is no network, no API key lookup
against a real service, and no paid call. Covers: successful generation, the §25
error-code mapping, malformed responses, timeout/network failure, cancellation,
secret non-leakage, and an end-to-end tool-calling task through the real runtime.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error

import pytest

from coding_agent.cli import build_arg_parser, build_runtime
from coding_agent.contracts import (
    MessageContent,
    MessageRole,
    ModelMessage,
    ModelRef,
    ModelRequest,
    PermissionClass,
    ProviderErrorCode,
    SessionState,
    SideEffect,
    StopReason,
    ToolCall,
    ToolDefinition,
)
from coding_agent.errors import ProviderExecutionError
from coding_agent.events import EventType, ListEventSink
from coding_agent.providers.http import HttpModelProvider, HttpRequest, HttpResponse

KEY_ENV = "TEST_CODING_AGENT_API_KEY"
ENDPOINT = "https://provider.invalid/v1/chat/completions"
SECRET = "sk-test-SECRET-do-not-leak"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with a known key in the env; none is ever sent for real."""
    monkeypatch.setenv(KEY_ENV, SECRET)


# --- helpers --------------------------------------------------------------- #


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="r1",
        session_id="s1",
        turn_id="t1",
        model=ModelRef(provider_id="http", model_id="test-model"),
        system_instructions="sys",
    )


def _request_with_tool() -> ModelRequest:
    return ModelRequest(
        request_id="r1",
        session_id="s1",
        turn_id="t1",
        model=ModelRef(provider_id="http", model_id="test-model"),
        system_instructions="sys",
        messages=[ModelMessage(role=MessageRole.USER, content=[MessageContent.text_block("hi")])],
        tools=[
            ToolDefinition(
                name="read_file",
                description="read a file",
                input_schema={"type": "object"},
                side_effect=SideEffect.NONE,
                permission=PermissionClass.READ,
            )
        ],
    )


def _tool_call_body(name: str, arguments: dict, *, call_id: str = "call_1") -> bytes:
    return json.dumps(
        {
            "id": "resp_tool",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
        }
    ).encode()


def _text_body(text: str, *, finish: str = "stop") -> bytes:
    return json.dumps(
        {
            "id": "resp_text",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    ).encode()


class FakeTransport:
    """Replays scripted responses; raises scripted exceptions; records requests."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            return HttpResponse(200, {}, _text_body("done"))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _provider(transport, **kwargs) -> HttpModelProvider:
    return HttpModelProvider(
        endpoint=ENDPOINT, model_id="test-model", api_key_env=KEY_ENV, transport=transport, **kwargs
    )


# --- interface surface ----------------------------------------------------- #


def test_describe_reports_tool_capable_non_streaming() -> None:
    descriptor = _provider(FakeTransport([])).describe()
    assert descriptor.provider_id == "http"
    assert descriptor.model_id == "test-model"
    assert descriptor.capabilities.tool_calling is True
    assert descriptor.capabilities.streaming is False
    assert descriptor.context_window > 0


def test_stream_is_interface_only() -> None:
    with pytest.raises(NotImplementedError):
        _provider(FakeTransport([])).stream(_request())


# --- successful generation ------------------------------------------------- #


async def test_generate_parses_tool_call_with_dict_arguments() -> None:
    transport = FakeTransport([HttpResponse(200, {}, _tool_call_body("read_file", {"path": "a"}))])
    response = await _provider(transport).generate(_request())
    assert response.stop_reason is StopReason.TOOL_CALL
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.tool_call_id, call.name) == ("call_1", "read_file")
    assert call.arguments == {"path": "a"}  # parsed to a dict, not a JSON string
    assert response.usage is not None
    assert response.usage.input_tokens == 12


async def test_generate_parses_final_text() -> None:
    transport = FakeTransport([HttpResponse(200, {}, _text_body("all done"))])
    response = await _provider(transport).generate(_request())
    assert response.stop_reason is StopReason.COMPLETED
    assert response.text() == "all done"
    assert response.tool_calls == []


# --- outbound request shape + secret placement ----------------------------- #


async def test_request_auth_header_and_no_secret_in_url_or_body() -> None:
    transport = FakeTransport([HttpResponse(200, {}, _text_body("ok"))])
    await _provider(transport).generate(_request_with_tool())
    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.url == ENDPOINT
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in sent.url
    assert SECRET.encode() not in sent.body
    payload = json.loads(sent.body.decode())
    assert payload["model"] == "test-model"
    assert payload["stream"] is False
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert payload["tool_choice"] == "auto"


async def test_tool_results_pair_by_id_and_verification_falls_back() -> None:
    transport = FakeTransport([HttpResponse(200, {}, _text_body("ok"))])
    request = ModelRequest(
        request_id="r1",
        session_id="s1",
        turn_id="t1",
        model=ModelRef(provider_id="http", model_id="test-model"),
        system_instructions="sys",
        messages=[
            ModelMessage(role=MessageRole.USER, content=[MessageContent.text_block("do it")]),
            ModelMessage(
                role=MessageRole.ASSISTANT,
                content=[],
                tool_calls=[ToolCall(tool_call_id="c1", name="read_file", arguments={"path": "a"})],
            ),
            ModelMessage(role=MessageRole.TOOL, content=[MessageContent.text_block("tool-result")]),
            ModelMessage(role=MessageRole.TOOL, content=[MessageContent.text_block("verified")]),
        ],
    )
    await _provider(transport).generate(request)
    messages = json.loads(transport.requests[0].body.decode())["messages"]
    assert messages[1] == {"role": "user", "content": "do it"}
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == json.dumps({"path": "a"})
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "tool-result"}
    # An unpaired tool message (a verification result) becomes a plain user turn.
    assert messages[4] == {"role": "user", "content": "verified"}


# --- §25 error-code mapping ------------------------------------------------ #


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderErrorCode.AUTHENTICATION_FAILED),
        (403, ProviderErrorCode.AUTHENTICATION_FAILED),
        (400, ProviderErrorCode.INVALID_REQUEST),
        (404, ProviderErrorCode.PROVIDER_ERROR),
        (413, ProviderErrorCode.CONTEXT_TOO_LARGE),
        (408, ProviderErrorCode.NETWORK_ERROR),
        (500, ProviderErrorCode.PROVIDER_ERROR),
        (503, ProviderErrorCode.PROVIDER_ERROR),
        (418, ProviderErrorCode.PROVIDER_ERROR),
    ],
)
async def test_http_status_maps_to_normalized_code(
    status: int, expected: ProviderErrorCode
) -> None:
    transport = FakeTransport([HttpResponse(status, {}, b'{"error":"x"}')])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    error = excinfo.value.error
    assert error.code is expected
    assert error.provider_id == "http"
    assert error.metadata["http_status"] == status


async def test_rate_limited_parses_retry_after() -> None:
    transport = FakeTransport([HttpResponse(429, {"retry-after": "5"}, b'{"error":"slow"}')])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    error = excinfo.value.error
    assert error.code is ProviderErrorCode.RATE_LIMITED
    assert error.retryable is True
    assert error.retry_after_ms == 5000


async def test_400_with_context_signal_maps_to_context_too_large() -> None:
    body = b'{"error":{"message":"maximum context length exceeded"}}'
    transport = FakeTransport([HttpResponse(400, {}, body)])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.CONTEXT_TOO_LARGE


async def test_404_model_not_found_maps_to_model_unavailable() -> None:
    body = b'{"error":{"code":"model_not_found","message":"The model `x` does not exist"}}'
    transport = FakeTransport([HttpResponse(404, {}, body)])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.MODEL_UNAVAILABLE


async def test_generic_404_maps_to_provider_error() -> None:
    # A wrong-path 404 (HTML) carries no model-not-found signal -> PROVIDER_ERROR.
    transport = FakeTransport([HttpResponse(404, {}, b"<html>404 Not Found</html>")])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


# --- transport-level failures -> NETWORK_ERROR ----------------------------- #


async def test_network_failure_maps_to_network_error() -> None:
    transport = FakeTransport([urllib.error.URLError("dns failure")])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    error = excinfo.value.error
    assert error.code is ProviderErrorCode.NETWORK_ERROR
    assert error.metadata["exception_type"] == "URLError"


async def test_timeout_maps_to_network_error() -> None:
    transport = FakeTransport([TimeoutError("timed out")])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.NETWORK_ERROR


# --- malformed provider responses -> PROVIDER_ERROR ------------------------ #


async def test_malformed_json_maps_to_provider_error() -> None:
    transport = FakeTransport([HttpResponse(200, {}, b"not json at all")])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


async def test_missing_choices_maps_to_provider_error() -> None:
    transport = FakeTransport([HttpResponse(200, {}, b'{"id":"x"}')])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


async def test_malformed_tool_arguments_maps_to_provider_error() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{oops"},
            }
        ],
    }
    envelope = {"id": "r", "choices": [{"finish_reason": "tool_calls", "message": message}]}
    transport = FakeTransport([HttpResponse(200, {}, json.dumps(envelope).encode())])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


async def test_missing_response_id_maps_to_provider_error() -> None:
    envelope = {"choices": [{"finish_reason": "stop", "message": {}}]}
    transport = FakeTransport([HttpResponse(200, {}, json.dumps(envelope).encode())])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


async def test_non_string_response_id_maps_to_provider_error() -> None:
    envelope = {"id": 123, "choices": [{"finish_reason": "stop", "message": {}}]}
    transport = FakeTransport([HttpResponse(200, {}, json.dumps(envelope).encode())])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


async def test_empty_response_id_maps_to_provider_error() -> None:
    envelope = {"id": "", "choices": [{"finish_reason": "stop", "message": {}}]}
    transport = FakeTransport([HttpResponse(200, {}, json.dumps(envelope).encode())])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


async def test_unknown_finish_reason_maps_to_provider_error() -> None:
    transport = FakeTransport([HttpResponse(200, {}, _text_body("x", finish="weird_reason"))])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.PROVIDER_ERROR


# --- authentication / secret handling -------------------------------------- #


async def test_missing_api_key_is_auth_failure_and_skips_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    transport = FakeTransport([HttpResponse(200, {}, _text_body("never"))])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    assert excinfo.value.error.code is ProviderErrorCode.AUTHENTICATION_FAILED
    assert transport.requests == []  # endpoint never contacted without a key


async def test_error_does_not_echo_body_or_secret() -> None:
    body = json.dumps({"error": {"message": f"bad request {SECRET}"}}).encode()
    transport = FakeTransport([HttpResponse(400, {}, body)])
    with pytest.raises(ProviderExecutionError) as excinfo:
        await _provider(transport).generate(_request())
    error = excinfo.value.error
    assert error.code is ProviderErrorCode.INVALID_REQUEST
    assert SECRET not in error.message
    assert "bad request" not in error.message  # raw body never echoed
    assert SECRET not in json.dumps(error.to_dict())


# --- cancellation is never swallowed --------------------------------------- #


async def test_cancellation_propagates_and_is_not_normalized() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow(request: HttpRequest) -> HttpResponse:
        started.set()
        release.wait(5)
        return HttpResponse(200, {}, _text_body("late"))

    task = asyncio.create_task(_provider(slow).generate(_request()))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()


# --- end-to-end through the real runtime (offline) ------------------------- #


async def test_end_to_end_real_tool_calling_task_offline(repo) -> None:
    write_body = _tool_call_body("write_file", {"path": "new.txt", "content": "hi"})
    transport = FakeTransport(
        [
            HttpResponse(200, {}, write_body),
            HttpResponse(200, {}, _text_body("done")),
        ]
    )
    provider = HttpModelProvider(
        endpoint=ENDPOINT, model_id="test-model", api_key_env=KEY_ENV, transport=transport
    )
    args = build_arg_parser().parse_args(["--repo", str(repo), "--yes", "write new.txt"])
    sink = ListEventSink()
    runtime, session = build_runtime(args, event_sink=sink, provider=provider)
    final = await runtime.run(session)

    assert (repo / "new.txt").read_text() == "hi"
    assert final.state is SessionState.COMPLETED_UNVERIFIED
    assert len(transport.requests) == 2
    events_blob = json.dumps([event.to_dict() for event in sink.events], default=str)
    assert SECRET not in events_blob
    for sent in transport.requests:
        assert sent.headers["authorization"] == f"Bearer {SECRET}"
        assert SECRET not in sent.url
        assert SECRET.encode() not in sent.body


async def test_runtime_normalizes_http_failure_into_session_failed(repo) -> None:
    transport = FakeTransport([HttpResponse(401, {}, b'{"error":"unauthorized"}')])
    provider = HttpModelProvider(
        endpoint=ENDPOINT, model_id="test-model", api_key_env=KEY_ENV, transport=transport
    )
    args = build_arg_parser().parse_args(["--repo", str(repo), "task"])
    sink = ListEventSink()
    runtime, session = build_runtime(args, event_sink=sink, provider=provider)
    final = await runtime.run(session)

    assert final.state is SessionState.FAILED
    assert "AUTHENTICATION_FAILED" in (final.completed_status or "")
    failed = sink.of_type(EventType.SESSION_FAILED)
    assert failed
    assert failed[0].payload["provider_error"]["code"] == "AUTHENTICATION_FAILED"
    assert SECRET not in json.dumps([event.to_dict() for event in sink.events], default=str)
