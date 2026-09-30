from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

from planrunway import planning
from planrunway import cli

PACKAGE = Path(__file__).parents[1] / "src"


def command(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "planrunway.cli", *args], env={**os.environ, "PYTHONPATH": str(PACKAGE)}, text=True, capture_output=True, check=False)


def git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def commit(root: Path, message: str) -> None:
    """Create fixture commit without depending on developer-machine commit hooks."""
    tree = subprocess.run(["git", "write-tree"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    parent = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, text=True, capture_output=True, check=False)
    command = ["git", "commit-tree", tree]
    if parent.returncode == 0:
        command.extend(("-p", parent.stdout.strip()))
    revision = subprocess.run([*command, "-m", message], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    git(root, "update-ref", "HEAD", revision)


def initialized_git(root: Path) -> None:
    assert command(root, "init", "--root", str(root)).returncode == 0
    git(root, "init")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "PlanRunway test")
    git(root, "add", ".")
    commit(root, "initialize")
    git(root, "branch", "-M", "main")


def test_disjoint_planning_entities_merge_without_conflict(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "PlanRunway test")
    git(tmp_path, "add", ".")
    commit(tmp_path, "initialize")
    git(tmp_path, "branch", "-M", "main")
    git(tmp_path, "switch", "-c", "feature/alpha")
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MSA", "--title", "Alpha").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "add alpha")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/beta")
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MSB", "--title", "Beta").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "add beta")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "--no-ff", "feature/alpha", "-m", "merge alpha")
    git(tmp_path, "merge", "--no-ff", "feature/beta", "-m", "merge beta")
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_same_entity_edits_remain_explicit_git_conflict(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Original").returncode == 0
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "PlanRunway test")
    git(tmp_path, "add", ".")
    commit(tmp_path, "initialize")
    git(tmp_path, "branch", "-M", "main")
    git(tmp_path, "switch", "-c", "feature/one")
    assert command(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "change one")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/two")
    assert command(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "blocked").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "change two")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/one")
    result = subprocess.run(["git", "merge", "feature/two"], cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "CONFLICT" in result.stdout or "CONFLICT" in result.stderr


def test_duplicate_display_ids_merge_but_lint_requires_human_resolution(tmp_path: Path) -> None:
    initialized_git(tmp_path)
    git(tmp_path, "switch", "-c", "feature/one")
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "One").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "add first duplicate")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/two")
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Two").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "add second duplicate")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/one")
    git(tmp_path, "merge", "feature/two")
    result = command(tmp_path, "lint", "--root", str(tmp_path))
    assert result.returncode == 1
    assert "HUMAN_INTERVENTION_REQUIRED" in result.stderr


def test_disjoint_evidence_records_merge_without_conflict(tmp_path: Path) -> None:
    initialized_git(tmp_path)
    git(tmp_path, "switch", "-c", "feature/alpha")
    assert command(tmp_path, "task-register", "--root", str(tmp_path), "--task-id", "alpha", "--criterion", "c", "--check", "check", "--requirement-revision", "r1").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "register alpha evidence")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/beta")
    assert command(tmp_path, "task-register", "--root", str(tmp_path), "--task-id", "beta", "--criterion", "c", "--check", "check", "--requirement-revision", "r1").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "register beta evidence")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/alpha")
    git(tmp_path, "merge", "feature/beta")
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_concurrent_reorders_have_deterministic_tie_breaker(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    for task_id in ("T1", "T2", "T3", "T4"):
        assert command(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", task_id, "--title", task_id).returncode == 0
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "PlanRunway test")
    git(tmp_path, "add", ".")
    commit(tmp_path, "initialize ordered tasks")
    git(tmp_path, "branch", "-M", "main")
    git(tmp_path, "switch", "-c", "feature/left")
    assert command(tmp_path, "task-reorder", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--after", "T2").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "move T1")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/right")
    assert command(tmp_path, "task-reorder", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T4", "--before", "T3").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "move T4")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/left")
    git(tmp_path, "merge", "feature/right")
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0
    first = command(tmp_path, "render", "--root", str(tmp_path))
    second = command(tmp_path, "render", "--root", str(tmp_path))
    assert first.returncode == second.returncode == 0
    status = (tmp_path / ".prway" / "execution" / "execution_status.md").read_text(encoding="utf-8")
    assert status.index("MS1-T2") < status.index("MS1-T3")
    assert status.index("MS1-T1") < status.index("MS1-T3")
    assert status.index("MS1-T4") < status.index("MS1-T3")


def test_rebase_preserves_disjoint_entity_change(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert command(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "One").returncode == 0
    assert command(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T2", "--title", "Two").returncode == 0
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "PlanRunway test")
    git(tmp_path, "add", ".")
    commit(tmp_path, "initialize tasks")
    git(tmp_path, "branch", "-M", "main")
    git(tmp_path, "switch", "-c", "feature/rebase")
    assert command(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T2", "--state", "in_progress").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "change T2")
    git(tmp_path, "switch", "main")
    assert command(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "change T1")
    git(tmp_path, "switch", "feature/rebase")
    git(tmp_path, "rebase", "main")
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_same_evidence_entity_edit_remains_explicit_git_conflict(tmp_path: Path) -> None:
    initialized_git(tmp_path)
    assert command(tmp_path, "task-register", "--root", str(tmp_path), "--task-id", "T1", "--criterion", "c", "--check", "check", "--requirement-revision", "r1").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "register evidence")
    git(tmp_path, "switch", "-c", "feature/one")
    assert command(tmp_path, "task-revise", "--root", str(tmp_path), "--task-id", "T1", "--requirement-revision", "r2").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "revise one")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/two")
    assert command(tmp_path, "task-revise", "--root", str(tmp_path), "--task-id", "T1", "--requirement-revision", "r3").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "revise two")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/one")
    result = subprocess.run(["git", "merge", "feature/two"], cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "CONFLICT" in result.stdout or "CONFLICT" in result.stderr


def test_rendered_views_are_ignored_and_regenerated_from_canonical_state(tmp_path: Path) -> None:
    initialized_git(tmp_path)
    assert command(tmp_path, "render", "--root", str(tmp_path)).returncode == 0
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, text=True, capture_output=True, check=True)
    assert status.stdout == ""
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert command(tmp_path, "render", "--root", str(tmp_path)).returncode == 0
    report = (tmp_path / ".prway" / "execution" / "execution_status.md").read_text(encoding="utf-8")
    assert "## MS1 | Milestone | pending" in report


def test_supersession_and_same_task_update_remain_explicit_git_conflict(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert command(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Original").returncode == 0
    assert command(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T2", "--title", "Successor").returncode == 0
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "PlanRunway test")
    git(tmp_path, "add", ".")
    commit(tmp_path, "initialize tasks")
    git(tmp_path, "branch", "-M", "main")
    git(tmp_path, "switch", "-c", "feature/supersede")
    assert command(tmp_path, "task-supersede", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--successor", "T2").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "supersede T1")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/update")
    assert command(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "update T1")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/supersede")
    result = subprocess.run(["git", "merge", "feature/update"], cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "CONFLICT" in result.stdout or "CONFLICT" in result.stderr


def _final_evidence(task_id: str, run_id: str) -> str:
    return (
        '{"format_version":1,"run_id":"' + run_id + '","task_id":"' + task_id + '",'
        '"requirement_revision":"r1","recorded_at":"2026-09-28T00:00:00Z","state":"final",'
        '"repository":{"kind":"non_git","file_snapshot_sha256":"digest"},'
        '"checks":[{"id":"check","result":"pass","summary":"passed","evidence_ref":"command"}],'
        '"criteria":[{"id":"c","result":"satisfied","summary":"covered","evidence_ref":"check"}],'
        '"accepted_test_conclusion":{"proposal_id":"p","decision":"accepted","decided_at":"2026-09-28T00:00:00Z","reason":"approved","test_plan":["check"]},'
        '"git_association":{"state":"pending"},"environment":{},"artifacts":[]}'
    )


def test_duplicate_final_run_id_is_explicit_git_add_add_conflict(tmp_path: Path) -> None:
    initialized_git(tmp_path)
    for task_id in ("T1", "T2"):
        assert command(tmp_path, "task-register", "--root", str(tmp_path), "--task-id", task_id, "--criterion", "c", "--check", "check", "--requirement-revision", "r1").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "register evidence tasks")
    alpha_evidence = tmp_path / "alpha-evidence.json"
    beta_evidence = tmp_path / "beta-evidence.json"
    alpha_evidence.write_text(_final_evidence("T1", "shared-run"), encoding="utf-8")
    beta_evidence.write_text(_final_evidence("T2", "shared-run"), encoding="utf-8")
    git(tmp_path, "switch", "-c", "feature/alpha")
    assert command(tmp_path, "task-complete", "--root", str(tmp_path), "--evidence", str(alpha_evidence)).returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "complete T1")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/beta")
    assert command(tmp_path, "task-complete", "--root", str(tmp_path), "--evidence", str(beta_evidence)).returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "complete T2")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/alpha")
    result = subprocess.run(["git", "merge", "feature/beta"], cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "CONFLICT" in result.stdout or "CONFLICT" in result.stderr


def test_interrupted_entity_write_leaves_no_partial_canonical_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    state = tmp_path / ".prway"
    monkeypatch.setattr(planning.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError, match="interrupted"):
        planning.create_milestone(state, "MS1", "Interrupted")
    assert not list((state / "meta" / "milestones").glob("*.json"))
    assert not list((state / "meta" / "milestones").glob(".*"))
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_lint_blocks_mutation_and_guides_batch_conflict_resolution(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    for task_id in ("T1", "T2"):
        assert command(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", task_id, "--title", task_id).returncode == 0
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "PlanRunway test")
    git(tmp_path, "add", ".")
    commit(tmp_path, "initialize")
    git(tmp_path, "branch", "-M", "main")
    git(tmp_path, "switch", "-c", "feature/one")
    for task_id in ("T1", "T2"):
        assert command(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", task_id, "--state", "in_progress").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "start tasks")
    git(tmp_path, "switch", "main")
    git(tmp_path, "switch", "-c", "feature/two")
    for task_id in ("T1", "T2"):
        assert command(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", task_id, "--state", "blocked").returncode == 0
    git(tmp_path, "add", ".prway")
    commit(tmp_path, "block tasks")
    git(tmp_path, "switch", "main")
    git(tmp_path, "merge", "feature/one")
    result = subprocess.run(["git", "merge", "feature/two"], cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    unresolved_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True, capture_output=True, check=True).stdout
    lint = command(tmp_path, "lint", "--root", str(tmp_path))
    assert lint.returncode == 1
    assert "HUMAN_INTERVENTION_REQUIRED: 2 blocking PlanRunway conflicts" in lint.stderr
    assert "No planning changes, status, or render can continue" in lint.stderr
    blocked = command(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T3", "--title", "Blocked")
    assert blocked.returncode == 1
    assert "conflict-resolve" in blocked.stderr
    preview = command(tmp_path, "conflict-resolve", "--root", str(tmp_path))
    assert preview.returncode == 1
    assert "Blocking items: 2" in preview.stdout
    assert "Options:" in preview.stdout
    assert "branch checked out when merge started" in preview.stdout
    assert "--all is required" in preview.stdout
    assert command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--strategy", "ours", "--no-stage").returncode == 1
    resolved = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--strategy", "ours", "--no-stage", "--all")
    assert resolved.returncode == 0
    assert "Files left unstaged" in resolved.stdout
    staged = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--strategy", "ours", "--stage", "--all")
    assert staged.returncode == 0
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True, capture_output=True, check=True).stdout == unresolved_head
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_resolver_admits_marker_without_intact_git_side_is_unresolvable(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    path = next((tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    original = path.read_bytes()
    path.write_bytes(b"<<<<<<< manual\n" + original + b"=======\n" + original + b">>>>>>> manual\n")
    lint = command(tmp_path, "lint", "--root", str(tmp_path))
    assert lint.returncode == 1
    assert "HUMAN_INTERVENTION_REQUIRED" in lint.stderr
    resolution = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--strategy", "ours", "--no-stage")
    assert resolution.returncode == 1
    assert "CANNOT_SAFELY_RESOLVE" in resolution.stdout or "CANNOT_SAFELY_RESOLVE" in resolution.stderr
    assert "Restore valid planning entity from a known Git revision" in resolution.stdout
    assert path.read_bytes().startswith(b"<<<<<<< manual")


def test_resolver_admits_duplicate_identity_needs_human_product_decision(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "One").returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS2", "--title", "Two").returncode == 0
    record = next(path for path in (tmp_path / ".prway" / "meta" / "milestones").glob("*.json") if '"display_id":"MS2"' in path.read_text(encoding="utf-8"))
    record.write_text(record.read_text(encoding="utf-8").replace("MS2", "MS1"), encoding="utf-8")
    lint = command(tmp_path, "lint", "--root", str(tmp_path))
    assert lint.returncode == 1
    assert "HUMAN_INTERVENTION_REQUIRED" in lint.stderr
    resolution = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--strategy", "ours", "--no-stage")
    assert resolution.returncode == 1
    assert "duplicate display identity" in resolution.stdout
    assert "Choose which entity keeps display ID" in resolution.stdout


def test_resolver_renames_or_archives_duplicate_identity_after_explicit_choice(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "One").returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS2", "--title", "Two").returncode == 0
    record = next(path for path in (tmp_path / ".prway" / "meta" / "milestones").glob("*.json") if '"display_id":"MS2"' in path.read_text(encoding="utf-8"))
    record.write_text(record.read_text(encoding="utf-8").replace("MS2", "MS1"), encoding="utf-8")
    renamed = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--rename", "MS2-renamed", "--no-stage")
    assert renamed.returncode == 0
    assert "Selected resolution is valid" in renamed.stdout
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_resolver_archives_duplicate_identity_after_explicit_choice(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "One").returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS2", "--title", "Two").returncode == 0
    record = next(path for path in (tmp_path / ".prway" / "meta" / "milestones").glob("*.json") if '"display_id":"MS2"' in path.read_text(encoding="utf-8"))
    record.write_text(record.read_text(encoding="utf-8").replace("MS2", "MS1"), encoding="utf-8")
    archived = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--archive", "--no-stage")
    assert archived.returncode == 0
    assert any(json.loads(path.read_text(encoding="utf-8"))["state"] == "archived" for path in (tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_resolver_reopens_duplicate_final_run_after_explicit_choice(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    for task_id in ("T1", "T2"):
        assert command(tmp_path, "task-register", "--root", str(tmp_path), "--task-id", task_id, "--criterion", "c", "--check", "check", "--requirement-revision", "r1").returncode == 0
    evidence = tmp_path / "evidence.json"
    evidence.write_text(_final_evidence("T1", "run-1"), encoding="utf-8")
    assert command(tmp_path, "task-complete", "--root", str(tmp_path), "--evidence", str(evidence)).returncode == 0
    task_one = tmp_path / ".prway" / "execution" / "tasks" / "T1.json"
    task_two = tmp_path / ".prway" / "execution" / "tasks" / "T2.json"
    duplicate = json.loads(task_one.read_text(encoding="utf-8"))
    duplicate["task_id"] = "T2"
    task_two.write_text(json.dumps(duplicate), encoding="utf-8")
    blocked = command(tmp_path, "lint", "--root", str(tmp_path))
    assert blocked.returncode == 1
    assert "HUMAN_INTERVENTION_REQUIRED" in blocked.stderr
    reopened = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--reopen", "--no-stage")
    assert reopened.returncode == 0
    repaired = json.loads(task_two.read_text(encoding="utf-8"))
    assert repaired["state"] == "in_progress"
    assert repaired["historical_run"] == "run-1"
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_tty_resolver_offers_semantic_choice_and_staging_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "One").returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS2", "--title", "Two").returncode == 0
    record = next(path for path in (tmp_path / ".prway" / "meta" / "milestones").glob("*.json") if '"display_id":"MS2"' in path.read_text(encoding="utf-8"))
    record.write_text(record.read_text(encoding="utf-8").replace("MS2", "MS1"), encoding="utf-8")

    class Terminal:
        def isatty(self) -> bool:
            return True

    answers = iter(("rename", "MS2-resolved", "n"))
    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert cli.cmd_conflict_resolve(tmp_path, None, False, None, None, False, False, None) == 0
    display_ids = [json.loads(path.read_text(encoding="utf-8"))["display_id"] for path in (tmp_path / ".prway" / "meta" / "milestones").glob("*.json")]
    assert "MS2-resolved" in display_ids
    assert display_ids.count("MS1") == 1
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_tty_resolver_cancel_leaves_semantic_conflict_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "One").returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS2", "--title", "Two").returncode == 0
    record = next(path for path in (tmp_path / ".prway" / "meta" / "milestones").glob("*.json") if '"display_id":"MS2"' in path.read_text(encoding="utf-8"))
    record.write_text(record.read_text(encoding="utf-8").replace("MS2", "MS1"), encoding="utf-8")
    before = record.read_bytes()

    class Terminal:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda _: "cancel")
    assert cli.cmd_conflict_resolve(tmp_path, None, False, None, None, False, False, None) == 0
    assert record.read_bytes() == before
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 1


def test_resolver_restores_malformed_record_from_explicit_git_revision(tmp_path: Path) -> None:
    assert command(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert command(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "PlanRunway test")
    git(tmp_path, "add", ".")
    commit(tmp_path, "valid record")
    record = next((tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    record.write_text("{not-json", encoding="utf-8")
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 1
    restored = command(tmp_path, "conflict-resolve", "--root", str(tmp_path), "--restore", "HEAD", "--no-stage")
    assert restored.returncode == 0
    assert json.loads(record.read_text(encoding="utf-8"))["display_id"] == "MS1"
    assert command(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0
