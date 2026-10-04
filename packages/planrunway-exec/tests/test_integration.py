from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PACKAGE))

from planrunway_exec.integration import INTEGRATION_BLOCKED_MAIN_DRIFT, cleanup_worktree, integrate
from planrunway_exec.state import create_run
from planrunway_exec.worktree import create_worktree


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


def fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test User")
    (root / "allowed.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "allowed.txt")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    create_run(root, {
        "run_id": "run-1", "task_id": "MS4-T03a", "task_revision": "revision-1", "machine_id": "host-a",
        "allowlist": ["allowed.txt", "added.txt"], "repository": {"kind": "git", "base_revision": base},
    }, "build")
    return root, create_worktree(root, "run-1", "build"), base


def test_integrate_applies_whole_verified_diff_without_commit(tmp_path: Path) -> None:
    root, worktree, base = fixture(tmp_path)
    (worktree / "allowed.txt").write_text("changed\n", encoding="utf-8")
    (worktree / "added.txt").write_text("new\n", encoding="utf-8")

    result = integrate(root, "run-1", worktree)

    assert result.outcome == "integrated"
    assert result.changed_paths == ("added.txt", "allowed.txt")
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "changed\n"
    assert (root / "added.txt").read_text(encoding="utf-8") == "new\n"
    assert git(root, "rev-parse", "HEAD") == base
    assert not worktree.exists()


def test_integration_blocks_main_drift_and_path_violation(tmp_path: Path) -> None:
    root, worktree, _ = fixture(tmp_path)
    (root / "unrelated.txt").write_text("drift\n", encoding="utf-8")
    assert integrate(root, "run-1", worktree).outcome == INTEGRATION_BLOCKED_MAIN_DRIFT
    assert worktree.exists()

    root2, worktree2, _ = fixture(tmp_path / "path")
    (worktree2 / "forbidden.txt").write_text("no\n", encoding="utf-8")
    blocked = integrate(root2, "run-1", worktree2)
    assert blocked.outcome == INTEGRATION_BLOCKED_MAIN_DRIFT
    assert blocked.changed_paths == ("forbidden.txt",)
    assert worktree2.exists()


def test_cleanup_refuses_foreign_worktree(tmp_path: Path) -> None:
    root, worktree, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="non-controller"):
        cleanup_worktree(root, "run-1", root)
    cleanup_worktree(root, "run-1", worktree)
    assert not worktree.exists()
