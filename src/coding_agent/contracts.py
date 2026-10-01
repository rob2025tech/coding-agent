"""Stable contract data shapes for the coding-agent runtime (v0.1).

This module owns *data shapes only* — enums, dataclasses, and error-code
constants — per ``docs/contracts.md`` and the module-boundary rule that
``contracts.py`` must not contain behavior. Behavior lives in the runtime,
executor, tools, providers, workspace, permissions, and verification modules.

Field names mirror the JSON shapes in ``docs/contracts.md`` exactly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------- #
# Error codes (docs/contracts.md §20)
# --------------------------------------------------------------------------- #


class ErrorCode(StrEnum):
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # edit_file specific (D3 / D17)
    EDIT_NO_MATCH = "EDIT_NO_MATCH"
    EDIT_AMBIGUOUS = "EDIT_AMBIGUOUS"
    EDIT_NOT_UTF8 = "EDIT_NOT_UTF8"


class ProviderErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# Session state machine (docs/contracts.md §31, architecture §5)
# --------------------------------------------------------------------------- #


class SessionState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    MODEL_THINKING = "MODEL_THINKING"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    PERMISSION_CHECK = "PERMISSION_CHECK"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    TOOL_DENIED = "TOOL_DENIED"
    EXECUTING_TOOL = "EXECUTING_TOOL"
    TOOL_RESULT = "TOOL_RESULT"
    VERIFICATION_CHECK = "VERIFICATION_CHECK"
    COMPLETED = "COMPLETED"
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


#: Terminal states (D4). ``TOOL_DENIED`` is deliberately *not* terminal.
TERMINAL_STATES: frozenset[SessionState] = frozenset(
    {
        SessionState.COMPLETED,
        SessionState.COMPLETED_UNVERIFIED,
        SessionState.INTERRUPTED,
        SessionState.FAILED,
    }
)


class StopReason(StrEnum):
    TOOL_CALL = "tool_call"
    COMPLETED = "completed"
    LENGTH = "length"
    ERROR = "error"
    CANCELLED = "cancelled"


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class PermissionDecisionValue(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionClass(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


class SideEffect(StrEnum):
    NONE = "none"
    FILESYSTEM_WRITE = "filesystem_write"
    PROCESS_EXECUTION = "process_execution"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ContentType(StrEnum):
    TEXT = "text"


# --------------------------------------------------------------------------- #
# Provider shapes (docs/contracts.md §4, §5, §6, §7, §8, §21, §24)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelCapabilities:
    text: bool = True
    tool_calling: bool = True
    streaming: bool = False
    vision: bool = False
    reasoning: bool = False


@dataclass(frozen=True)
class ModelDescriptor:
    provider_id: str
    model_id: str
    capabilities: ModelCapabilities
    context_window: int
    display_name: str | None = None
    max_output_tokens: int | None = None
    pricing: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRef:
    """The ``model`` block of a ModelRequest (§6): provider + model identity."""

    provider_id: str
    model_id: str

    @classmethod
    def from_descriptor(cls, descriptor: ModelDescriptor) -> ModelRef:
        return cls(provider_id=descriptor.provider_id, model_id=descriptor.model_id)


@dataclass(frozen=True)
class MessageContent:
    type: ContentType
    text: str

    @classmethod
    def text_block(cls, text: str) -> MessageContent:
        return cls(type=ContentType.TEXT, text=text)


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelMessage:
    role: MessageRole
    content: list[MessageContent] = field(default_factory=list)
    message_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class ModelRequest:
    request_id: str
    session_id: str
    turn_id: str
    model: ModelRef
    system_instructions: str
    messages: list[ModelMessage] = field(default_factory=list)
    tools: list[ToolDefinition] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    response_id: str
    stop_reason: StopReason
    content: list[MessageContent] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return "".join(block.text for block in self.content)


# --------------------------------------------------------------------------- #
# Tool shapes (docs/contracts.md §9, §14, §15, §16, §17, §18, §20)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    side_effect: SideEffect
    permission: PermissionClass


@dataclass(frozen=True)
class PermissionRequest:
    permission_request_id: str
    tool_call_id: str
    tool_name: str
    permission_class: PermissionClass
    reason: str
    risk: RiskLevel = RiskLevel.MEDIUM


@dataclass(frozen=True)
class PermissionDecision:
    decision: PermissionDecisionValue
    reason: str | None = None


@dataclass(frozen=True)
class ToolExecutionRequest:
    execution_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    workspace: str
    timeout_ms: int


@dataclass(frozen=True)
class ToolError:
    code: ErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    status: ToolResultStatus
    output: Any = None
    error: ToolError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.status is ToolResultStatus.SUCCESS

    def counts_as_write(self) -> bool:
        """D12: whether this result invalidates a passing verification."""
        return bool(self.metadata.get("counts_as_write", False))


# --------------------------------------------------------------------------- #
# Verification (docs/contracts.md §35, D4/D12/D15)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    command: str
    output: str
    exit_code: int | None
    ran_at: dt.datetime
    timed_out: bool = False


# --------------------------------------------------------------------------- #
# Limits & session (docs/contracts.md §32, §33, §34)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Limits:
    """Runtime limits with the finalized defaults (D15)."""

    max_turns: int = 20
    max_tool_calls: int = 50
    max_time_s: float = 600.0
    max_repeated_failures: int = 3


@dataclass
class AgentTurn:
    turn_id: str
    session_id: str
    index: int
    started_at: dt.datetime
    model_request: ModelRequest | None = None
    model_response: ModelResponse | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    final_response: str | None = None
    ended_at: dt.datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "index": self.index,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "final_response": self.final_response,
            "tool_calls": [
                {
                    "tool_call_id": tc.tool_call_id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
                for tc in self.tool_calls
            ],
            "tool_results": [
                {
                    "tool_call_id": tr.tool_call_id,
                    "status": tr.status.value,
                    "output": tr.output,
                    "error": (
                        {
                            "code": tr.error.code.value,
                            "message": tr.error.message,
                            "retryable": tr.error.retryable,
                            "details": tr.error.details,
                        }
                        if tr.error
                        else None
                    ),
                    "metadata": tr.metadata,
                }
                for tr in self.tool_results
            ],
        }


@dataclass
class AgentSession:
    session_id: str
    request: str
    repo_root: str
    working_dir: str
    limits: Limits = field(default_factory=Limits)
    state: SessionState = SessionState.CREATED
    history: list[AgentTurn] = field(default_factory=list)
    tool_call_history: list[ToolCall] = field(default_factory=list)
    verification_history: list[VerificationResult] = field(default_factory=list)
    created_at: dt.datetime = field(default_factory=dt.datetime.now)
    updated_at: dt.datetime = field(default_factory=dt.datetime.now)
    completed_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "request": self.request,
            "repo_root": self.repo_root,
            "working_dir": self.working_dir,
            "state": self.state.value,
            "completed_status": self.completed_status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "limits": {
                "max_turns": self.limits.max_turns,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_time_s": self.limits.max_time_s,
                "max_repeated_failures": self.limits.max_repeated_failures,
            },
            "history": [turn.to_dict() for turn in self.history],
            "verification_history": [
                {
                    "passed": vr.passed,
                    "command": vr.command,
                    "output": vr.output,
                    "exit_code": vr.exit_code,
                    "ran_at": vr.ran_at.isoformat(),
                    "timed_out": vr.timed_out,
                }
                for vr in self.verification_history
            ],
        }
