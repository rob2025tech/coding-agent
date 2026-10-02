"""Minimal real HTTP model provider (architecture §4.3/§7; contracts §3.1/§25; D21).

A concrete :class:`~coding_agent.providers.base.ModelProvider` that speaks a single
tool-capable HTTP wire format (OpenAI-compatible ``chat/completions``) using only
the Python standard library. It *adapts* the HTTP API to the existing
provider-neutral contracts; the core runtime, executor, permissions, workspace,
verification, and context layers are unchanged.

Design constraints (D21):

* stdlib-only transport (``urllib.request`` run via :func:`asyncio.to_thread`),
  so no third-party dependency is added (D1);
* the transport is injectable, keeping tests deterministic and fully offline;
* the API key is read from an environment variable at call time and sent **only**
  as an ``Authorization`` header — it never enters a URL, a contract type,
  ``metadata``, an event, a tool result, or a :class:`ProviderError`;
* failures are normalized into the existing §25
  :class:`~coding_agent.contracts.ProviderErrorCode` vocabulary by raising
  :class:`~coding_agent.errors.ProviderExecutionError`, so the runtime never sees
  a provider-specific exception type;
* ``stream()`` is intentionally left unimplemented (D9: interface-only).

Provider-specific HTTP/JSON details live entirely in this module; only contract
types cross back into the runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from coding_agent.contracts import (
    MessageContent,
    MessageRole,
    ModelCapabilities,
    ModelDescriptor,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorCode,
    StopReason,
    ToolCall,
    Usage,
)
from coding_agent.errors import ProviderExecutionError
from coding_agent.providers.base import ModelProvider


@dataclass(frozen=True)
class HttpRequest:
    """A provider-neutral HTTP request handed to the transport."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class HttpResponse:
    """A provider-neutral HTTP response returned by the transport."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


#: Injectable transport seam: a synchronous callable run in a worker thread so the
#: awaited ``generate()`` never blocks the event loop. Tests replace it wholesale.
Transport = Callable[[HttpRequest], HttpResponse]

#: Default finite HTTP timeout (seconds). Bounds a hung provider so the runtime's
#: wall-clock limit stays meaningful; a timeout normalizes to ``NETWORK_ERROR``.
DEFAULT_HTTP_TIMEOUT_S = 60.0

#: Environment variable holding the API key. Never a CLI argument (D21).
DEFAULT_API_KEY_ENV = "CODING_AGENT_API_KEY"

_FINISH_REASON_TO_STOP: dict[str, StopReason] = {
    "tool_calls": StopReason.TOOL_CALL,
    "function_call": StopReason.TOOL_CALL,
    "stop": StopReason.COMPLETED,
    "length": StopReason.LENGTH,
    "cancelled": StopReason.CANCELLED,
}


class HttpModelProvider(ModelProvider):
    """A single-endpoint HTTP provider speaking an OpenAI-compatible format.

    This is *one* concrete adapter behind the existing seam — not a provider
    framework, registry, or router (D21). Non-secret configuration (endpoint,
    model id) is supplied by the caller; the secret API key is read from
    ``api_key_env`` at call time and never stored on a contract object.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model_id: str,
        provider_id: str = "http",
        api_key_env: str = DEFAULT_API_KEY_ENV,
        context_window: int = 128_000,
        max_output_tokens: int = 4096,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        transport: Transport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._model_id = model_id
        self._provider_id = provider_id
        self._api_key_env = api_key_env
        self._context_window = context_window
        self._max_output_tokens = max_output_tokens
        self._timeout_s = timeout_s
        self._transport: Transport = transport or self._urllib_transport

    # -- ModelProvider surface ---------------------------------------------- #

    def describe(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider_id=self._provider_id,
            model_id=self._model_id,
            display_name=f"HTTP {self._model_id}",
            capabilities=ModelCapabilities(
                text=True, tool_calling=True, streaming=False, vision=False, reasoning=False
            ),
            context_window=self._context_window,
            max_output_tokens=self._max_output_tokens,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        api_key = os.environ.get(self._api_key_env, "")
        if not api_key:
            # Missing credentials are a deterministic auth failure; the endpoint is
            # never contacted, so the transport cannot leak an absent secret.
            raise ProviderExecutionError(
                ProviderError(
                    code=ProviderErrorCode.AUTHENTICATION_FAILED,
                    message=f"missing API key environment variable: {self._api_key_env}",
                    provider_id=self._provider_id,
                )
            )
        http_request = self._build_http_request(request, api_key)
        try:
            http_response = await asyncio.to_thread(self._transport, http_request)
        except asyncio.CancelledError:
            raise  # never swallow cancellation into a normalized provider error
        except OSError as exc:  # URLError, timeouts, and connection errors are OSError subclasses
            raise ProviderExecutionError(
                ProviderError(
                    code=ProviderErrorCode.NETWORK_ERROR,
                    message=f"network error contacting provider: {type(exc).__name__}",
                    provider_id=self._provider_id,
                    metadata={"exception_type": type(exc).__name__},
                )
            ) from None
        return self._to_model_response(http_response)

    # ``stream()`` is inherited from ModelProvider and raises NotImplementedError (D9).

    # -- transport (stdlib) -------------------------------------------------- #

    def _urllib_transport(self, request: HttpRequest) -> HttpResponse:
        req = urllib.request.Request(
            request.url, data=request.body, headers=request.headers, method=request.method
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                return HttpResponse(
                    status=int(resp.status),
                    headers={key.lower(): value for key, value in resp.headers.items()},
                    body=resp.read(),
                )
        except urllib.error.HTTPError as exc:  # a real HTTP response carrying an error status
            items = exc.headers.items() if exc.headers is not None else []
            return HttpResponse(
                status=int(exc.code),
                headers={str(key).lower(): str(value) for key, value in items},
                body=exc.read(),
            )

    # -- request building ---------------------------------------------------- #

    def _build_http_request(self, request: ModelRequest, api_key: str) -> HttpRequest:
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            # The secret lives ONLY in this header — never in the URL or the body.
            "authorization": f"Bearer {api_key}",
        }
        payload = self._build_payload(request)
        return HttpRequest(
            method="POST",
            url=self._endpoint,
            headers=headers,
            body=json.dumps(payload).encode("utf-8"),
        )

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        model = request.model.model_id or self._model_id
        messages: list[dict[str, Any]] = []
        if request.system_instructions:
            messages.append({"role": "system", "content": request.system_instructions})
        messages.extend(self._map_messages(request.messages))
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _map_messages(self, messages: list[ModelMessage]) -> list[dict[str, Any]]:
        """Map neutral messages onto the wire format, pairing tool results by id.

        The runtime appends tool results in the same order as the assistant's
        ``tool_calls``; verification results are also rendered as ``tool``-role
        messages but have no matching call. Unpaired tool messages therefore fall
        back to a ``user`` turn carrying the already-rendered text, so the request
        stays valid for a strict chat/completions server.
        """
        mapped: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        for message in messages:
            text = "".join(block.text for block in message.content)
            if message.role is MessageRole.ASSISTANT:
                entry: dict[str, Any] = {"role": "assistant", "content": text or None}
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": call.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in message.tool_calls
                    ]
                    pending_ids = [call.tool_call_id for call in message.tool_calls]
                mapped.append(entry)
            elif message.role is MessageRole.TOOL:
                # Pair strictly by the preceding assistant tool_call_id; a TOOL
                # message's own message_id is never a tool_call_id.
                call_id = pending_ids.pop(0) if pending_ids else ""
                if call_id:
                    mapped.append({"role": "tool", "tool_call_id": call_id, "content": text})
                else:
                    mapped.append({"role": "user", "content": text})
            elif message.role is MessageRole.SYSTEM:
                mapped.append({"role": "system", "content": text})
            else:  # USER
                mapped.append({"role": "user", "content": text})
        return mapped

    # -- response translation ------------------------------------------------ #

    def _to_model_response(self, response: HttpResponse) -> ModelResponse:
        if response.status != 200:
            raise self._error_for_status(response)
        try:
            body = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._malformed(
                f"malformed provider response: {type(exc).__name__}", 200
            ) from None
        return self._parse_response(body, 200)

    def _parse_response(self, body: Any, status: int) -> ModelResponse:
        if not isinstance(body, dict):
            raise self._malformed("malformed provider response: not an object", status)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise self._malformed("malformed provider response: no choices", status)
        first = choices[0]
        if not isinstance(first, dict):
            raise self._malformed("malformed provider response: bad choice", status)
        message = first.get("message")
        if not isinstance(message, dict):
            raise self._malformed("malformed provider response: no message", status)

        content: list[MessageContent] = []
        text = message.get("content")
        if isinstance(text, str) and text:
            content.append(MessageContent.text_block(text))

        tool_calls = self._parse_tool_calls(message.get("tool_calls"), status)

        finish = first.get("finish_reason")
        if not isinstance(finish, str) or finish not in _FINISH_REASON_TO_STOP:
            # Only recognized finish reasons are valid; anything else is malformed.
            raise self._malformed("malformed provider response: unknown finish_reason", status)
        stop_reason = _FINISH_REASON_TO_STOP[finish]
        if tool_calls and stop_reason is StopReason.COMPLETED:
            stop_reason = StopReason.TOOL_CALL

        response_id = body.get("id")
        if not isinstance(response_id, str) or not response_id:
            # The provider response id is required; never fabricate an empty one.
            raise self._malformed("malformed provider response: missing id", status)
        return ModelResponse(
            response_id=response_id,
            stop_reason=stop_reason,
            content=content,
            tool_calls=tool_calls,
            usage=self._parse_usage(body.get("usage")),
        )

    def _parse_tool_calls(self, raw: Any, status: int) -> list[ToolCall]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise self._malformed("malformed provider response: bad tool_calls", status)
        calls: list[ToolCall] = []
        for item in raw:
            if not isinstance(item, dict):
                raise self._malformed("malformed provider response: bad tool_call", status)
            function = item.get("function")
            if not isinstance(function, dict):
                raise self._malformed("malformed provider response: bad tool_call function", status)
            call_id = item.get("id")
            name = function.get("name")
            calls.append(
                ToolCall(
                    tool_call_id=call_id if isinstance(call_id, str) else "",
                    name=name if isinstance(name, str) else "",
                    arguments=self._parse_arguments(function.get("arguments", "{}"), status),
                )
            )
        return calls

    def _parse_arguments(self, raw_args: Any, status: int) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            if not raw_args.strip():
                return {}
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                raise self._malformed(
                    f"malformed tool_call arguments: {type(exc).__name__}", status
                ) from None
            if isinstance(parsed, dict):
                return parsed
        raise self._malformed("malformed tool_call arguments: not an object", status)

    def _parse_usage(self, raw: Any) -> Usage | None:
        if not isinstance(raw, dict):
            return None
        return Usage(
            input_tokens=_as_int(raw.get("prompt_tokens")),
            output_tokens=_as_int(raw.get("completion_tokens")),
            cached_input_tokens=_as_int(raw.get("cached_input_tokens")),
        )

    # -- error mapping (§25 vocabulary; conservative on ambiguity) ----------- #

    def _error_for_status(self, response: HttpResponse) -> ProviderExecutionError:
        status = response.status
        retryable = False
        retry_after_ms: int | None = None
        code: ProviderErrorCode
        if status in (401, 403):
            code = ProviderErrorCode.AUTHENTICATION_FAILED
        elif status == 429:
            code = ProviderErrorCode.RATE_LIMITED
            retryable = True
            retry_after_ms = _retry_after_ms(response.headers.get("retry-after"))
        elif status == 413:
            code = ProviderErrorCode.CONTEXT_TOO_LARGE
        elif status == 400:
            code = (
                ProviderErrorCode.CONTEXT_TOO_LARGE
                if _signals_context_overflow(response.body)
                else ProviderErrorCode.INVALID_REQUEST
            )
        elif status == 404:
            # An explicit model-not-found signal -> MODEL_UNAVAILABLE; a generic or
            # ambiguous 404 (e.g. a wrong path) -> conservative PROVIDER_ERROR.
            code = (
                ProviderErrorCode.MODEL_UNAVAILABLE
                if _signals_model_not_found(response.body)
                else ProviderErrorCode.PROVIDER_ERROR
            )
        elif status == 408:
            code = ProviderErrorCode.NETWORK_ERROR
        else:
            # 5xx and any unclassified status: conservative fallback.
            code = ProviderErrorCode.PROVIDER_ERROR
        return ProviderExecutionError(
            ProviderError(
                code=code,
                # Generic message: never echo the raw body or authorization info.
                message=f"provider returned HTTP {status}",
                retryable=retryable,
                retry_after_ms=retry_after_ms,
                provider_id=self._provider_id,
                metadata={"http_status": status},
            )
        )

    def _malformed(self, message: str, status: int) -> ProviderExecutionError:
        return ProviderExecutionError(
            ProviderError(
                code=ProviderErrorCode.PROVIDER_ERROR,
                message=message,
                provider_id=self._provider_id,
                metadata={"http_status": status},
            )
        )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _retry_after_ms(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _signals_context_overflow(body: bytes) -> bool:
    """Conservative body inspection for a context-length signal (never echoed)."""
    text = body.decode("utf-8", "replace").lower()
    return "context" in text and (
        "length" in text or "exceed" in text or "too many tokens" in text
    )


def _signals_model_not_found(body: bytes) -> bool:
    """Conservative body inspection for an explicit model-not-found signal."""
    text = body.decode("utf-8", "replace").lower()
    if "model_not_found" in text or "no such model" in text:
        return True
    return "model" in text and (
        "does not exist" in text or "not found" in text or "not_found" in text
    )
