"""Workspace resolution / containment tests (docs/contracts.md §36)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.errors import PathOutsideWorkspaceError
from coding_agent.workspace import Workspace


def test_resolve_relative_path_stays_inside(workspace: Workspace) -> None:
    resolved = workspace.resolve("hello.py")
    assert resolved == os.path.join(workspace.repo_root, "hello.py")


def test_resolve_nested_relative(workspace: Workspace) -> None:
    assert workspace.resolve("data/a.txt").endswith(os.path.join("data", "a.txt"))


def test_dotdot_escape_raises(workspace: Workspace) -> None:
    with pytest.raises(PathOutsideWorkspaceError):
        workspace.resolve("../outside.txt")


def test_absolute_inside_ok(workspace: Workspace) -> None:
    inside = os.path.join(workspace.repo_root, "data", "a.txt")
    assert workspace.resolve(inside) == os.path.normpath(inside)


def test_absolute_outside_raises(workspace: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")
    with pytest.raises(PathOutsideWorkspaceError):
        workspace.resolve(str(outside))


def test_contains(workspace: Workspace) -> None:
    assert workspace.contains("hello.py") is True
    assert workspace.contains("data/a.txt") is True
    assert workspace.contains("../outside.txt") is False


def test_symlink_escape_rejected(workspace: Workspace, repo: Path, tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret\n", encoding="utf-8")
    os.symlink(str(outside_dir), str(repo / "link"))

    # Lexically inside the repo, but the real target escapes -> rejected (§36).
    with pytest.raises(PathOutsideWorkspaceError):
        workspace.resolve("link/secret.txt")
    assert workspace.contains("link/secret.txt") is False


def test_internal_symlink_allowed(workspace: Workspace, repo: Path) -> None:
    (repo / "real.txt").write_text("hi\n", encoding="utf-8")
    os.symlink(str(repo / "real.txt"), str(repo / "alias.txt"))
    # Target stays inside the workspace -> allowed.
    assert workspace.contains("alias.txt") is True
