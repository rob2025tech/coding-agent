"""Terminal CLI entry point (docs/contracts.md §37; D7/D14; architecture §4.1).

Flags required by the contracts:
  --verify "<cmd>"        human-supplied verify command (single argv, D14)
  --verify-timeout <s>    verifier timeout in seconds (default 120, R3)
  --yes                   auto-approve ASK only, never DENY (D7)

The default provider is the deterministic offline ``mock``. Selecting ``http``
talks to a real endpoint configured via environment variables (D21); the API key
is read from ``CODING_AGENT_API_KEY`` and is never a CLI argument.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from coding_agent.context import ContextBuilder
from coding_agent.contracts import AgentSession, Limits, ModelRef, SessionState
from coding_agent.events import AgentEvent, CallbackEventSink, EventSink, NullEventSink
from coding_agent.executor import ToolExecutor
from coding_agent.permissions import CliApprover, PermissionPolicy
from coding_agent.providers.base import ModelProvider
from coding_agent.providers.http import HttpModelProvider
from coding_agent.providers.mock import MockModelProvider
from coding_agent.runtime import AgentRuntime, new_session_id
from coding_agent.tools.registry import default_registry
from coding_agent.verification import DEFAULT_VERIFY_TIMEOUT_S, Verifier
from coding_agent.workspace import Workspace


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent", description="Run the coding-agent runtime (v0.1)."
    )
    parser.add_argument("request", nargs="?", default="", help="Task/request for the agent.")
    parser.add_argument("--repo", default=".", help="Repository root (workspace). Default: cwd.")
    parser.add_argument(
        "--provider",
        default="mock",
        choices=["mock", "http"],
        help="Model provider: mock (default, offline) or http (real endpoint via env).",
    )
    parser.add_argument(
        "--verify", default=None, help="Human-supplied verify command (single argv; D14)."
    )
    parser.add_argument(
        "--verify-timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT_S,
        dest="verify_timeout",
        help="Verifier timeout in seconds (default: 120).",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Auto-approve ASK prompts (never DENY; D7)."
    )
    # Limit overrides fall back to the centralized Limits defaults (no magic numbers).
    parser.add_argument("--max-turns", type=int, default=None, dest="max_turns")
    parser.add_argument("--max-tool-calls", type=int, default=None, dest="max_tool_calls")
    parser.add_argument("--max-time", type=float, default=None, dest="max_time_s")
    parser.add_argument("--quiet", action="store_true", help="Do not print events to stderr.")
    return parser


def _build_provider(name: str) -> ModelProvider:
    if name == "mock":
        return MockModelProvider([])
    if name == "http":
        return _build_http_provider()
    raise SystemExit(f"unknown provider: {name} (available: mock, http)")


def _build_http_provider() -> ModelProvider:
    """Build the HTTP provider from non-secret env config (D21).

    ``CODING_AGENT_ENDPOINT`` (full request URL) and ``CODING_AGENT_MODEL`` are
    required here; the secret ``CODING_AGENT_API_KEY`` is read lazily by the
    provider at call time, so it is never a CLI argument and never stored here.
    """
    endpoint = os.environ.get("CODING_AGENT_ENDPOINT", "")
    model = os.environ.get("CODING_AGENT_MODEL", "")
    if not endpoint or not model:
        raise SystemExit(
            "http provider requires CODING_AGENT_ENDPOINT and CODING_AGENT_MODEL "
            "environment variables"
        )
    return HttpModelProvider(endpoint=endpoint, model_id=model)


def _resolve_limits(args: argparse.Namespace) -> Limits:
    base = Limits()
    return Limits(
        max_turns=base.max_turns if args.max_turns is None else args.max_turns,
        max_tool_calls=base.max_tool_calls if args.max_tool_calls is None else args.max_tool_calls,
        max_time_s=base.max_time_s if args.max_time_s is None else args.max_time_s,
        max_repeated_failures=base.max_repeated_failures,
    )


def _print_event(event: AgentEvent) -> None:
    parts = " ".join(f"{key}={value}" for key, value in event.payload.items())
    line = f"[{event.type.value}] {parts}".rstrip()
    print(line, file=sys.stderr)


def _make_sink(args: argparse.Namespace) -> EventSink:
    if args.quiet:
        return NullEventSink()
    return CallbackEventSink(_print_event)


def build_runtime(
    args: argparse.Namespace,
    *,
    event_sink: EventSink,
    provider: ModelProvider | None = None,
) -> tuple[AgentRuntime, AgentSession]:
    """Assemble the runtime + a fresh session from parsed CLI args."""
    workspace = Workspace(args.repo)
    registry = default_registry()
    policy = PermissionPolicy()
    approver = CliApprover(auto_yes=args.yes)
    verifier = Verifier(
        args.verify, repo_root=workspace.repo_root, timeout_s=args.verify_timeout
    )
    model_provider = provider or _build_provider(args.provider)
    context_builder = ContextBuilder(ModelRef.from_descriptor(model_provider.describe()))
    executor = ToolExecutor(
        registry=registry,
        workspace=workspace,
        policy=policy,
        approver=approver,
        event_sink=event_sink,
    )
    runtime = AgentRuntime(
        provider=model_provider,
        registry=registry,
        executor=executor,
        context_builder=context_builder,
        verifier=verifier,
        event_sink=event_sink,
    )
    session = AgentSession(
        session_id=new_session_id(),
        request=args.request,
        repo_root=workspace.repo_root,
        working_dir=workspace.repo_root,
        limits=_resolve_limits(args),
    )
    return runtime, session


def _exit_code(state: SessionState) -> int:
    if state in (SessionState.COMPLETED, SessionState.COMPLETED_UNVERIFIED):
        return 0
    return 1


def _final_answer(session: AgentSession) -> str | None:
    """The last final-answer text recorded on the turn history, if any.

    The runtime stores the model's final answer as ``AgentTurn.final_response``
    (§33) when a turn ends without tool calls; no new session field is invented.
    """
    for turn in reversed(session.history):
        if turn.final_response is not None:
            return turn.final_response
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    event_sink = _make_sink(args)
    runtime, session = build_runtime(args, event_sink=event_sink)
    final = asyncio.run(runtime.run(session))
    # The model's final answer is the user-facing result: stdout only, never
    # duplicated into the stderr event telemetry. Quiet suppresses all output.
    if not args.quiet:
        answer = _final_answer(final)
        if answer:
            print(answer)
    if not args.quiet:
        print(
            f"[session {final.session_id}] {final.state.value}: {final.completed_status}",
            file=sys.stderr,
        )
    return _exit_code(final.state)


if __name__ == "__main__":
    raise SystemExit(main())
