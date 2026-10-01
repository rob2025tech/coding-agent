"""Workspace path resolution and containment (docs/contracts.md §36).

Pure and synchronous (D1). The workspace guarantee applies to **file tools**
only; the ``shell`` tool is gated by human approval, not contained (D5,
architecture §6).
"""

from __future__ import annotations

import os

from coding_agent.errors import PathOutsideWorkspaceError


def _is_within(root: str, target: str) -> bool:
    """True iff ``target`` is ``root`` itself or a descendant of ``root``."""
    root_n = os.path.normpath(root)
    target_n = os.path.normpath(target)
    if target_n == root_n:
        return True
    return target_n.startswith(root_n + os.sep)


class Workspace:
    """Resolves and confines file-tool paths to ``repo_root``."""

    def __init__(self, repo_root: str) -> None:
        self.repo_root: str = os.path.abspath(os.path.expanduser(repo_root))
        self._real_root: str = os.path.realpath(self.repo_root)

    def resolve(self, path: str) -> str:
        """Normalize ``path`` under ``repo_root``.

        Raises :class:`PathOutsideWorkspaceError` if the path escapes
        ``repo_root`` — including via ``..`` traversal or a symlink whose real
        target lies outside the workspace (symlink-escape rejection, §36).
        """
        candidate = os.path.expanduser(path)
        joined = candidate if os.path.isabs(candidate) else os.path.join(self.repo_root, candidate)
        normalized = os.path.normpath(joined)

        # 1. lexical containment (catches `..` escapes)
        if not _is_within(self.repo_root, normalized):
            raise PathOutsideWorkspaceError(path)

        # 2. symlink-escape rejection (catches links pointing outside)
        real = os.path.realpath(normalized)
        if not _is_within(self._real_root, real):
            raise PathOutsideWorkspaceError(path)

        return normalized

    def contains(self, path: str) -> bool:
        """True iff ``resolve(path)`` stays within ``repo_root``."""
        try:
            self.resolve(path)
        except PathOutsideWorkspaceError:
            return False
        return True
