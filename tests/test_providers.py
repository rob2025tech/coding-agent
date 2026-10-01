"""Provider abstraction tests (docs/contracts.md §3.1/§22; D1/D9/D13).

Deterministic; no network, no API keys, no paid calls. Verifies the canonical
interface (describe/generate/stream), the mock's scripted replay, and that the
superseded request()/capabilities()/model_identity() sketch stays absent.
"""

from __future__ import annotations

import inspect
import json

import pytest

from coding_agent.contracts import (
    ModelCapabilities,
    ModelDescriptor,
    ModelRef,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorCode,
    StopReason,
)
from coding_agent.errors import ProviderExecutionError, normalize_provider_failure
from coding_agent.providers.base import ModelProvider
from coding_agent.providers.mock import (
    MockModelProvider,
    final_response,
    tool_call_response,
)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="r1",
        session_id="s1",
        turn_id="t1",
        model=ModelRef(provider_id="mock", model_id="mock-v1"),
        system_instructions="sys",
    )


# --- canonical interface (D13): describe / generate / stream, nothing superseded --- #


def test_interface_has_canonical_methods_only() -> None:
    assert hasattr(ModelProvider, "describe")
    assert hasattr(ModelProvider, "generate")
    assert hasattr(ModelProvider, "stream")
    for legacy in ("request", "capabilities", "model_identity"):
        assert not hasattr(ModelProvider, legacy)


def test_generate_is_the_async_boundary_d1() -> None:
    assert inspect.iscoroutinefunction(ModelProvider.generate)


def test_abstract_provider_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ModelProvider()  # type: ignore[abstract]


# --- describe() --- #


def test_describe_returns_descriptor() -> None:
    descriptor = MockModelProvider([]).describe()
    assert isinstance(descriptor, ModelDescriptor)
    assert descriptor.provider_id == "mock"
    assert descriptor.model_id == "mock-v1"
    assert isinstance(descriptor.capabilities, ModelCapabilities)
    assert descriptor.capabilities.tool_calling is True
    assert descriptor.context_window > 0


def test_describe_reflects_constructor_identity() -> None:
    provider = MockModelProvider([], provider_id="p", model_id="m", context_window=999)
    descriptor = provider.describe()
    assert (descriptor.provider_id, descriptor.model_id) == ("p", "m")
    assert descriptor.context_window == 999


def test_model_ref_from_descriptor() -> None:
    ref = ModelRef.from_descriptor(MockModelProvider([]).describe())
    assert (ref.provider_id, ref.model_id) == ("mock", "mock-v1")


# --- generate(): deterministic scripted replay --- #


async def test_generate_replays_script_in_order() -> None:
    provider = MockModelProvider([final_response("one"), final_response("two")])
    first = await provider.generate(_request())
    second = await provider.generate(_request())
    assert first.text() == "one"
    assert second.text() == "two"
    assert provider.call_count == 2


async def test_generate_records_requests() -> None:
    provider = MockModelProvider([final_response("x")])
    request = _request()
    await provider.generate(request)
    assert provider.requests == [request]


async def test_generate_after_script_exhausted_is_final() -> None:
    provider = MockModelProvider([final_response("only")])
    await provider.generate(_request())  # consumes the single scripted response
    extra = await provider.generate(_request())  # deterministic fallback
    assert extra.stop_reason is StopReason.COMPLETED
    assert extra.tool_calls == []


# --- response builders --- #


def test_tool_call_response_shape() -> None:
    response = tool_call_response("read_file", {"path": "a"}, tool_call_id="c1")
    assert response.stop_reason is StopReason.TOOL_CALL
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.tool_call_id, call.name, call.arguments) == ("c1", "read_file", {"path": "a"})


def test_final_response_shape() -> None:
    response = final_response("done")
    assert response.stop_reason is StopReason.COMPLETED
    assert response.tool_calls == []
    assert response.text() == "done"


# --- stream(): interface-only in v0.1 (D9) --- #


def test_stream_is_interface_only() -> None:
    with pytest.raises(NotImplementedError):
        MockModelProvider([]).stream(_request())


# --- replaceability: any concrete provider satisfies the seam (D13) --- #


class _CannedProvider(ModelProvider):
    def describe(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider_id="canned",
            model_id="c1",
            capabilities=ModelCapabilities(text=True, tool_calling=True),
            context_window=10,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return final_response("canned")


async def test_provider_is_replaceable() -> None:
    provider = _CannedProvider()
    assert isinstance(provider, ModelProvider)
    response = await provider.generate(_request())
    assert response.text() == "canned"


# --- §25 provider error contract: exact normalized code vocabulary --- #


def test_provider_error_code_vocabulary_is_exact() -> None:
    assert {code.value for code in ProviderErrorCode} == {
        "AUTHENTICATION_FAILED",
        "INVALID_REQUEST",
        "RATE_LIMITED",
        "CONTEXT_TOO_LARGE",
        "MODEL_UNAVAILABLE",
        "NETWORK_ERROR",
        "PROVIDER_ERROR",
        "CANCELLED",
    }


def test_superseded_provider_error_values_absent() -> None:
    values = {code.value for code in ProviderErrorCode}
    for legacy in ("RATE_LIMIT_EXCEEDED", "INVALID_RESPONSE", "PROVIDER_UNAVAILABLE"):
        assert legacy not in values


# --- §25 normalized provider-error representation --- #


def test_provider_error_representation_and_to_dict() -> None:
    error = ProviderError(
        code=ProviderErrorCode.RATE_LIMITED,
        message="Provider rate limit reached.",
        retryable=True,
        retry_after_ms=5000,
        provider_id="example-provider",
        metadata={"scope": "chat"},
    )
    assert error.to_dict() == {
        "code": "RATE_LIMITED",
        "message": "Provider rate limit reached.",
        "retryable": True,
        "retry_after_ms": 5000,
        "provider_id": "example-provider",
        "metadata": {"scope": "chat"},
    }
    assert json.dumps(error.to_dict())  # JSON serializable


def test_provider_error_defaults() -> None:
    error = ProviderError(code=ProviderErrorCode.PROVIDER_ERROR, message="x")
    assert error.retryable is False
    assert error.retry_after_ms is None
    assert error.provider_id == ""
    assert error.metadata == {}


# --- normalization at the provider boundary --- #


def test_normalize_preserves_already_normalized_error() -> None:
    original = ProviderError(
        code=ProviderErrorCode.CONTEXT_TOO_LARGE,
        message="too big",
        provider_id="p",
        metadata={"tokens": 999},
    )
    normalized = normalize_provider_failure(
        ProviderExecutionError(original), provider_id="ignored"
    )
    assert normalized == original  # preserved verbatim when provider_id is present


def test_normalize_fills_missing_provider_id() -> None:
    original = ProviderError(code=ProviderErrorCode.NETWORK_ERROR, message="dns")
    normalized = normalize_provider_failure(ProviderExecutionError(original), provider_id="acme")
    assert normalized.code is ProviderErrorCode.NETWORK_ERROR
    assert normalized.provider_id == "acme"


def test_normalize_generic_exception_is_provider_error() -> None:
    class VendorSDKError(Exception):
        pass

    normalized = normalize_provider_failure(VendorSDKError("kaboom"), provider_id="acme")
    assert normalized.code is ProviderErrorCode.PROVIDER_ERROR
    assert normalized.provider_id == "acme"
    assert normalized.retryable is False
    # The provider-specific *type name* is retained in metadata, never the class.
    assert normalized.metadata["exception_type"] == "VendorSDKError"
    assert "kaboom" in normalized.message
