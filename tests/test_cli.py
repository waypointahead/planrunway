from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path


PACKAGE = Path(__file__).parents[1] / "src"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "planrunway.cli", *args],
        env={"PYTHONPATH": str(PACKAGE)}, text=True, capture_output=True, check=False,
    )


def test_fresh_init_uses_only_prway(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert (tmp_path / ".prway" / "compatibility.json").is_file()
    assert not (tmp_path / ("." + "plan")).exists()
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0
    assert "No milestones" in run(tmp_path, "status", "--root", str(tmp_path)).stdout


def test_fresh_init_installs_agent_bootstrap_contract(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    ground_rules = (tmp_path / ".prway" / "system" / "ground_rules.md").read_text(encoding="utf-8")
    assert "Read `.prway/system/ground_rules.md`" in agents
    assert "Bootstrap project context." in ground_rules
    assert "Product vision" in ground_rules
    assert "Technical preferences" in ground_rules
    assert "Testing approach" in ground_rules
    assert "planrunway milestone-create" in ground_rules
    assert (tmp_path / ".prway" / "user_instructions.md").is_file()


def test_existing_legacy_managed_agents_block_remains_usable(tmp_path: Path) -> None:
    legacy = "<!-- planrunway:begin -->\nRead `.prway/system/bin/planrunway` before working in this repository.\n<!-- planrunway:end -->\n"
    (tmp_path / "AGENTS.md").write_text(legacy, encoding="utf-8")
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 1
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == legacy


def test_init_is_fresh_only(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0


def test_migrate_requires_explicit_confirmation(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "migrate", "--root", str(tmp_path)).returncode == 2
    assert run(tmp_path, "migrate", "--root", str(tmp_path), "--yes").returncode == 0


def test_migrate_converts_legacy_shared_registries_with_rollback_sources(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    state = tmp_path / ".prway"
    (state / "meta" / "plan.json").write_text(json.dumps({
        "schema_version": "0.3.0",
        "milestones": [{"model_id": "m1", "display_id": "MS1", "title": "Milestone", "state": "pending", "order_key": "a"}],
        "tasks": [{"model_id": "t1", "milestone_id": "m1", "display_id": "T1", "title": "Task", "state": "pending", "order_key": "a"}],
    }), encoding="utf-8")
    (state / "execution" / "tasks.json").write_text(json.dumps({"format_version": 1, "tasks": {"T1": {"state": "pending", "criteria": ["c"], "checks": ["check"], "requirement_revision": "r", "final_run": None}}}), encoding="utf-8")
    assert run(tmp_path, "migrate", "--root", str(tmp_path), "--yes").returncode == 0
    assert (state / "meta" / "plan.0.3.0.json").is_file()
    assert (state / "execution" / "tasks.legacy.json").is_file()
    assert (state / "meta" / "milestones" / "m1.json").is_file()
    assert (state / "meta" / "tasks" / "t1.json").is_file()
    assert (state / "execution" / "tasks" / "T1.json").is_file()
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0


def test_embedded_runtime_is_read_only_and_usable(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    command = tmp_path / ".prway" / "system" / "bin" / "planrunway"
    result = subprocess.run([str(command), "status", "--root", str(tmp_path)], text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert "No milestones" in result.stdout


def test_embedded_runtime_reports_conflict_markers(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    record = next((tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    record.write_bytes(b"<<<<<<< current\n" + record.read_bytes() + b"=======\n{}\n>>>>>>> incoming\n")
    command = tmp_path / ".prway" / "system" / "bin" / "planrunway"
    result = subprocess.run([str(command), "lint", "--root", str(tmp_path)], text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert "HUMAN_INTERVENTION_REQUIRED" in result.stderr
    assert "conflict-resolve" in result.stderr


def test_newer_repository_refuses_every_global_command_without_writes(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    envelope = tmp_path / ".prway" / "compatibility.json"
    value = json.loads(envelope.read_text(encoding="utf-8"))
    value["minimum_cli_version"] = "0.3.2"
    envelope.write_text(json.dumps(value), encoding="utf-8")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in (tmp_path / ".prway").rglob("*") if path.is_file()}
    for command in (("init",), ("lint",), ("status",), ("migrate", "--yes")):
        result = run(tmp_path, *command, "--root", str(tmp_path))
        assert result.returncode == 1
        assert "0.3.1" in result.stderr
        assert "0.3.2" in result.stderr
        assert "github.com/waypointahead/planrunway/releases" in result.stderr
    after = {path.relative_to(tmp_path): path.read_bytes() for path in (tmp_path / ".prway").rglob("*") if path.is_file()}
    assert after == before


def test_compatibility_envelope_rejects_missing_and_malformed_values(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    envelope = tmp_path / ".prway" / "compatibility.json"
    envelope.unlink()
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 1
    envelope.write_text('{"format_version": 2}', encoding="utf-8")
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 1


def test_init_preserves_foreign_agents_bytes_and_uninstall_restores_them(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    original = b"# Other tool\n\nKeep this byte sequence.\n"
    agents.write_bytes(original)
    (tmp_path / "CLAUDE.md").write_text("foreign\n", encoding="utf-8")
    (tmp_path / ".opencode").mkdir()
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert agents.read_bytes().startswith(original)
    assert b"planrunway:begin" in agents.read_bytes()
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "foreign\n"
    assert (tmp_path / ".opencode").is_dir()
    dry_run = run(tmp_path, "uninstall", "--root", str(tmp_path), "--dry-run")
    assert dry_run.returncode == 0
    assert (tmp_path / ".prway").is_dir()
    assert run(tmp_path, "uninstall", "--root", str(tmp_path), "--yes").returncode == 0
    assert agents.read_bytes() == original
    assert not (tmp_path / ".prway").exists()
    assert len(list((tmp_path / ".prway-backups").iterdir())) == 1


def test_uninstall_removes_exact_generated_agents_file(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "uninstall", "--root", str(tmp_path), "--yes").returncode == 0
    assert not (tmp_path / "AGENTS.md").exists()
    assert run(tmp_path, "uninstall", "--root", str(tmp_path), "--yes").returncode == 0


def test_marker_collision_and_symlink_refuse_without_writes(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    malformed = "<!-- planrunway:begin -->\nforeign\n"
    agents.write_text(malformed, encoding="utf-8")
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 1
    assert agents.read_text(encoding="utf-8") == malformed
    assert not (tmp_path / ".prway").exists()
    agents.unlink()
    target = tmp_path / "foreign-agents.md"
    target.write_text("foreign\n", encoding="utf-8")
    agents.symlink_to(target)
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 1
    assert target.read_text(encoding="utf-8") == "foreign\n"
    assert not (tmp_path / ".prway").exists()


def test_task_evidence_manual_confirmation_flow(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    registered = run(
        tmp_path, "task-register", "--root", str(tmp_path), "--task-id", "T1",
        "--criterion", "criterion", "--check", "pytest", "--requirement-revision", "sha256:one",
    )
    assert registered.returncode == 0
    evidence = {
        "format_version": 1, "run_id": "run-1", "task_id": "T1", "requirement_revision": "sha256:one",
        "recorded_at": "2026-09-27T00:00:00Z", "state": "final",
        "repository": {"kind": "non_git", "file_snapshot_sha256": "digest"},
        "checks": [{"id": "pytest", "result": "pass", "summary": "passed", "evidence_ref": "command:1"}],
        "criteria": [{"id": "criterion", "result": "satisfied", "summary": "covered", "evidence_ref": "check:pytest"}],
        "accepted_test_conclusion": {"proposal_id": "p1", "decision": "accepted", "decided_at": "2026-09-27T00:00:00Z", "reason": "approved", "test_plan": ["pytest"]},
        "git_association": {"state": "pending"}, "environment": {"python": "3.11"}, "artifacts": [],
    }
    input_path = tmp_path / "evidence.json"
    input_path.write_text(json.dumps(evidence), encoding="utf-8")
    completed = run(tmp_path, "task-complete", "--root", str(tmp_path), "--evidence", str(input_path), "--awaiting-manual")
    assert completed.returncode == 0
    rejected = run(tmp_path, "task-confirm", "--root", str(tmp_path), "--task-id", "T1", "--operator", "reviewer", "--notes", "failed manual check", "--reject")
    assert rejected.returncode == 0
    task = json.loads((tmp_path / ".prway" / "execution" / "tasks" / "T1.json").read_text(encoding="utf-8"))
    assert task["state"] == "in_progress"


def test_fresh_init_creates_evidence_skeleton_and_status_report(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    policy = json.loads((tmp_path / ".prway" / "execution" / "evidence_policy.json").read_text(encoding="utf-8"))
    assert policy["command_log_retention"] == "summary_only"
    assert (tmp_path / ".prway" / "execution" / "runs").is_dir()
    assert run(tmp_path, "render", "--root", str(tmp_path)).returncode == 0
    report = (tmp_path / ".prway" / "execution" / "evidence_status.md").read_text(encoding="utf-8")
    assert report == "# Evidence Status\n\nNo task evidence recorded.\n"


def test_status_report_shows_manual_state_and_git_association(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "task-register", "--root", str(tmp_path), "--task-id", "T1", "--criterion", "criterion", "--check", "pytest", "--requirement-revision", "sha256:one").returncode == 0
    evidence = {
        "format_version": 1, "run_id": "run-1", "task_id": "T1", "requirement_revision": "sha256:one",
        "recorded_at": "2026-09-27T00:00:00Z", "state": "final",
        "repository": {"kind": "git", "base_revision": "base", "observed_head": "head", "changed_files": [], "patch_sha256": "digest"},
        "checks": [{"id": "pytest", "result": "pass", "summary": "passed", "evidence_ref": "command:1"}],
        "criteria": [{"id": "criterion", "result": "satisfied", "summary": "covered", "evidence_ref": "check:pytest"}],
        "accepted_test_conclusion": {"proposal_id": "p1", "decision": "accepted", "decided_at": "2026-09-27T00:00:00Z", "reason": "approved", "test_plan": ["pytest"]},
        "git_association": {"state": "inconclusive"}, "environment": {"python": "3.11"}, "artifacts": [],
    }
    input_path = tmp_path / "evidence.json"
    input_path.write_text(json.dumps(evidence), encoding="utf-8")
    assert run(tmp_path, "task-complete", "--root", str(tmp_path), "--evidence", str(input_path), "--awaiting-manual").returncode == 0
    assert run(tmp_path, "render", "--root", str(tmp_path)).returncode == 0
    report = (tmp_path / ".prway" / "execution" / "evidence_status.md").read_text(encoding="utf-8")
    assert "T1: awaiting_manual; final run run-1; Git association: inconclusive; checks pass=1 fail=0 deferred=0 skipped=0" in report


def test_status_report_identifies_legacy_evidence_without_a_run(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    task_path = tmp_path / ".prway" / "execution" / "tasks" / "T0.json"
    task_path.write_text(json.dumps({"format_version": 1, "task_id": "T0", "state": "legacy", "criteria": [], "checks": [], "requirement_revision": "legacy", "final_run": None}), encoding="utf-8")
    assert run(tmp_path, "render", "--root", str(tmp_path)).returncode == 0
    assert "T0: legacy; legacy evidence; no fabricated final run" in (tmp_path / ".prway" / "execution" / "evidence_status.md").read_text(encoding="utf-8")


def test_status_report_identifies_historical_evidence(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    task_path = tmp_path / ".prway" / "execution" / "tasks" / "T0.json"
    task_path.write_text(json.dumps({"format_version": 1, "task_id": "T0", "state": "in_progress", "criteria": [], "checks": [], "requirement_revision": "new", "final_run": None, "historical_run": "old-run"}), encoding="utf-8")
    assert run(tmp_path, "render", "--root", str(tmp_path)).returncode == 0
    assert "T0: in_progress; historical evidence: run old-run" in (tmp_path / ".prway" / "execution" / "evidence_status.md").read_text(encoding="utf-8")


def test_status_uses_native_canonical_planning_metadata(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Task").returncode == 0
    result = run(tmp_path, "status", "--root", str(tmp_path))
    assert result.returncode == 0
    assert "Active milestone: MS1 | in_progress | Milestone" in result.stdout
    assert "Active task: MS1-T1 | pending | Task" in result.stdout
    assert run(tmp_path, "render", "--root", str(tmp_path)).returncode == 0
    assert "## MS1 | Milestone | in_progress" in (tmp_path / ".prway" / "execution" / "execution_status.md").read_text(encoding="utf-8")


def test_native_planning_lifecycle_supports_future_work(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Task").returncode == 0
    assert run(tmp_path, "status", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    assert "Active task: MS1-T1 | in_progress | Task" in run(tmp_path, "status", "--root", str(tmp_path)).stdout


def test_native_glossary_and_review_artifacts(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "glossary-add", "--root", str(tmp_path), "--term", "SDD", "--definition", "Specification-driven development.").returncode == 0
    assert "SDD" in run(tmp_path, "glossary-list", "--root", str(tmp_path)).stdout
    assert run(tmp_path, "glossary-validate", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert run(tmp_path, "prepare-review", "--root", str(tmp_path), "--milestone", "MS1").returncode == 0
    assert (tmp_path / ".prway" / "releases" / "MS1_release_notes.md").is_file()
