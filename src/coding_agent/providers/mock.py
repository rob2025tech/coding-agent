"""Deterministic mock provider for tests (docs/contracts.md §22, D9, D11).

The mock replays a scripted sequence of :class:`ModelResponse` objects, so both
named scenarios (D11) are scriptable without any network or paid API call:

* **happy path:** read -> edit[approved] -> completion -> verification passes;
* **failure path:** verification fails -> diagnose -> edit -> verification passes.

``generate()`` is async (D1). ``stream()`` is intentionally not implemented (D9).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from coding_agent.contracts import (
    MessageContent,
    ModelCapabilities,
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    StopReason,
    ToolCall,
)
from coding_agent.providers.base import ModelProvider


def tool_call_response(
    name: str,
    arguments: dict[str, object] | None = None,
    *,
    tool_call_id: str | None = None,
) -> ModelResponse:
    """Build a response that requests a single tool call (stop_reason=tool_call)."""
    call = ToolCall(
        tool_call_id=tool_call_id or f"call_{uuid.uuid4().hex[:8]}",
        name=name,
        arguments=dict(arguments or {}),
    )
    return ModelResponse(
        response_id=f"resp_{uuid.uuid4().hex[:8]}",
        stop_reason=StopReason.TOOL_CALL,
        content=[],
        tool_calls=[call],
    )


def final_response(text: str) -> ModelResponse:
    """Build a response carrying a final answer (stop_reason=completed)."""
    return ModelResponse(
        response_id=f"resp_{uuid.uuid4().hex[:8]}",
        stop_reason=StopReason.COMPLETED,
        content=[MessageContent.text_block(text)],
        tool_calls=[],
    )


class MockModelProvider(ModelProvider):
    """Replays a fixed script of responses; records every request it received."""

    def __init__(
        self,
        responses: Sequence[ModelResponse],
        *,
        provider_id: str = "mock",
        model_id: str = "mock-v1",
        context_window: int = 128_000,
    ) -> None:
        self._script: list[ModelResponse] = list(responses)
        self._provider_id = provider_id
        self._model_id = model_id
        self._context_window = context_window
        self.requests: list[ModelRequest] = []

    def describe(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider_id=self._provider_id,
            model_id=self._model_id,
            display_name="Mock Model",
            capabilities=ModelCapabilities(
                text=True, tool_calling=True, streaming=False, vision=False, reasoning=False
            ),
            context_window=self._context_window,
            max_output_tokens=4096,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._script:
            return self._script.pop(0)
        # Script exhausted: end the loop deterministically.
        return final_response("[mock] no further scripted responses")

    @property
    def call_count(self) -> int:
        return len(self.requests)
