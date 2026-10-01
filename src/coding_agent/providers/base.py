"""Model provider interface (docs/contracts.md §3.1, D13).

Canonical methods: ``describe()``, ``generate(request)``, ``stream(request)``.
The superseded ``request()`` / ``capabilities()`` / ``model_identity()`` sketch is
**not** reintroduced. No vendor SDK types appear here (module-boundary rule).

``generate`` is async (D1: async at the provider boundary). ``stream`` is
interface-only in v0.1 (D9) — declared, but providers need not implement it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from coding_agent.contracts import (
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    StopReason,
)


@dataclass(frozen=True)
class ModelStreamEvent:
    """A streaming delta (interface-only in v0.1; the mock does not emit these)."""

    delta_text: str = ""
    finish_reason: StopReason | None = None


class ModelProvider(ABC):
    """Replaceable provider seam. Concrete providers must not leak SDK types."""

    @abstractmethod
    def describe(self) -> ModelDescriptor:
        """Return the provider/model descriptor (capabilities, context window)."""

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Produce one complete response for ``request`` (native tool-calling)."""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Interface-only in v0.1 (D9). May be implemented by real providers."""
        raise NotImplementedError("streaming is interface-only in v0.1 (D9)")
