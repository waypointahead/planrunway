from __future__ import annotations

import subprocess
import sys
import json
import os
from pathlib import Path

import pytest


PACKAGE = Path(__file__).parents[1] / "src"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "planrunway.cli", *args],
        env={"PYTHONPATH": str(PACKAGE), "PATH": str(root / "bin") + os.pathsep + os.environ.get("PATH", "")}, text=True, capture_output=True, check=False,
    )


def test_fresh_init_uses_only_prway(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert (tmp_path / ".prway" / "compatibility.json").is_file()
    assert not (tmp_path / ("." + "plan")).exists()
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0
    assert "No milestones" in run(tmp_path, "status", "--root", str(tmp_path)).stdout
    assert (tmp_path / ".prway" / "execution" / "runtime" / "state").is_dir()
    assert "execution/runtime/local/" in (tmp_path / ".prway" / ".gitignore").read_text(encoding="utf-8")


def test_help_and_actionable_lifecycle_diagnostics_are_concise(tmp_path: Path) -> None:
    help_output = run(tmp_path, "task-rename", "--help")
    assert help_output.returncode == 0
    assert "Normal workflow:" in help_output.stdout
    assert "task-rename" in help_output.stdout
    assert "--new-id" in help_output.stdout
    assert "never edit product code or Git history" in help_output.stdout

    absent = run(tmp_path, "status", "--root", str(tmp_path))
    assert absent.returncode == 1
    assert f"planrunway init --root {tmp_path}" in absent.stderr

    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    confirmation = run(tmp_path, "task-confirm", "--root", str(tmp_path), "--task-id", "missing")
    assert confirmation.returncode == 1
    assert "--manual-applicability applicable or not_applicable" in confirmation.stderr
    assert "testing_approach.md" in confirmation.stderr


def test_controlled_exec_requires_separate_compatible_install_and_explicit_choice(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    config = tmp_path / ".prway" / "config.json"
    original = config.read_bytes()
    refused = run(tmp_path, "config-reconfigure", "--root", str(tmp_path), "--tier", "controlled-exec", "--yes")
    assert refused.returncode == 1
    assert "install Exec separately" in refused.stderr
    assert config.read_bytes() == original
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "planrunway-exec"
    executable.write_text("#!/bin/sh\nprintf 'planrunway-exec 0.1.0\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    assert run(tmp_path, "config-reconfigure", "--root", str(tmp_path), "--tier", "controlled-exec").returncode == 1
    assert config.read_bytes() == original
    changed = run(tmp_path, "config-reconfigure", "--root", str(tmp_path), "--tier", "controlled-exec", "--yes")
    assert changed.returncode == 0, changed.stderr
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0
    embedded = subprocess.run([str(tmp_path / ".prway" / "system" / "bin" / "planrunway"), "status", "--root", str(tmp_path)], text=True, capture_output=True, check=False)
    assert embedded.returncode == 0, embedded.stderr
    assert run(tmp_path, "migrate", "--root", str(tmp_path), "--yes").returncode == 0
    assert json.loads(config.read_text(encoding="utf-8"))["tier_id"] == "controlled-exec"


def test_read_only_commands_discover_nearest_root_and_stop_at_nested_git_boundary(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    assert run(tmp_path, "lint", "--root", str(nested)).returncode == 0

    nested_git = nested / "vendor"
    nested_git.mkdir()
    subprocess.run(["git", "init"], cwd=nested_git, check=True, capture_output=True)
    result = run(tmp_path, "lint", "--root", str(nested_git))
    assert result.returncode == 1
    assert "state is absent" in result.stderr


def test_absent_and_malformed_state_have_distinct_guidance(tmp_path: Path) -> None:
    absent = run(tmp_path, "lint", "--root", str(tmp_path))
    assert absent.returncode == 1
    assert "planrunway init" in absent.stderr

    state = tmp_path / ".prway"
    state.mkdir()
    (state / "compatibility.json").write_text("{", encoding="utf-8")
    malformed = run(tmp_path, "lint", "--root", str(tmp_path))
    assert malformed.returncode == 1
    assert "Malformed compatibility envelope" in malformed.stderr


def test_task_create_places_before_after_or_appends_and_guides_duplicates(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert "appended" in run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T2", "--title", "Two").stdout
    assert "before T2" in run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "One", "--before", "T2").stdout
    assert "after T1" in run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1b", "--title", "Middle", "--after", "T1").stdout
    records = sorted((tmp_path / ".prway" / "meta" / "tasks").glob("*.json"))
    ordered = [json.loads(path.read_text(encoding="utf-8")) for path in records]
    assert [item["display_id"] for item in sorted(ordered, key=lambda item: item["order_key"])] == ["T1", "T1b", "T2"]
    duplicate = run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Duplicate")
    assert duplicate.returncode == 1
    assert "meta/tasks/" in duplicate.stderr
    assert "task-set-state" in duplicate.stderr


def test_task_create_refuses_exhausted_insertion_gap(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    for task_id in ("T1", "T2"):
        assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", task_id, "--title", task_id).returncode == 0
    records = sorted((tmp_path / ".prway" / "meta" / "tasks").glob("*.json"))
    for order, path in enumerate(records, start=1):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["order_key"] = str(order).zfill(36)
        path.write_text(json.dumps(record), encoding="utf-8")
    exhausted = run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1b", "--title", "Inserted", "--before", "T2")
    assert exhausted.returncode == 1
    assert "order space is exhausted" in exhausted.stderr


def test_milestone_and_task_rename_preserve_model_identity_and_reject_collisions(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "One").returncode == 0
    milestone_path = next((tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    task_path = next((tmp_path / ".prway" / "meta" / "tasks").glob("*.json"))
    milestone_id = json.loads(milestone_path.read_text(encoding="utf-8"))["model_id"]
    task_id = json.loads(task_path.read_text(encoding="utf-8"))["model_id"]

    assert run(tmp_path, "milestone-rename", "--root", str(tmp_path), "--id", "MS1", "--new-id", "MSA").returncode == 0
    assert run(tmp_path, "task-rename", "--root", str(tmp_path), "--milestone", "MSA", "--id", "T1", "--new-id", "TaskA").returncode == 0
    assert json.loads(milestone_path.read_text(encoding="utf-8"))["model_id"] == milestone_id
    assert json.loads(task_path.read_text(encoding="utf-8"))["model_id"] == task_id
    assert "MSA-TaskA" in run(tmp_path, "status", "--root", str(tmp_path)).stdout

    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MSA", "--id", "Old", "--title", "Old").returncode == 0
    assert run(tmp_path, "task-archive", "--root", str(tmp_path), "--milestone", "MSA", "--id", "Old").returncode == 0
    collision = run(tmp_path, "task-rename", "--root", str(tmp_path), "--milestone", "MSA", "--id", "TaskA", "--new-id", "Old")
    assert collision.returncode == 1
    assert "already owns that ID" in collision.stderr

    malformed = run(tmp_path, "task-rename", "--root", str(tmp_path), "--milestone", "MSA", "--id", "TaskA", "--new-id", "bad-id")
    assert malformed.returncode == 1
    assert "letters or digits" in malformed.stderr


def test_rename_is_blocked_by_canonical_conflict_marker(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    record = next((tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    record.write_bytes(b"<<<<<<< current\n" + record.read_bytes() + b"=======\n{}\n>>>>>>> incoming\n")
    blocked = run(tmp_path, "milestone-rename", "--root", str(tmp_path), "--id", "MS1", "--new-id", "MSA")
    assert blocked.returncode == 1
    assert "conflict-resolve" in blocked.stderr


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


def test_runtime_only_migration_rolls_back_partial_embedded_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from planrunway import cli

    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    state = tmp_path / ".prway"
    envelope = state / "compatibility.json"
    previous = json.loads(envelope.read_text(encoding="utf-8"))
    previous["minimum_cli_version"] = "0.3.1"
    envelope.write_text(json.dumps(previous), encoding="utf-8")
    owned = [state / "compatibility.json", state / "system" / "product.json", state / "system" / "bin" / "planrunway", state / "system" / "ground_rules.md", state / ".gitignore"]
    original = {path: path.read_bytes() for path in owned}

    def interrupted(runtime: Path) -> None:
        (runtime / "system" / "product.json").write_text("partial upgrade", encoding="utf-8")
        raise OSError("simulated interruption")

    monkeypatch.setattr(cli, "_install_embedded_runtime", interrupted)
    assert cli.cmd_migrate(tmp_path, True) == 1
    assert {path: path.read_bytes() for path in owned} == original
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0
    monkeypatch.undo()
    assert run(tmp_path, "migrate", "--root", str(tmp_path), "--yes").returncode == 0
    assert json.loads(envelope.read_text(encoding="utf-8"))["minimum_cli_version"] == "0.3.2"


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
    assert (state / "execution" / "runtime" / "state").is_dir()
    assert (state / "execution" / "runtime" / "local").is_dir()
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
    value["minimum_cli_version"] = "0.3.3"
    envelope.write_text(json.dumps(value), encoding="utf-8")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in (tmp_path / ".prway").rglob("*") if path.is_file()}
    for command in (("init",), ("lint",), ("status",), ("migrate", "--yes")):
        result = run(tmp_path, *command, "--root", str(tmp_path))
        assert result.returncode == 1
        assert "0.3.2" in result.stderr
        assert "0.3.3" in result.stderr
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
    missing_applicability = run(tmp_path, "task-confirm", "--root", str(tmp_path), "--task-id", "T1", "--operator", "reviewer", "--notes", "failed manual check", "--reject")
    assert missing_applicability.returncode == 1
    assert "testing_approach.md" in missing_applicability.stderr
    rejected = run(tmp_path, "task-confirm", "--root", str(tmp_path), "--task-id", "T1", "--operator", "reviewer", "--notes", "failed manual check", "--reject", "--manual-applicability", "applicable")
    assert rejected.returncode == 0
    task = json.loads((tmp_path / ".prway" / "execution" / "tasks" / "T1.json").read_text(encoding="utf-8"))
    assert task["state"] == "in_progress"


def test_fresh_init_creates_evidence_skeleton_and_status_report(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    policy = json.loads((tmp_path / ".prway" / "execution" / "evidence_policy.json").read_text(encoding="utf-8"))
    assert policy["command_log_retention"] == "summary_only"
    assert (tmp_path / ".prway" / "execution" / "runs").is_dir()
    testing_approach = (tmp_path / ".prway" / "technical" / "testing_approach.md").read_text(encoding="utf-8")
    assert "Manual Validation Policy" in testing_approach
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
    lint = run(tmp_path, "lint", "--root", str(tmp_path))
    assert "ACTION_REQUIRED" in lint.stdout
    assert "manual-applicability applicable or not_applicable" in lint.stdout


def test_task_confirm_records_non_applicable_manual_validation(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "task-register", "--root", str(tmp_path), "--task-id", "T1", "--criterion", "criterion", "--check", "pytest", "--requirement-revision", "sha256:one").returncode == 0
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
    assert run(tmp_path, "task-complete", "--root", str(tmp_path), "--evidence", str(input_path), "--awaiting-manual").returncode == 0
    result = run(tmp_path, "task-confirm", "--root", str(tmp_path), "--task-id", "T1", "--operator", "agent", "--notes", "policy says no manual check", "--manual-applicability", "not_applicable")
    assert result.returncode == 0
    assert "not applicable" in result.stdout
    task = json.loads((tmp_path / ".prway" / "execution" / "tasks" / "T1.json").read_text(encoding="utf-8"))
    assert task["state"] == "done"
    event = (tmp_path / ".prway" / "execution" / "events" / "T1.jsonl").read_text(encoding="utf-8")
    assert "not_applicable" in event


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


def test_status_surfaces_task_awaiting_manual_before_milestone_close_hint(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Task").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "awaiting_manual").returncode == 0
    status = run(tmp_path, "status", "--root", str(tmp_path))
    assert "Active task: MS1-T1 | awaiting_manual" in status.stdout
    assert "testing_approach.md" in status.stdout
    assert "milestone-close-check" not in status.stdout


def test_status_selects_first_pending_milestone_after_completed_work(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Completed").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Delivered").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "done").returncode == 0
    brief = tmp_path / ".prway" / "vision" / "milestones" / "MS1.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("# Goal\n\nComplete delivered milestone.\n", encoding="utf-8")
    assert run(tmp_path, "milestone-goal-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--coverage", "complete", "--reason", "Goal reviewed").returncode == 0
    assert run(tmp_path, "milestone-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--manual-applicability", "not_applicable", "--reason", "No manual journey in this milestone").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "ready_to_close").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "done").returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS2", "--title", "Next").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS2", "--id", "T1", "--title", "Task").returncode == 0

    result = run(tmp_path, "status", "--root", str(tmp_path))

    assert result.returncode == 0
    assert "Active milestone: MS2 | pending | Next" in result.stdout
    assert "Active task: MS2-T1 | pending | Task" in result.stdout
    embedded = subprocess.run([str(tmp_path / ".prway" / "system" / "bin" / "planrunway"), "status", "--root", str(tmp_path)], text=True, capture_output=True, check=False)
    assert embedded.returncode == 0, embedded.stderr
    assert "Active milestone: MS2 | pending | Next" in embedded.stdout
    assert "Active task: MS2-T1 | pending | Task" in embedded.stdout


def test_milestone_closure_requires_current_whole_scope_review_and_finished_tasks(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Milestone").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Task").returncode == 0
    record = next((tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    original = record.read_bytes()
    check = run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1")
    assert check.returncode == 1
    assert "whole milestone is represented as applicable" in check.stdout
    assert "Unfinished task: MS1-T1" in check.stdout
    assert record.read_bytes() == original
    refused = run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "ready_to_close")
    assert refused.returncode == 1
    assert "MILESTONE_CLOSURE_BLOCKED" in refused.stderr
    assert record.read_bytes() == original

    incomplete = run(tmp_path, "milestone-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--manual-applicability", "not_applicable", "--reason", "No manual journey")
    assert incomplete.returncode == 1
    assert record.read_bytes() == original
    review_args = ("milestone-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--manual-applicability", "not_applicable", "--reason", "Reviewed full milestone; deterministic checks cover scope")
    assert run(tmp_path, *review_args).returncode == 0
    assert run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").returncode == 1
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "done").returncode == 0
    assert "task scope changed" in run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").stdout
    assert run(tmp_path, *review_args).returncode == 0
    brief = tmp_path / ".prway" / "vision" / "milestones" / "MS1.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("# Goal\n\nTask T1 delivers milestone.\n", encoding="utf-8")
    goal_args = ("milestone-goal-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--coverage", "complete", "--reason", "T1 covers goal")
    assert run(tmp_path, *goal_args).returncode == 0
    assert run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").returncode == 0

    policy = tmp_path / ".prway" / "technical" / "testing_approach.md"
    policy.write_text(policy.read_text(encoding="utf-8") + "\nNew policy.\n", encoding="utf-8")
    stale = run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "ready_to_close")
    assert stale.returncode == 1
    assert "testing_approach.md changed" in stale.stderr
    assert "Missing: closure review" not in stale.stderr
    assert "Next: planrunway milestone-review --id MS1 --help" in stale.stderr
    assert run(tmp_path, *review_args).returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "ready_to_close").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "done").returncode == 0


def test_milestone_review_requires_manual_checks_and_historical_done_is_readable(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Legacy").returncode == 0
    record = next((tmp_path / ".prway" / "meta" / "milestones").glob("*.json"))
    historical = json.loads(record.read_text(encoding="utf-8"))
    historical["state"] = "done"
    record.write_text(json.dumps(historical), encoding="utf-8")
    assert run(tmp_path, "lint", "--root", str(tmp_path)).returncode == 0
    assert "closure_review" not in json.loads(record.read_text(encoding="utf-8"))
    assert run(tmp_path, "milestone-reopen", "--root", str(tmp_path), "--id", "MS1").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Reviewed delivery").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "done").returncode == 0
    missing = run(tmp_path, "milestone-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--manual-applicability", "applicable", "--reason", "Requires operator run")
    assert missing.returncode == 1
    assert "--manual-check" in missing.stderr
    accepted = run(tmp_path, "milestone-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--manual-applicability", "applicable", "--reason", "Reviewed full scope", "--manual-check", "Real CLI journey verified")
    assert accepted.returncode == 0
    brief = tmp_path / ".prway" / "vision" / "milestones" / "MS1.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("# Goal\n\nReviewed delivered scope.\n", encoding="utf-8")
    assert run(tmp_path, "milestone-goal-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--coverage", "complete", "--reason", "Reviewed scope").returncode == 0
    assert run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").returncode == 0


def test_goal_review_fails_fast_on_missing_gap_and_stale_promise(tmp_path: Path) -> None:
    assert run(tmp_path, "init", "--root", str(tmp_path)).returncode == 0
    assert run(tmp_path, "milestone-create", "--root", str(tmp_path), "--id", "MS1", "--title", "Goal").returncode == 0
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "milestone-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--manual-applicability", "not_applicable", "--reason", "Deterministic scope").returncode == 0
    brief = tmp_path / ".prway" / "vision" / "milestones" / "MS1.md"
    missing = run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1")
    assert "MISSING: milestone goal brief" in missing.stdout
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "ready_to_close").returncode == 1
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("# Goal\n\nProvide a runnable feature.\n", encoding="utf-8")
    assert "MISSING: milestone goal completeness review" in run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").stdout
    gap = run(tmp_path, "milestone-goal-review", "--root", str(tmp_path), "--id", "MS1", "--whole-milestone", "--operator", "reviewer", "--coverage", "missing", "--uncovered", "Runnable feature", "--reason", "No task implements it")
    assert gap.returncode == 0
    blocked = run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1")
    assert "MISSING: milestone goal promises: Runnable feature" in blocked.stdout
    assert "task-create" in blocked.stdout
    assert run(tmp_path, "milestone-set-state", "--root", str(tmp_path), "--id", "MS1", "--state", "ready_to_close").returncode == 1
    assert run(tmp_path, "milestone-goal-review", "--root", str(tmp_path), "--id", "MS1", "--whole-milestone", "--operator", "reviewer", "--coverage", "complete", "--reason", "Premature signoff").returncode == 1
    assert run(tmp_path, "task-create", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--title", "Deliver runnable feature").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "done").returncode == 0
    assert run(tmp_path, "milestone-review", "--root", str(tmp_path), "--id", "MS1", "--operator", "reviewer", "--whole-milestone", "--manual-applicability", "not_applicable", "--reason", "Task scope reviewed").returncode == 0
    assert run(tmp_path, "milestone-goal-review", "--root", str(tmp_path), "--id", "MS1", "--whole-milestone", "--operator", "reviewer", "--coverage", "complete", "--reason", "Reviewed implementation against goal").returncode == 0
    assert run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").returncode == 0
    brief.write_text("# Goal\n\nProvide a runnable feature and export.\n", encoding="utf-8")
    assert "goal or task scope changed" in run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").stdout
    brief.write_text("# Goal\n\nProvide a runnable feature.\n", encoding="utf-8")
    assert run(tmp_path, "task-reopen", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    assert run(tmp_path, "task-set-state", "--root", str(tmp_path), "--milestone", "MS1", "--id", "T1", "--state", "done").returncode == 0
    assert "goal or task scope changed" in run(tmp_path, "milestone-close-check", "--root", str(tmp_path), "--id", "MS1").stdout


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
