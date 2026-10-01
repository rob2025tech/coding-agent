"""Tool registry and the v0.1 default tool set (D2)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from coding_agent.contracts import ToolDefinition
from coding_agent.tools.base import Tool
from coding_agent.tools.filesystem import EditFileTool, ListFilesTool, ReadFileTool
from coding_agent.tools.search import SearchTool
from coding_agent.tools.shell import ShellTool


class ToolRegistry:
    """Name -> Tool lookup. The executor resolves tool calls through it."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())


def default_tools() -> list[Tool]:
    """The v0.1 tool set: list_files, read_file, search, edit_file, shell (D2).

    ``write_file`` / ``delete_file`` are deferred (docs/contracts.md §30).
    """
    return [ListFilesTool(), ReadFileTool(), SearchTool(), EditFileTool(), ShellTool()]


def default_registry() -> ToolRegistry:
    return ToolRegistry(default_tools())
