"""``search`` tool: regex line search over UTF-8 text files in the workspace."""

from __future__ import annotations

import os
import re
from typing import Any

from coding_agent.contracts import (
    ErrorCode,
    PermissionClass,
    SideEffect,
    ToolDefinition,
    ToolResult,
)
from coding_agent.tools.base import Tool, ToolInvocation, error_result, success_result

_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv", ".mypy_cache"})
_MAX_MATCHES = 200


class SearchTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search",
            description="Search UTF-8 text files under a path for a regular expression.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            side_effect=SideEffect.NONE,
            permission=PermissionClass.READ,
        )

    @property
    def path_arguments(self) -> tuple[str, ...]:
        return ("path",)

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        pattern = str(invocation.args["pattern"])
        repo_root = invocation.workspace.repo_root
        root = invocation.paths.get("path") or repo_root

        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return error_result(
                invocation.tool_call_id, ErrorCode.INVALID_ARGUMENTS, f"invalid pattern: {exc}"
            )

        matches: list[dict[str, Any]] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
            )
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    with open(full, "rb") as handle:
                        data = handle.read()
                except OSError:
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                rel = os.path.relpath(full, repo_root)
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        matches.append({"path": rel, "line": lineno, "text": line})
                        if len(matches) >= _MAX_MATCHES:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break

        return success_result(
            invocation.tool_call_id,
            {"pattern": pattern, "matches": matches, "truncated": truncated},
        )
