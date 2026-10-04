from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PACKAGE = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PACKAGE))

from planrunway_exec.provenance import commit_trailers, record_provenance
from planrunway_exec.state import accept_tests, create_run, propose_tests, record_attempt
from planrunway_exec.worktree import create_worktree


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test User")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "file.txt")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    create_run(root, {"run_id": "run-1", "task_id": "MS4-T04a", "task_revision": "r1", "machine_id": "host", "allowlist": ["file.txt"], "repository": {"kind": "git", "base_revision": base}}, "build")
    return root, create_worktree(root, "run-1", "build")


def test_provenance_contains_bounded_git_context_and_trailers(tmp_path: Path) -> None:
    root, worktree = fixture(tmp_path)
    (worktree / "file.txt").write_text("changed\n", encoding="utf-8")

    contribution = record_provenance(root, "run-1", worktree)

    assert contribution.status == "available"
    assert contribution.context is not None
    assert contribution.context["changed_files"] == ["file.txt"]
    assert commit_trailers(root, "run-1") == "PlanRunway-Task: MS4-T04a\nPlanRunway-Milestone: MS4\nPlanRunway-Run: run-1\n"
    stored = json.loads((root / ".prway" / "execution" / "runs" / "run-1" / "provenance.json").read_text(encoding="utf-8"))
    assert "prompt" not in stored and "transcript" not in stored


def test_suspended_or_unavailable_provenance_is_fail_open(tmp_path: Path) -> None:
    root, worktree = fixture(tmp_path)
    propose_tests(root, "run-1", "proposal-1", "build")
    accept_tests(root, "run-1", "proposal-1", "r1", "build")
    record_attempt(root, "run-1", "quota", "quota", "limit", "build")
    assert record_provenance(root, "run-1", worktree).status == "suspended"
