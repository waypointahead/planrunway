from __future__ import annotations

import json

import pytest

from planrunway.evidence import (
    append_event,
    bind_git_association,
    complete_task,
    confirm_task,
    fingerprint_files,
    lint_tasks,
    register_task,
    revise_task,
    store_final_run,
    validate_final_run,
)


def final_run(kind: str = "git") -> dict[str, object]:
    repository: dict[str, object] = (
        {"kind": "git", "base_revision": "base", "observed_head": "head", "changed_files": ["src/a.py"], "patch_sha256": "digest"}
        if kind == "git"
        else {"kind": "non_git", "file_snapshot_sha256": "digest"}
    )
    return {
        "format_version": 1,
        "run_id": "run-1",
        "task_id": "MS3-T02a",
        "requirement_revision": "sha256:digest",
        "recorded_at": "2026-09-27T00:00:00Z",
        "state": "final",
        "repository": repository,
        "checks": [{"id": "pytest", "result": "pass", "summary": "passed", "evidence_ref": "command:1"}],
        "criteria": [{"id": "criterion", "result": "satisfied", "summary": "covered", "evidence_ref": "check:pytest"}],
        "accepted_test_conclusion": {"proposal_id": "proposal-1", "decision": "accepted", "decided_at": "2026-09-27T00:00:00Z", "reason": "approved", "test_plan": ["pytest"]},
        "git_association": {"state": "pending"},
        "environment": {"python": "3.11"},
        "artifacts": [],
    }


@pytest.mark.parametrize("kind", ["git", "non_git"])
def test_final_run_supports_git_and_non_git_records(kind: str) -> None:
    assert validate_final_run(final_run(kind))["repository"]


def test_final_run_storage_is_write_once(tmp_path) -> None:
    record = final_run()
    path = store_final_run(tmp_path / ".prway", record)
    assert json.loads(path.read_text(encoding="utf-8"))["task_id"] == "MS3-T02a"
    with pytest.raises(ValueError, match="already exists"):
        store_final_run(tmp_path / ".prway", record)


def test_events_are_append_only_and_validate_manual_notes(tmp_path) -> None:
    state = tmp_path / ".prway"
    event = {"format_version": 1, "kind": "manual_confirmation", "task_id": "MS3-T02a", "operator": "reviewer", "decision": "confirmed", "notes": "manual check passed"}
    path = append_event(state, "MS3-T02a", event)
    append_event(state, "MS3-T02a", {"format_version": 1, "kind": "attempt", "task_id": "MS3-T02a", "result": "rejected", "reason": "missing check"})
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    with pytest.raises(ValueError, match="decision"):
        append_event(state, "MS3-T02a", {**event, "decision": "maybe"})


def test_snapshot_fingerprint_excludes_planrunway_state(tmp_path) -> None:
    (tmp_path / "source.txt").write_text("content", encoding="utf-8")
    first = fingerprint_files(tmp_path)
    (tmp_path / ".prway").mkdir()
    (tmp_path / ".prway" / "ignored.txt").write_text("state", encoding="utf-8")
    assert fingerprint_files(tmp_path) == first


def test_completion_requires_declared_evidence_and_manual_confirmation(tmp_path) -> None:
    state = tmp_path / ".prway"
    register_task(state, "MS3-T02a", ["criterion"], ["pytest"], "sha256:digest")
    complete_task(state, final_run(), awaiting_manual=True)
    task = json.loads((state / "execution" / "tasks" / "MS3-T02a.json").read_text(encoding="utf-8"))
    assert task["state"] == "awaiting_manual"
    confirm_task(state, "MS3-T02a", "reviewer", "checked", confirmed=False)
    task = json.loads((state / "execution" / "tasks" / "MS3-T02a.json").read_text(encoding="utf-8"))
    assert task["state"] == "in_progress"
    assert "rejected" in (state / "execution" / "events" / "MS3-T02a.jsonl").read_text(encoding="utf-8")


def test_stale_requirement_reopens_done_task(tmp_path) -> None:
    state = tmp_path / ".prway"
    register_task(state, "MS3-T02a", ["criterion"], ["pytest"], "sha256:digest")
    complete_task(state, final_run())
    revise_task(state, "MS3-T02a", "sha256:revised")
    task = json.loads((state / "execution" / "tasks" / "MS3-T02a.json").read_text(encoding="utf-8"))
    assert task["state"] == "in_progress"
    assert task["historical_run"] == "run-1"


def test_later_git_association_does_not_change_completion(tmp_path) -> None:
    state = tmp_path / ".prway"
    register_task(state, "MS3-T02a", ["criterion"], ["pytest"], "sha256:digest")
    complete_task(state, final_run())
    bind_git_association(state, "MS3-T02a", {"state": "range", "from": "base", "to": "head"})
    record = json.loads((state / "execution" / "runs" / "run-1" / "final_run.json").read_text(encoding="utf-8"))
    assert record["git_association"] == {"state": "range", "from": "base", "to": "head"}


def test_lint_rejects_stale_completed_evidence(tmp_path) -> None:
    state = tmp_path / ".prway"
    register_task(state, "MS3-T02a", ["criterion"], ["pytest"], "sha256:digest")
    complete_task(state, final_run())
    revise_task(state, "MS3-T02a", "sha256:revised")
    # Simulate a stale run still claimed as completion by malformed external state.
    task_path = state / "execution" / "tasks" / "MS3-T02a.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["state"] = "done"
    task["final_run"] = "run-1"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        lint_tasks(state)


def test_legacy_task_state_requires_no_fabricated_final_run(tmp_path) -> None:
    state = tmp_path / ".prway"
    register_task(state, "legacy-task", ["criterion"], ["pytest"], "sha256:legacy")
    path = state / "execution" / "tasks" / "legacy-task.json"
    task = json.loads(path.read_text(encoding="utf-8"))
    task["state"] = "legacy"
    path.write_text(json.dumps(task), encoding="utf-8")
    lint_tasks(state)
