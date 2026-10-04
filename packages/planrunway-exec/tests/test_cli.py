from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


EXEC = Path(__file__).parents[1] / "src"
CORE = Path(__file__).parents[2] / "planrunway" / "src"
if not CORE.is_dir():
    CORE = Path(__file__).parents[3] / "src"  # exported public layout


def command(root: Path, module: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *args, "--root", str(root)],
        cwd=root, env={**os.environ, "PYTHONPATH": os.pathsep.join((str(EXEC), str(CORE)))},
        text=True, capture_output=True, check=False,
    )


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.com")
    assert command(root, "planrunway.cli", "init").returncode == 0
    assert command(root, "planrunway.cli", "milestone-create", "--id", "MS1", "--title", "Demo").returncode == 0
    assert command(root, "planrunway.cli", "task-create", "--milestone", "MS1", "--id", "T1", "--title", "Update file").returncode == 0
    assert command(root, "planrunway.cli", "task-set-state", "--milestone", "MS1", "--id", "T1", "--state", "in_progress").returncode == 0
    milestone = root / ".prway" / "vision" / "milestones" / "MS1.md"
    milestone.parent.mkdir()
    milestone.write_text("# Goal\n\nUpdate allowed.txt.\n", encoding="utf-8")
    brief = root / ".prway" / "execution" / "tasks" / "MS1" / "T1.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# Task\n\nChange allowed.txt.\n\n## Acceptance Criteria\n\n- allowed.txt contains changed\n", encoding="utf-8")
    config = root / ".prway" / "config.json"
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["tier_id"] = "controlled-exec"
    config.write_text(json.dumps(settings), encoding="utf-8")
    (root / "allowed.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    agent = tmp_path / "opencode"
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "prompt = sys.argv[-1]\n"
        "if prompt.startswith('Implement task below'):\n"
        "    pathlib.Path('allowed.txt').write_text('changed\\n')\n"
        "if prompt.startswith('Propose a concise'):\n"
        "    print(json.dumps({'type': 'text', 'part': {'text': 'Check allowed.txt after update'}}))\n"
        "if prompt.startswith('Read-only review'):\n"
        "    print(json.dumps({'type': 'text', 'part': {'text': 'APPROVED'}}))\n"
        "print(json.dumps({'type': 'step_finish'}))\n",
        encoding="utf-8",
    )
    agent.chmod(0o755)
    wrapper = tmp_path / "reviewer"
    wrapper.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return root, agent, wrapper


def test_operator_lifecycle_from_cli_to_unstaged_integration(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    inspected = command(root, "planrunway_exec.cli", "inspect", "--task", "MS1-T1")
    assert inspected.returncode == 0
    revision = inspected.stdout.split("revision: ", 1)[1].splitlines()[0]
    missing_model = command(root, "planrunway_exec.cli", "start", "--mode", "build", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent))
    assert missing_model.returncode == 1
    assert "explicit --model" in missing_model.stderr
    assert not list((root / ".prway" / "execution" / "runtime" / "state").glob("*.json"))
    denied = command(root, "planrunway_exec.cli", "start", "--mode", "plan", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent), "--model", "test/mock")
    assert denied.returncode == 1
    assert not list((root / ".prway" / "execution" / "runtime" / "state").glob("*.json"))
    started = command(root, "planrunway_exec.cli", "start", "--mode", "build", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--fast-check", "quick=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent), "--model", "test/mock")
    assert started.returncode == 0, started.stderr
    assert started.stdout.index("Creating isolated run") < started.stdout.index("Created isolated run")
    run_id = started.stdout.split("Created isolated run ", 1)[1].split(";", 1)[0]
    premature = command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build")
    assert premature.returncode == 1
    assert "Starting implementer" not in premature.stdout
    proposed = command(root, "planrunway_exec.cli", "propose", "--run-id", run_id, "--mode", "build")
    assert proposed.returncode == 0, proposed.stderr
    assert proposed.stdout.index("Starting proposal agent") < proposed.stdout.index("Proposed tests")
    proposal_id = re.search(r"Proposed tests \[([0-9a-f]{16})\]", proposed.stdout).group(1)
    assert f"approve --proposal-id {proposal_id}" in command(root, "planrunway_exec.cli", "status", "--run-id", run_id).stdout
    assert command(root, "planrunway_exec.cli", "approve", "--run-id", run_id, "--mode", "build", "--proposal-id", proposal_id).returncode == 1
    approved = command(root, "planrunway_exec.cli", "approve", "--run-id", run_id, "--mode", "build", "--proposal-id", proposal_id, "--yes")
    assert approved.returncode == 0, approved.stderr
    implemented = command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build")
    assert implemented.returncode == 0
    assert implemented.stdout.index("Starting implementer") < implemented.stdout.index("Running 1 fast check(s)") < implemented.stdout.index("Implementation finished")
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "base\n"
    assert command(root, "planrunway_exec.cli", "claim", "--run-id", run_id, "--mode", "build").returncode == 1
    reviewed = command(root, "planrunway_exec.cli", "review", "--run-id", run_id, "--mode", "build")
    assert reviewed.returncode == 0
    assert reviewed.stdout.index("Starting read-only reviewer") < reviewed.stdout.index("Reviewer passed")
    claim = command(root, "planrunway_exec.cli", "claim", "--run-id", run_id, "--mode", "build")
    assert claim.returncode == 0, claim.stderr
    assert claim.stdout.index("Running 1 required check(s)") < claim.stdout.index("CompletionClaim")
    assert "Criterion PASS: allowed.txt contains changed" in claim.stdout
    assert "Check PASS: full | output SHA-256" in claim.stdout
    state_path = root / ".prway" / "execution" / "runtime" / "state" / f"{run_id}.json"
    retained = json.loads(state_path.read_text(encoding="utf-8"))["claim"]
    assert retained["diff_sha256"] in claim.stdout
    original = state_path.read_bytes()
    shown = command(root, "planrunway_exec.cli", "claim-show", "--run-id", run_id)
    assert shown.returncode == 0, shown.stderr
    assert "Criterion PASS: allowed.txt contains changed" in shown.stdout
    assert "Check PASS: full" in shown.stdout
    assert retained["diff_sha256"] in shown.stdout
    assert "proposal_text" not in shown.stdout
    assert state_path.read_bytes() == original
    assert "Next: claim-show --run-id" in command(root, "planrunway_exec.cli", "status", "--run-id", run_id).stdout
    assert command(root, "planrunway_exec.cli", "integrate", "--run-id", run_id, "--mode", "build").returncode == 1
    integrated = command(root, "planrunway_exec.cli", "integrate", "--run-id", run_id, "--mode", "build", "--yes")
    assert integrated.returncode == 0, integrated.stderr
    assert "integrated" in integrated.stdout
    assert "Integrating SDD-approved patch" in integrated.stdout
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "changed\n"
    assert git(root, "diff", "--name-only") == "allowed.txt"
    task_records = list((root / ".prway" / "meta" / "tasks").glob("*.json"))
    assert len(task_records) == 1
    assert json.loads(task_records[0].read_text(encoding="utf-8"))["state"] == "in_progress"
    after_integration = command(root, "planrunway_exec.cli", "claim-show", "--run-id", run_id)
    assert after_integration.returncode == 0, after_integration.stderr
    assert "Worktree absent" in after_integration.stdout
    assert "integrate --mode build --yes" not in after_integration.stdout


def _start(root: Path, agent: Path, wrapper: Path, *, no_git: bool = False) -> str:
    inspected = command(root, "planrunway_exec.cli", "inspect", "--task", "MS1-T1")
    revision = inspected.stdout.split("revision: ", 1)[1].splitlines()[0]
    started = command(root, "planrunway_exec.cli", "start", "--mode", "build", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent), "--model", "test/mock", *(["--allow-no-git"] if no_git else []))
    assert started.returncode == 0, started.stderr
    return started.stdout.split("Created isolated run ", 1)[1].split(";", 1)[0]


def test_proposal_progress_is_visible_before_agent_finishes(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    agent.write_text(agent.read_text(encoding="utf-8").replace("prompt = sys.argv[-1]", "import time\ntime.sleep(1)\nprompt = sys.argv[-1]"), encoding="utf-8")
    run_id = _start(root, agent, wrapper)
    process = subprocess.Popen(
        [sys.executable, "-m", "planrunway_exec.cli", "propose", "--run-id", run_id, "--mode", "build", "--root", str(root)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(EXEC), str(CORE)))},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    first_line = process.stdout.readline()
    assert "Starting proposal agent" in first_line
    assert process.poll() is None
    output, error = process.communicate(timeout=5)
    assert process.returncode == 0, error
    assert "Proposed tests" in output


def test_legacy_run_without_model_can_be_cancelled_but_not_invoke_agents(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    run_id = _start(root, agent, wrapper)
    state_path = root / ".prway" / "execution" / "runtime" / "state" / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    del state["model"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    proposed = command(root, "planrunway_exec.cli", "propose", "--run-id", run_id, "--mode", "build")
    assert proposed.returncode == 1
    assert "Legacy run has no explicit model" in proposed.stderr
    cancelled = command(root, "planrunway_exec.cli", "cancel", "--run-id", run_id, "--mode", "build", "--yes")
    assert cancelled.returncode == 0, cancelled.stderr


def _approve(root: Path, run_id: str) -> None:
    proposed = command(root, "planrunway_exec.cli", "propose", "--run-id", run_id, "--mode", "build")
    assert proposed.returncode == 0, proposed.stderr
    proposal_id = re.search(r"Proposed tests \[([0-9a-f]{16})\]", proposed.stdout).group(1)
    assert command(root, "planrunway_exec.cli", "approve", "--run-id", run_id, "--mode", "build", "--proposal-id", proposal_id, "--yes").returncode == 0


def test_non_git_requires_opt_in_and_retains_backup_after_integration(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    shutil.rmtree(root / ".git")
    inspected = command(root, "planrunway_exec.cli", "inspect", "--task", "MS1-T1")
    revision = inspected.stdout.split("revision: ", 1)[1].splitlines()[0]
    refused = command(root, "planrunway_exec.cli", "start", "--mode", "build", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent), "--model", "test/mock")
    assert refused.returncode == 1
    assert "--allow-no-git" in refused.stderr
    assert not list((root / ".prway" / "execution" / "runtime" / "state").glob("*.json"))
    run_id = _start(root, agent, wrapper, no_git=True)
    _approve(root, run_id)
    assert command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "review", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "claim", "--run-id", run_id, "--mode", "build").returncode == 0
    integrated = command(root, "planrunway_exec.cli", "integrate", "--run-id", run_id, "--mode", "build", "--yes")
    assert integrated.returncode == 0, integrated.stderr
    assert "weaker non-Git isolation" in integrated.stdout
    assert "integrated_non_git_backup_retained" in integrated.stdout
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "changed\n"
    assert (root / ".prway" / "execution" / "runtime" / "local" / "backups" / run_id / "allowed.txt").read_text(encoding="utf-8") == "base\n"


def test_cross_machine_restart_requires_discard_and_retries_from_base(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    run_id = _start(root, agent, wrapper)
    _approve(root, run_id)
    worktree = root / ".prway" / "execution" / "runtime" / "local" / "worktrees" / run_id
    (worktree / "allowed.txt").write_text("unsynchronized\n", encoding="utf-8")
    refused = command(root, "planrunway_exec.cli", "resume", "--run-id", run_id, "--mode", "build", "--machine-id", "other-host")
    assert refused.returncode == 1
    assert "--discard-local --yes" in refused.stderr
    assert (worktree / "allowed.txt").read_text(encoding="utf-8") == "unsynchronized\n"
    resumed = command(root, "planrunway_exec.cli", "resume", "--run-id", run_id, "--mode", "build", "--machine-id", "other-host", "--discard-local", "--yes")
    assert resumed.returncode == 0, resumed.stderr
    assert "retries: 1/2" in resumed.stdout
    assert (worktree / "allowed.txt").read_text(encoding="utf-8") == "base\n"
    cancelled = command(root, "planrunway_exec.cli", "cancel", "--run-id", run_id, "--mode", "build", "--yes")
    assert cancelled.returncode == 0, cancelled.stderr
    assert not worktree.exists()


def test_non_git_main_drift_blocks_integration_without_overwriting_backup(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    shutil.rmtree(root / ".git")
    run_id = _start(root, agent, wrapper, no_git=True)
    _approve(root, run_id)
    assert command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "review", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "claim", "--run-id", run_id, "--mode", "build").returncode == 0
    (root / "allowed.txt").write_text("main drift\n", encoding="utf-8")
    result = command(root, "planrunway_exec.cli", "integrate", "--run-id", run_id, "--mode", "build", "--yes")
    assert result.returncode == 1
    assert "INTEGRATION_BLOCKED_MAIN_DRIFT" in result.stderr
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "main drift\n"
    assert (root / ".prway" / "execution" / "runtime" / "local" / "backups" / run_id / "allowed.txt").read_text(encoding="utf-8") == "base\n"


def test_changed_task_revision_invalidates_run_and_cleans_worktree(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    run_id = _start(root, agent, wrapper)
    _approve(root, run_id)
    brief = root / ".prway" / "execution" / "tasks" / "MS1" / "T1.md"
    brief.write_text(brief.read_text(encoding="utf-8") + "\nNew criterion.\n", encoding="utf-8")
    resumed = command(root, "planrunway_exec.cli", "resume", "--run-id", run_id, "--mode", "build")
    assert resumed.returncode == 0, resumed.stderr
    assert "needs_replan" in resumed.stdout
    assert not (root / ".prway" / "execution" / "runtime" / "local" / "worktrees" / run_id).exists()
    assert "needs_replan" in command(root, "planrunway_exec.cli", "status", "--run-id", run_id).stdout


def test_reviewer_findings_retry_without_claim(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    agent.write_text(agent.read_text(encoding="utf-8").replace("APPROVED", "CHANGES_REQUESTED: check edge case"), encoding="utf-8")
    run_id = _start(root, agent, wrapper)
    _approve(root, run_id)
    assert command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build").returncode == 0
    reviewed = command(root, "planrunway_exec.cli", "review", "--run-id", run_id, "--mode", "build")
    assert reviewed.returncode == 0, reviewed.stderr
    assert "check edge case" in reviewed.stdout
    assert "implementing" in reviewed.stdout
    assert command(root, "planrunway_exec.cli", "claim", "--run-id", run_id, "--mode", "build").returncode == 1
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "base\n"


def test_configured_quota_suspends_without_retry_and_resumes_explicitly(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    original_agent = agent.read_text(encoding="utf-8")
    agent.write_text(agent.read_text(encoding="utf-8").replace("    pathlib.Path('allowed.txt').write_text('changed\\n')", "    print('SUBSCRIPTION_LIMIT', file=sys.stderr); sys.exit(1)"), encoding="utf-8")
    inspected = command(root, "planrunway_exec.cli", "inspect", "--task", "MS1-T1")
    revision = inspected.stdout.split("revision: ", 1)[1].splitlines()[0]
    started = command(root, "planrunway_exec.cli", "start", "--mode", "build", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent), "--model", "test/mock", "--quota-signal", "SUBSCRIPTION_LIMIT")
    assert started.returncode == 0, started.stderr
    run_id = started.stdout.split("Created isolated run ", 1)[1].split(";", 1)[0]
    _approve(root, run_id)
    result = command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build")
    assert result.returncode == 0, result.stderr
    assert "suspended_quota" in result.stdout
    record = json.loads((root / ".prway" / "execution" / "runtime" / "state" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert record["retries_consumed"] == 0
    assert command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build").returncode == 1
    resumed = command(root, "planrunway_exec.cli", "resume", "--run-id", run_id, "--mode", "build")
    assert resumed.returncode == 0, resumed.stderr
    assert "retries: 0/2" in resumed.stdout
    agent.write_text(original_agent, encoding="utf-8")
    assert command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build").returncode == 0
    record = json.loads((root / ".prway" / "execution" / "runtime" / "state" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert record["state"] == "reviewing" and record["retries_consumed"] == 0


def test_git_main_drift_blocks_sdd_integration_without_writes(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    run_id = _start(root, agent, wrapper)
    _approve(root, run_id)
    assert command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "review", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "claim", "--run-id", run_id, "--mode", "build").returncode == 0
    (root / "allowed.txt").write_text("operator work\n", encoding="utf-8")
    blocked = command(root, "planrunway_exec.cli", "integrate", "--run-id", run_id, "--mode", "build", "--yes")
    assert blocked.returncode == 1
    assert "INTEGRATION_BLOCKED_MAIN_DRIFT" in blocked.stderr
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "operator work\n"


def test_changed_isolated_patch_after_claim_cannot_integrate(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    run_id = _start(root, agent, wrapper)
    _approve(root, run_id)
    assert command(root, "planrunway_exec.cli", "implement", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "review", "--run-id", run_id, "--mode", "build").returncode == 0
    assert command(root, "planrunway_exec.cli", "claim", "--run-id", run_id, "--mode", "build").returncode == 0
    isolated = root / ".prway" / "execution" / "runtime" / "local" / "worktrees" / run_id / "allowed.txt"
    isolated.write_text("changed after claim\n", encoding="utf-8")
    denied = command(root, "planrunway_exec.cli", "integrate", "--run-id", run_id, "--mode", "build", "--yes")
    assert denied.returncode == 1
    assert "patch changed since CompletionClaim" in denied.stderr
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "base\n"


def test_proposal_timeout_blocks_before_approval_and_leaves_main_untouched(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    agent.write_text(agent.read_text(encoding="utf-8").replace("prompt = sys.argv[-1]", "import time\ntime.sleep(0.2)\nprompt = sys.argv[-1]"), encoding="utf-8")
    inspected = command(root, "planrunway_exec.cli", "inspect", "--task", "MS1-T1")
    revision = inspected.stdout.split("revision: ", 1)[1].splitlines()[0]
    started = command(root, "planrunway_exec.cli", "start", "--mode", "build", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent), "--model", "test/mock", "--timeout", "0.05")
    assert started.returncode == 0, started.stderr
    run_id = started.stdout.split("Created isolated run ", 1)[1].split(";", 1)[0]
    timed_out = command(root, "planrunway_exec.cli", "propose", "--run-id", run_id, "--mode", "build")
    assert timed_out.returncode == 1
    assert "Proposal failed" in timed_out.stderr
    assert "blocked" in command(root, "planrunway_exec.cli", "status", "--run-id", run_id).stdout
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "base\n"


def test_non_git_symlink_refusal_leaves_no_run_or_backup(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    shutil.rmtree(root / ".git")
    (root / "foreign").symlink_to(agent)
    inspected = command(root, "planrunway_exec.cli", "inspect", "--task", "MS1-T1")
    revision = inspected.stdout.split("revision: ", 1)[1].splitlines()[0]
    refused = command(root, "planrunway_exec.cli", "start", "--mode", "build", "--task", "MS1-T1", "--revision", revision, "--allow", "allowed.txt", "--check", "full=true", "--reviewer-wrapper", str(wrapper), "--opencode", str(agent), "--model", "test/mock", "--allow-no-git")
    assert refused.returncode == 1
    assert "refuses symlink" in refused.stderr
    assert not list((root / ".prway" / "execution" / "runtime" / "state").glob("*.json"))
    assert not list((root / ".prway" / "execution" / "runtime" / "local" / "backups").glob("*"))


def test_interrupted_testing_resumes_fast_checks_without_reinvoking_agent(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    run_id = _start(root, agent, wrapper)
    _approve(root, run_id)
    sys.path.insert(0, str(EXEC))
    from planrunway_exec.state import transition

    transition(root, run_id, "testing", "build")
    resumed = command(root, "planrunway_exec.cli", "resume", "--run-id", run_id, "--mode", "build")
    assert resumed.returncode == 0, resumed.stderr
    assert "fast checks resumed" in resumed.stdout
    assert "reviewing" in resumed.stdout
    record = json.loads((root / ".prway" / "execution" / "runtime" / "state" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert record["checkpoint"].startswith("fast_checks:")


def test_superseded_task_invalidates_run_and_removes_obsolete_worktree(tmp_path: Path) -> None:
    root, agent, wrapper = fixture(tmp_path)
    run_id = _start(root, agent, wrapper)
    assert command(root, "planrunway.cli", "task-archive", "--milestone", "MS1", "--id", "T1").returncode == 0
    invalidated = command(root, "planrunway_exec.cli", "resume", "--run-id", run_id, "--mode", "build")
    assert invalidated.returncode == 0, invalidated.stderr
    assert "needs_replan" in invalidated.stdout
    assert not (root / ".prway" / "execution" / "runtime" / "local" / "worktrees" / run_id).exists()
