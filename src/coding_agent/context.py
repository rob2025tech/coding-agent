"""Context assembly (docs/contracts.md §39, architecture §4.7).

``ContextBuilder.build`` is pure and synchronous (D1): it folds the session
history — user request, assistant turns, tool results, and verification results —
plus the tool list into a single ``ModelRequest``. No I/O.
"""

from __future__ import annotations

import json
import uuid

from coding_agent.contracts import (
    AgentSession,
    MessageContent,
    MessageRole,
    ModelMessage,
    ModelRef,
    ModelRequest,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    VerificationResult,
)

DEFAULT_SYSTEM_PROMPT = (
    "You are a coding agent operating in a terminal. You change a repository only "
    "by calling the provided tools. The runtime — not you — decides permissions and "
    "runs verification. Read before you edit, make exact and minimal edits, and when "
    "the task is complete reply with a final message and stop calling tools."
)


def _render_tool_result(result: ToolResult) -> str:
    if result.status is ToolResultStatus.SUCCESS:
        try:
            rendered = json.dumps(result.output, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = str(result.output)
        return f"[tool_result success] {rendered}"
    error = result.error
    code = error.code.value if error else result.status.value
    message = error.message if error else ""
    return f"[tool_result {result.status.value}] {code}: {message}"


def _render_verification(result: VerificationResult) -> str:
    if result.passed:
        status = "PASSED"
    elif result.timed_out:
        status = "TIMEOUT"
    else:
        status = "FAILED"
    return (
        f"[verification {status}] command={result.command!r} "
        f"exit_code={result.exit_code}\n{result.output}"
    )


class ContextBuilder:
    """Builds the :class:`ModelRequest` for the next model turn."""

    def __init__(
        self, model: ModelRef, *, system_instructions: str = DEFAULT_SYSTEM_PROMPT
    ) -> None:
        self._model = model
        self._system_instructions = system_instructions

    def build(self, session: AgentSession, tools: list[ToolDefinition]) -> ModelRequest:
        messages: list[ModelMessage] = [
            ModelMessage(
                role=MessageRole.USER,
                content=[MessageContent.text_block(session.request)],
            )
        ]

        verification_index = 0
        for turn in session.history:
            response = turn.model_response
            if response is not None:
                messages.append(
                    ModelMessage(
                        role=MessageRole.ASSISTANT,
                        content=list(response.content),
                        message_id=response.response_id,
                        tool_calls=list(response.tool_calls),
                    )
                )
            for result in turn.tool_results:
                messages.append(
                    ModelMessage(
                        role=MessageRole.TOOL,
                        content=[MessageContent.text_block(_render_tool_result(result))],
                    )
                )
            if turn.final_response:
                messages.append(
                    ModelMessage(
                        role=MessageRole.ASSISTANT,
                        content=[MessageContent.text_block(turn.final_response)],
                    )
                )
            # Only a final-answer turn (no tool calls) triggers runtime verification,
            # and the runtime appends that result to ``verification_history`` right
            # after the turn. Render each verification immediately after the turn
            # that produced it so the model sees the true causal chronology — and a
            # failed verification stays visible to the very next turn (D11).
            if not turn.tool_calls and verification_index < len(session.verification_history):
                verification = session.verification_history[verification_index]
                verification_index += 1
                messages.append(
                    ModelMessage(
                        role=MessageRole.TOOL,
                        content=[MessageContent.text_block(_render_verification(verification))],
                    )
                )

        return ModelRequest(
            request_id=f"req_{uuid.uuid4().hex[:8]}",
            session_id=session.session_id,
            turn_id=f"turn_{uuid.uuid4().hex[:8]}",
            model=self._model,
            system_instructions=self._system_instructions,
            messages=messages,
            tools=list(tools),
            options={},
            metadata={},
        )
