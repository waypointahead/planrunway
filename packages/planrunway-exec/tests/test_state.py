from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PACKAGE))

from planrunway_exec.state import accept_tests, checkpoint, create_run, propose_tests, record_attempt, resume, resume_quota, transition


def request(run_id: str = "run-1", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "run_id": run_id,
        "task_id": "MS4-T01a",
        "task_revision": "revision-1",
        "machine_id": "host-a",
        "allowlist": ["packages/planrunway-exec/**"],
        "repository": {"kind": "git", "base_revision": "abc123"},
    }
    value.update(overrides)
    return value


def start(root: Path) -> None:
    create_run(root, request(), "build")
    propose_tests(root, "run-1", "proposal-1", "build")
    accept_tests(root, "run-1", "proposal-1", "revision-1", "build")


def test_build_mode_and_single_active_run_are_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Build mode"):
        create_run(tmp_path, request(), "plan")
    create_run(tmp_path, request(), "build")
    with pytest.raises(ValueError, match="already active"):
        create_run(tmp_path, request("run-2"), "build")


def test_implementation_requires_retained_approval_and_runs_to_completion(tmp_path: Path) -> None:
    create_run(tmp_path, request(), "build")
    with pytest.raises(ValueError, match="Invalid execution state transition"):
        transition(tmp_path, "run-1", "testing", "build")
    propose_tests(tmp_path, "run-1", "proposal-1", "build")
    record = accept_tests(tmp_path, "run-1", "proposal-1", "revision-1", "build")
    assert record["state"] == "implementing"
    checkpoint(tmp_path, "run-1", "worktree:checkpoint-1", "build")
    assert transition(tmp_path, "run-1", "testing", "build")["state"] == "testing"
    assert transition(tmp_path, "run-1", "reviewing", "build")["state"] == "reviewing"
    assert transition(tmp_path, "run-1", "completed", "build")["state"] == "completed"


def test_quota_suspension_preserves_retry_and_same_machine_checkpoint(tmp_path: Path) -> None:
    start(tmp_path)
    checkpoint(tmp_path, "run-1", "worktree:checkpoint-1", "build")
    suspended = record_attempt(tmp_path, "run-1", "quota", "quota:provider", "subscription limit", "build")
    assert suspended["state"] == "suspended_quota"
    assert suspended["retries_consumed"] == 0
    resumed = resume_quota(tmp_path, "run-1", "revision-1", "host-a", "build")
    assert resumed["state"] == "implementing"
    assert resumed["checkpoint"] == "worktree:checkpoint-1"
    assert resumed["retries_consumed"] == 0


def test_local_resume_preserves_safe_checkpoint(tmp_path: Path) -> None:
    start(tmp_path)
    checkpoint(tmp_path, "run-1", "worktree:checkpoint-1", "build")
    transition(tmp_path, "run-1", "testing", "build")

    resumed = resume(tmp_path, "run-1", "revision-1", "host-a", "build")

    assert resumed["state"] == "testing"
    assert resumed["checkpoint"] == "worktree:checkpoint-1"
    assert resumed["retries_consumed"] == 0


def test_cross_machine_resume_consumes_retry_and_revision_change_needs_replan(tmp_path: Path) -> None:
    start(tmp_path)
    record_attempt(tmp_path, "run-1", "quota", "quota:provider", "subscription limit", "build")
    restarted = resume_quota(tmp_path, "run-1", "revision-1", "host-b", "build")
    assert restarted["state"] == "implementing"
    assert restarted["retries_consumed"] == 1
    assert "checkpoint" not in restarted
    record_attempt(tmp_path, "run-1", "quota", "quota:provider", "subscription limit", "build")
    changed = resume_quota(tmp_path, "run-1", "revision-2", "host-b", "build")
    assert changed["state"] == "needs_replan"


def test_repeated_equivalent_failure_blocks_without_extra_retry(tmp_path: Path) -> None:
    start(tmp_path)
    first = record_attempt(tmp_path, "run-1", "retryable_failure", "patch:one/checks:one", "test failed", "build")
    assert first["state"] == "implementing"
    assert first["retries_consumed"] == 1
    blocked = record_attempt(tmp_path, "run-1", "retryable_failure", "patch:one/checks:one", "test failed", "build")
    assert blocked["state"] == "blocked"
    assert blocked["retries_consumed"] == 1


def test_non_git_execution_requires_explicit_backup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="backup_ref"):
        create_run(tmp_path, request(repository={"kind": "non_git"}), "build")
    record = create_run(tmp_path, request(repository={"kind": "non_git", "backup_ref": "backup-1"}), "build")
    assert record["repository"] == {"kind": "non_git", "backup_ref": "backup-1"}
