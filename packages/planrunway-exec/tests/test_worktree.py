from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PACKAGE))

from planrunway_exec.state import create_run, propose_tests
from planrunway_exec.worktree import create_worktree, worktree_path


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}; {result.stdout}"
    return result.stdout.strip()


def request(base_revision: str) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "task_id": "MS4-T02a",
        "task_revision": "revision-1",
        "machine_id": "host-a",
        "allowlist": ["packages/planrunway-exec/**"],
        "repository": {"kind": "git", "base_revision": base_revision},
    }


def initialized_git_root(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test User")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "base")
    return root, git(root, "rev-parse", "HEAD")


def test_create_worktree_uses_recorded_base_and_is_ignored(tmp_path: Path) -> None:
    root, base = initialized_git_root(tmp_path)
    create_run(root, request(base), "build")

    worktree = create_worktree(root, "run-1", "build")

    assert worktree == worktree_path(root, "run-1")
    assert git(worktree, "rev-parse", "HEAD") == base
    assert worktree.is_dir()


def test_worktree_requires_build_mode_active_proposal_run_and_known_base(tmp_path: Path) -> None:
    root, base = initialized_git_root(tmp_path)
    create_run(root, request(base), "build")
    with pytest.raises(ValueError, match="Build mode"):
        create_worktree(root, "run-1", "plan")
    propose_tests(root, "run-1", "proposal-1", "build")
    with pytest.raises(ValueError, match="current execution state"):
        create_worktree(root, "run-1", "build")

    other_root, _ = initialized_git_root(tmp_path / "other")
    create_run(other_root, request("missing"), "build")
    with pytest.raises(ValueError, match="base revision"):
        create_worktree(other_root, "run-1", "build")
