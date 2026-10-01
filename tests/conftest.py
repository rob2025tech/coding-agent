"""Shared pytest fixtures — deterministic, no network, no API keys, no paid calls."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from coding_agent.contracts import ToolExecutionRequest, ToolResult
from coding_agent.events import ListEventSink
from coding_agent.executor import ToolExecutor
from coding_agent.permissions import Approver, AutoApprover, PermissionPolicy
from coding_agent.tools.base import Tool, ToolInvocation
from coding_agent.tools.registry import ToolRegistry, default_registry
from coding_agent.workspace import Workspace


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny fake repo at ``tmp_path/repo`` (so ``tmp_path`` is 'outside')."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "hello.py").write_text("print('Hello')\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "a.txt").write_text("alpha\n", encoding="utf-8")
    return root


@pytest.fixture
def workspace(repo: Path) -> Workspace:
    return Workspace(str(repo))


@pytest.fixture
def pycmd() -> Callable[[str], str]:
    """Build a shell=False-safe ``python -c`` command that round-trips shlex."""

    def _build(code: str) -> str:
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    return _build


@pytest.fixture
def sleep_cmd(pycmd: Callable[[str], str]) -> str:
    return pycmd("import time; time.sleep(30)")


@pytest.fixture
def invoke_tool(workspace: Workspace) -> Callable[..., Awaitable[ToolResult]]:
    """Run a tool directly (bypassing policy) with resolved paths."""

    async def _invoke(
        tool: Tool, arguments: dict, *, timeout_ms: int = 30_000, call_id: str = "c1"
    ) -> ToolResult:
        request = ToolExecutionRequest(
            execution_id="e1",
            tool_call_id=call_id,
            tool_name=tool.definition.name,
            arguments=arguments,
            workspace=workspace.repo_root,
            timeout_ms=timeout_ms,
        )
        paths = {
            key: workspace.resolve(str(arguments[key]))
            for key in tool.path_arguments
            if arguments.get(key) is not None
        }
        invocation = ToolInvocation(
            args=arguments, request=request, workspace=workspace, paths=paths
        )
        return await tool.execute(invocation)

    return _invoke


@pytest.fixture
def make_executor(workspace: Workspace) -> Callable[..., ToolExecutor]:
    """Build a ToolExecutor with a chosen approver / registry / timeout / sink."""

    def _make(
        *,
        approver: Approver | None = None,
        registry: ToolRegistry | None = None,
        timeout_ms: int = 30_000,
        sink: ListEventSink | None = None,
    ) -> ToolExecutor:
        return ToolExecutor(
            registry=registry or default_registry(),
            workspace=workspace,
            policy=PermissionPolicy(),
            approver=approver or AutoApprover(),
            event_sink=sink or ListEventSink(),
            default_timeout_ms=timeout_ms,
        )

    return _make
