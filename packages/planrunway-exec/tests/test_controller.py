from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PACKAGE))

from planrunway_exec.controller import Check, SingleAgentController
from planrunway_exec.opencode import OpenCodeAdapter
from planrunway_exec.state import create_run, run_record
from planrunway_exec.worktree import create_worktree


class Process:
    pid = 123
    returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return '{"type":"step_finish"}\n', ""


def process_factory(*args: object, **kwargs: object) -> Process:
    return Process()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


def root_with_base(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test User")
    (root / "allowed.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "allowed.txt")
    git(root, "commit", "-m", "base")
    return root, git(root, "rev-parse", "HEAD")


def controller(tmp_path: Path) -> tuple[SingleAgentController, Path]:
    root, base = root_with_base(tmp_path)
    create_run(root, {
        "run_id": "run-1",
        "task_id": "MS4-T02a",
        "task_revision": "revision-1",
        "machine_id": "host-a",
        "allowlist": ["allowed.txt"],
        "repository": {"kind": "git", "base_revision": base},
    }, "build")
    worktree = create_worktree(root, "run-1", "build")
    adapter = OpenCodeAdapter(reviewer_prefix=("sandbox",), popen_factory=process_factory)
    return SingleAgentController(root, "run-1", worktree, adapter), root


def approve(controller: SingleAgentController) -> None:
    assert controller.propose("propose", 1, "build").outcome == "completed"
    controller.retain_proposal("proposal-1", "build")
    controller.accept_proposal("proposal-1", "revision-1", "build")


def test_controller_requires_approval_runs_checks_and_returns_claim(tmp_path: Path) -> None:
    flow, root = controller(tmp_path)
    with pytest.raises(ValueError, match="not expected"):
        flow.implement("implement", 1, "build")

    approve(flow)
    assert flow.implement("implement", 1, "build").outcome == "completed"
    (flow.worktree / "allowed.txt").write_text("changed\n", encoding="utf-8")
    assert flow.fast_checks((Check("fast", ("true",), fast=True),), "build")[0].passed
    assert flow.review("review", 1, "build").outcome == "completed"
    claim = flow.completion_claim(("criterion",), (Check("full", ("true",)),), "build")

    assert claim.accepted_proposal_id == "proposal-1"
    assert claim.criterion_results == (("criterion", True),)
    assert claim.changed_paths == ("allowed.txt",)
    assert run_record(root, "run-1")["state"] == "completed"


def test_failed_fast_check_retries_before_reviewer(tmp_path: Path) -> None:
    flow, root = controller(tmp_path)
    approve(flow)
    flow.implement("implement", 1, "build")

    result = flow.fast_checks((Check("fast", ("false",), fast=True),), "build")

    assert not result[0].passed
    assert run_record(root, "run-1")["state"] == "implementing"


def test_completion_claim_rejects_diff_outside_allowlist(tmp_path: Path) -> None:
    flow, root = controller(tmp_path)
    approve(flow)
    flow.implement("implement", 1, "build")
    flow.fast_checks((), "build")
    flow.review("review", 1, "build")
    (flow.worktree / "outside.txt").write_text("no\n", encoding="utf-8")

    with pytest.raises(ValueError, match="allowlist"):
        flow.completion_claim(("criterion",), (), "build")
    assert run_record(root, "run-1")["state"] == "reviewing"
