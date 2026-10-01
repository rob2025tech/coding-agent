"""Event shapes and a simple callback sink (docs/contracts.md §40).

A callback sink — *not* an event bus (architecture §9, "avoid premature
infrastructure"). ``EventSink`` is the stable boundary; concrete sinks are
swappable (CLI printer, in-memory list for tests).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class EventType(StrEnum):
    SESSION_STARTED = "SessionStarted"
    MODEL_REQUESTED = "ModelRequested"
    MODEL_RESPONDED = "ModelResponded"
    TOOL_REQUESTED = "ToolRequested"
    PERMISSION_REQUESTED = "PermissionRequested"
    PERMISSION_GRANTED = "PermissionGranted"
    PERMISSION_DENIED = "PermissionDenied"
    TOOL_STARTED = "ToolStarted"
    TOOL_COMPLETED = "ToolCompleted"
    TOOL_FAILED = "ToolFailed"
    VERIFICATION_STARTED = "VerificationStarted"
    VERIFICATION_PASSED = "VerificationPassed"
    VERIFICATION_FAILED = "VerificationFailed"
    SESSION_INTERRUPTED = "SessionInterrupted"
    SESSION_COMPLETED = "SessionCompleted"
    SESSION_FAILED = "SessionFailed"


@dataclass(frozen=True)
class AgentEvent:
    type: EventType
    session_id: str
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    timestamp: dt.datetime = field(default_factory=dt.datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
        }


class EventSink(Protocol):
    def emit(self, event: AgentEvent) -> None: ...


class CallbackEventSink:
    """Adapts a plain ``callable(AgentEvent)`` into an :class:`EventSink`."""

    def __init__(self, callback: Callable[[AgentEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: AgentEvent) -> None:
        self._callback(event)


class ListEventSink:
    """Collects events in memory (deterministic tests, inspection)."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    def types(self) -> list[EventType]:
        return [event.type for event in self.events]

    def of_type(self, event_type: EventType) -> list[AgentEvent]:
        return [event for event in self.events if event.type is event_type]


class NullEventSink:
    """Discards all events."""

    def emit(self, event: AgentEvent) -> None:
        return None
