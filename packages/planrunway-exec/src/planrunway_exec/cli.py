"""Explicit operator boundary for one isolated, approved Exec run."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .controller import Check, SingleAgentController, changed_paths
from .integration import cleanup_worktree, integrate
from .non_git import backup_and_worktree, backup_path, changes as non_git_changes, integrate as integrate_non_git, snapshot as non_git_snapshot
from .opencode import OpenCodeAdapter
from .state import ACTIVE_STATES, _require_build, accept_tests, block_run, cancel_run, checkpoint, create_run, invocation_allowed, mark_reviewed, propose_tests, record_attempt, resume, retain_claim, run_record
from .worktree import create_worktree, restore_missing_worktree, worktree_path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"Git repository required: {result.stderr.strip() or 'unable to inspect repository'}")
    return result.stdout.strip()


def _progress(message: str) -> None:
    print(f"[planrunway-exec] {message}", flush=True)


def _canonical(state: Path, kind: str) -> list[dict[str, object]]:
    directory = state / "meta" / f"{kind}s"
    records = []
    for path in sorted(directory.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(item, dict) or item.get("kind") != kind or item.get("model_id") != path.stem:
            raise ValueError(f"Malformed canonical {kind}: {path}")
        records.append(item)
    return records


def task_context(root: Path, task_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-[A-Za-z][A-Za-z0-9]*", task_id):
        raise ValueError("Task ID must be MILESTONE-TASK (letters and digits only)")
    state = root / ".prway"
    envelope = json.loads((state / "compatibility.json").read_text(encoding="utf-8"))
    if envelope.get("schema_version") != "0.4.0" or envelope.get("format_version") != 1:
        raise ValueError("Exec 0.1.0 requires compatible Core schema 0.4.0")
    milestone_id, display_id = task_id.split("-", 1)
    milestones = [item for item in _canonical(state, "milestone") if item.get("display_id") == milestone_id and item.get("state") != "archived"]
    if len(milestones) != 1:
        raise ValueError(f"Unknown or ambiguous milestone: {milestone_id}")
    tasks = [item for item in _canonical(state, "task") if item.get("milestone_id") == milestones[0]["model_id"] and item.get("display_id") == display_id and item.get("state") != "archived"]
    if len(tasks) != 1:
        raise ValueError(f"Unknown or ambiguous task: {task_id}")
    brief = state / "execution" / "tasks" / milestone_id / f"{display_id}.md"
    milestone_brief = state / "vision" / "milestones" / f"{milestone_id}.md"
    for path in (brief, milestone_brief):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768 or not path.read_bytes().strip():
            raise ValueError(f"Missing, empty or oversized planning brief: {path}")
    content = brief.read_text(encoding="utf-8")
    criteria = []
    in_criteria = False
    for line in content.splitlines():
        if line.lstrip("# ").lower().startswith("acceptance criteria"):
            in_criteria = True
        elif in_criteria and line.startswith("#"):
            break
        elif in_criteria and line.strip().startswith("- "):
            criteria.append(line.strip()[2:].strip())
    if not criteria:
        raise ValueError(f"Task brief has no acceptance criteria: {brief}")
    return {
        "task_id": task_id, "revision": hashlib.sha256(brief.read_bytes()).hexdigest(),
        "criteria": criteria, "milestone_brief": milestone_brief.read_text(encoding="utf-8"),
        "task_brief": content,
    }


def _require_profile(root: Path) -> None:
    config = json.loads((root / ".prway" / "config.json").read_text(encoding="utf-8"))
    if config.get("tier_id") != "controlled-exec":
        raise ValueError("Exec tier is not enabled. Next: planrunway config-reconfigure --tier controlled-exec --yes")


def _safe_path(pattern: str) -> bool:
    return bool(pattern and not pattern.startswith(("/", ".prway/")) and not any(segment in {".", ".."} for segment in pattern.split("/")) and "\\" not in pattern)


def _checks(values: list[str]) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for item in values:
        if "=" not in item:
            raise ValueError("Check must be NAME=COMMAND; passed without a shell")
        name, command = item.split("=", 1)
        argv = shlex.split(command)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not argv or name in {str(check["name"]) for check in checks}:
            raise ValueError("Checks require unique names and nonempty commands")
        checks.append({"name": name, "command": argv})
    return checks


def _start(root: Path, args: argparse.Namespace) -> str:
    _require_build(args.mode)
    _require_profile(root)
    context = task_context(root, args.task or "")
    milestone_id, display_id = str(context["task_id"]).split("-", 1)
    task = next(item for item in _canonical(root / ".prway", "task") if item.get("display_id") == display_id and item.get("milestone_id") == next(m["model_id"] for m in _canonical(root / ".prway", "milestone") if m.get("display_id") == milestone_id))
    if task.get("state") != "in_progress":
        raise ValueError("Execution requires an in_progress canonical task")
    if context["revision"] != args.revision:
        raise ValueError("Task revision changed; inspect task context and approve the current revision")
    if not args.allow or not all(_safe_path(path) for path in args.allow):
        raise ValueError("Execution requires explicit safe --allow paths or globs")
    if not args.check or not args.reviewer_wrapper:
        raise ValueError("Execution requires --check NAME=COMMAND and an explicit read-only --reviewer-wrapper")
    if not args.model or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+", args.model):
        raise ValueError("Execution requires explicit --model PROVIDER/MODEL; ambient OpenCode model configuration is not inherited")
    if (root / ".opencode").exists() or (root / ".opencode").is_symlink():
        raise ValueError("Project .opencode inputs require explicit Exec policy; remove them from this isolated smoke fixture")
    checks = _checks(args.check)
    fast_checks = _checks(args.fast_check)
    if args.timeout <= 0 or not args.machine_id:
        raise ValueError("Execution requires positive --timeout and nonempty --machine-id")
    if any(not signal.strip() or len(signal) > 80 or "\n" in signal for signal in args.quota_signal):
        raise ValueError("Quota signals must be explicit, bounded, single-line provider markers")
    if not Path(args.reviewer_wrapper).is_file() or not os.access(args.reviewer_wrapper, os.X_OK):
        raise ValueError("Reviewer wrapper must be an existing executable with read-only filesystem policy")
    if not shutil.which(args.opencode):
        raise ValueError("OpenCode executable unavailable; install and configure opencode before starting")
    git_probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, text=True, capture_output=True, check=False)
    if git_probe.returncode == 0:
        if args.allow_no_git:
            raise ValueError("--allow-no-git is only for repositories without Git")
        if Path(git_probe.stdout.strip()).resolve() != root:
            raise ValueError("--root must be Git worktree root")
        head = _git(root, "rev-parse", "HEAD")
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
        if any(line[3:] and not line[3:].startswith(".prway/execution/runtime/") for line in status.splitlines()):
            raise ValueError("Main worktree must be clean before creating an Exec run")
    elif not args.allow_no_git:
        raise ValueError("Git repository required; --allow-no-git explicitly accepts backup-protected weaker isolation")
    run_id = str(uuid.uuid4())
    repository = ({"kind": "git", "base_revision": head} if git_probe.returncode == 0
                  else {"kind": "non_git", "backup_ref": str(backup_path(root, run_id))})
    _progress(f"Creating isolated run for {context['task_id']}...")
    create_run(root, {
        "run_id": run_id, "task_id": context["task_id"], "task_revision": context["revision"],
        "machine_id": args.machine_id, "allowlist": args.allow,
        "repository": repository,
        "checks": checks, "fast_checks": fast_checks, "criteria": context["criteria"],
        "reviewer_wrapper": str(Path(args.reviewer_wrapper).resolve()), "opencode": args.opencode,
        "timeout_seconds": args.timeout, "quota_signals": args.quota_signal, "model": args.model,
    }, "build")
    try:
        (root / ".prway" / "execution" / "runtime" / "local" / "config" / run_id).mkdir(parents=True, exist_ok=False)
        if repository["kind"] == "git":
            create_worktree(root, run_id, "build")
        else:
            backup_and_worktree(root, run_id)
    except BaseException:
        (root / ".prway" / "execution" / "runtime" / "state" / f"{run_id}.json").unlink(missing_ok=True)
        shutil.rmtree(root / ".prway" / "execution" / "runtime" / "local" / "config" / run_id, ignore_errors=True)
        raise
    return run_id


def _controller(root: Path, run_id: str) -> tuple[SingleAgentController, dict[str, object]]:
    record = run_record(root, run_id)
    worktree = worktree_path(root, run_id)
    if not worktree.is_dir():
        raise ValueError("Run worktree unavailable; do not recreate unsynchronized changes automatically")
    environment = {**os.environ, "XDG_CONFIG_HOME": str(root / ".prway" / "execution" / "runtime" / "local" / "config" / run_id)}
    model = record.get("model")
    adapter = OpenCodeAdapter(executable=str(record["opencode"]), reviewer_prefix=(str(record["reviewer_wrapper"]),), quota_signals=tuple(record.get("quota_signals", [])), model=model if isinstance(model, str) else None, environment=environment)
    return SingleAgentController(root, run_id, worktree, adapter), record


def _require_explicit_model(record: dict[str, object]) -> None:
    if not isinstance(record.get("model"), str) or not record["model"]:
        raise ValueError("Legacy run has no explicit model; cancel or complete non-agent stages without invoking OpenCode")


def _remove_local_config(root: Path, run_id: str) -> None:
    path = root / ".prway" / "execution" / "runtime" / "local" / "config" / run_id
    if path.is_symlink():
        raise ValueError("Refusing symlink Exec configuration cleanup")
    if path.is_dir():
        shutil.rmtree(path)


def _run_checks(record: dict[str, object], fast: bool) -> tuple[Check, ...]:
    values = record["fast_checks" if fast else "checks"]
    if not isinstance(values, list):
        raise ValueError("Malformed approved checks")
    return tuple(Check(str(item["name"]), tuple(item["command"]), fast=fast) for item in values)


def _snapshot(worktree: Path, base: str) -> str:
    if not (worktree / ".git").exists():
        return non_git_snapshot(worktree, Path(base))
    diff = _git(worktree, "diff", "--binary", base, "--")
    paths = changed_paths(worktree, base)
    digest = hashlib.sha256(diff.encode())
    for path in paths:
        file = worktree / path
        if file.is_file() and path in _git(worktree, "ls-files", "--others", "--exclude-standard").splitlines():
            digest.update(path.encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()


def _base(record: dict[str, object]) -> str:
    repository = record["repository"]
    if not isinstance(repository, dict):
        raise ValueError("Invalid execution repository")
    return str(repository["base_revision"] if repository["kind"] == "git" else repository["backup_ref"])


def _review_payload(worktree: Path, record: dict[str, object]) -> str:
    repository = record["repository"]
    base = _base(record)
    if repository["kind"] == "git":
        detail = _git(worktree, "diff", "--binary", base, "--")
        untracked = _git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
        changed = set(changed_paths(worktree, base))
        for relative in untracked:
            if relative not in changed:
                continue
            file = worktree / relative
            if file.is_symlink() or not file.is_file():
                raise ValueError("Reviewer cannot safely inspect an untracked symlink or directory")
            try:
                content = file.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("Reviewer cannot inspect binary untracked changes") from error
            detail += f"\nUntracked {relative}:\n{content}"
    else:
        detail = ""
        backup = Path(base)
        for relative in non_git_changes(worktree, backup):
            file = worktree / relative
            if file.exists():
                try:
                    content = file.read_text(encoding="utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError("Reviewer cannot inspect binary non-Git changes") from error
                detail += f"\nChanged {relative}:\n{content}"
            else:
                detail += f"\nDeleted {relative}"
    if len(detail.encode()) > 24000:
        raise ValueError("Diff exceeds bounded reviewer context; split task before review")
    return detail


def _claim_report(root: Path, record: dict[str, object]) -> str:
    claim = record.get("claim")
    if record.get("state") != "completed" or not isinstance(claim, dict):
        raise ValueError("No retained CompletionClaim; complete read-only review and required checks first")
    criteria, checks, paths = claim.get("criterion_results"), claim.get("checks"), claim.get("changed_paths")
    digest = claim.get("diff_sha256")
    proposal = claim.get("accepted_proposal_id")
    if (claim.get("run_id") != record.get("run_id") or claim.get("task_id") != record.get("task_id")
        or claim.get("task_revision") != record.get("task_revision")
        or not isinstance(proposal, str) or not proposal or proposal != record.get("accepted_proposal_id")
        or not isinstance(claim.get("reviewed_snapshot"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", claim["reviewed_snapshot"])
        or claim.get("reviewed_snapshot") != record.get("reviewed_snapshot")
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(criteria, list) or not criteria or len(criteria) > 50
        or not isinstance(checks, list) or not checks or len(checks) > 50
        or not isinstance(paths, list) or len(paths) > 50):
        raise ValueError("Malformed or incomplete retained CompletionClaim")
    lines = [f"CompletionClaim {record['run_id']} | {record['task_id']} | revision {record['task_revision']}",
             f"  Approved proposal: {proposal} | reviewer: passed"]
    for item in criteria:
        if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
            or not item[0] or len(item[0]) > 512 or item[1] is not True):
            raise ValueError("Retained CompletionClaim has an invalid or unsatisfied criterion")
        lines.append(f"  Criterion PASS: {item[0]}")
    for item in checks:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", item["name"]) or item.get("passed") is not True
            or not isinstance(item.get("output_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["output_sha256"])):
            raise ValueError("Retained CompletionClaim has an invalid or failed check")
        lines.append(f"  Check PASS: {item['name']} | output SHA-256 {item['output_sha256']}")
    if not all(isinstance(path, str) and path and len(path) <= 512 and "\n" not in path for path in paths):
        raise ValueError("Retained CompletionClaim has invalid changed paths")
    lines.extend((f"  Changed paths: {', '.join(paths) if paths else '(none)'}", f"  Diff SHA-256: {digest}"))
    if worktree_path(root, str(record["run_id"])).is_dir():
        lines.append("  Next: inspect isolated diff; integrate --mode build --yes only after SDD acceptance")
    else:
        lines.append("  Worktree absent; inspect main and canonical task state before further action")
    return "\n".join(lines)


def _advance(root: Path, args: argparse.Namespace) -> str:
    _require_build(args.mode)
    _require_profile(root)
    if args.action == "resume":
        run_id = args.run_id or ""
        record = run_record(root, run_id)
        target = worktree_path(root, run_id)
        if record["state"] not in ACTIVE_STATES:
            raise ValueError("Only an active or suspended run can resume")
        try:
            revision = str(task_context(root, str(record["task_id"]))["revision"])
        except ValueError:
            revision = "task_removed_or_superseded"
        changed_machine = record["machine_id"] != args.machine_id
        if not changed_machine and not target.is_dir() and revision == record["task_revision"]:
            raise ValueError("Safe local worktree checkpoint missing; do not resume")
        if changed_machine and record["repository"]["kind"] == "non_git" and not Path(str(record["repository"]["backup_ref"])).is_dir():
            raise ValueError("Non-Git backup unavailable; cannot restart")
        if changed_machine and target.exists() and not (args.discard_local and args.yes):
            raise ValueError("Unsynchronized local worktree exists; cross-machine restart requires --discard-local --yes")
        if changed_machine and target.exists():
            if record["repository"]["kind"] == "git":
                cleanup_worktree(root, run_id, target)
            else:
                shutil.rmtree(target)
        resumed = resume(root, run_id, revision, args.machine_id, "build")
        if resumed["state"] == "needs_replan":
            if target.exists():
                if record["repository"]["kind"] == "git":
                    cleanup_worktree(root, run_id, target)
                else:
                    shutil.rmtree(target)
            _remove_local_config(root, run_id)
            return f"Run {run_id}: needs_replan; task revision or scope changed; obsolete worktree removed"
        if resumed["state"] == "failed":
            return f"Run {run_id}: failed; retry budget exhausted after cross-machine restart"
        if changed_machine and resumed["state"] in {"implementing", "proposing_tests"}:
            if record["repository"]["kind"] == "git":
                restore_missing_worktree(root, run_id, "build")
            else:
                backup = Path(str(record["repository"]["backup_ref"]))
                if not backup.is_dir():
                    raise ValueError("Non-Git backup unavailable; cannot restart")
                shutil.copytree(backup, target)
        if not target.is_dir():
            raise ValueError("Run worktree unavailable; no safe local checkpoint to resume")
        if resumed["state"] == "testing":
            flow, _ = _controller(root, run_id)
            _progress(f"Resuming fast checks for run {run_id}...")
            results = flow.fast_checks(_run_checks(resumed, True), "build")
            if run_record(root, run_id)["state"] == "reviewing":
                checkpoint(root, run_id, f"fast_checks:{_snapshot(flow.worktree, _base(resumed))}", "build")
            return f"Run {run_id}: fast checks resumed {[(item.name, item.passed) for item in results]}; state: {run_record(root, run_id)['state']}"
        return f"Run {run_id}: {resumed['state']} | retries: {resumed['retries_consumed']}/{resumed['retry_budget']}"
    flow, record = _controller(root, args.run_id or "")
    run_id = str(record["run_id"])
    timeout = float(record["timeout_seconds"])
    if args.action == "propose":
        _require_explicit_model(record)
        context = task_context(root, str(record["task_id"]))
        if context["revision"] != record["task_revision"]:
            raise ValueError("Task revision changed; cancel this run and request new approval")
        prompt = ("Propose a concise acceptance test plan for this exact task revision. "
                  "Return text in structured JSON text events. Do not modify files.\n"
                  f"Milestone:\n{context['milestone_brief']}\nTask:\n{context['task_brief']}\n"
                  f"Required checks: {record['checks']}\nAllowlist: {record['allowlist']}")
        before = _snapshot(flow.worktree, _base(record))
        invocation_allowed(root, run_id, "proposal", "build")
        _progress(f"Starting proposal agent for {record['task_id']} (timeout {timeout:g}s)...")
        result = flow.propose(prompt, timeout, "build")
        if result.outcome != "completed" or not result.proposal_text or _snapshot(flow.worktree, _base(record)) != before:
            block_run(root, run_id, f"proposal_{result.outcome}_or_modified_worktree", "build")
            raise ValueError("Proposal failed, changed worktree, or provided no bounded text; inspect run before retry")
        proposal_id = hashlib.sha256(f"{run_id}:{record['task_revision']}:{result.proposal_text}".encode()).hexdigest()[:16]
        propose_tests(root, run_id, proposal_id, "build", result.proposal_text)
        return f"Proposed tests [{proposal_id}]: {result.proposal_text}\n[planrunway-exec] Next: approve --run-id {run_id} --proposal-id {proposal_id} --mode build --yes"
    if args.action == "approve":
        if not args.yes or not args.proposal_id:
            raise ValueError("SDD approval requires --proposal-id from the displayed proposal and --yes")
        if task_context(root, str(record["task_id"]))["revision"] != record["task_revision"]:
            raise ValueError("Task revision changed; approval requires a new execution request")
        accept_tests(root, run_id, args.proposal_id, str(record["task_revision"]), "build")
        checkpoint(root, run_id, f"approved:{args.proposal_id}", "build")
        return f"Accepted test proposal {args.proposal_id}; next: implement --run-id {run_id} --mode build"
    if args.action == "implement":
        _require_explicit_model(record)
        context = task_context(root, str(record["task_id"]))
        if context["revision"] != record["task_revision"]:
            raise ValueError("Task revision changed; cancel and restart with a new proposal")
        recent = record.get("events", [])[-3:]
        invocation_allowed(root, run_id, "implementer", "build")
        _progress(f"Starting implementer for {record['task_id']} (timeout {timeout:g}s)...")
        result = flow.implement(f"Implement task below within allowlist {record['allowlist']}. Approved tests: {record.get('proposal_text')}. Recent failure context: {recent}. Task: {context['task_brief']}", timeout, "build")
        if result.outcome != "completed":
            return f"Implementation {result.outcome}; run state: {run_record(root, run_id)['state']}"
        if record["fast_checks"]:
            _progress(f"Running {len(record['fast_checks'])} fast check(s) for run {run_id}...")
        checks = flow.fast_checks(_run_checks(record, True), "build")
        if run_record(root, run_id)["state"] == "reviewing":
            checkpoint(root, run_id, f"fast_checks:{_snapshot(flow.worktree, _base(record))}", "build")
        return f"Implementation finished; fast checks: {[(item.name, item.passed) for item in checks]}; state: {run_record(root, run_id)['state']}"
    if args.action == "review":
        _require_explicit_model(record)
        if record["state"] != "reviewing":
            raise ValueError("Reviewer requires passed fast checks and approved implementation")
        base = _base(record)
        before = _snapshot(flow.worktree, base)
        diff = _review_payload(flow.worktree, record)
        invocation_allowed(root, run_id, "reviewer", "build")
        _progress(f"Starting read-only reviewer for {record['task_id']} (timeout {timeout:g}s)...")
        result = flow.review(f"Read-only review of task {record['task_id']} against criteria {record['criteria']} and diff:\n{diff}\nReturn exactly APPROVED or CHANGES_REQUESTED: concise findings in a text event; never edit files.", timeout, "build")
        if result.outcome != "completed" or _snapshot(flow.worktree, base) != before:
            if run_record(root, run_id)["state"] in ACTIVE_STATES:
                block_run(root, run_id, "review_failed_or_modified_worktree", "build")
            raise ValueError("Reviewer failed or modified isolated worktree; no claim issued")
        if result.review_verdict == "CHANGES_REQUESTED":
            record_attempt(root, run_id, "retryable_failure", result.output_sha256, f"review_changes_requested: {result.review_notes}", "build")
            return f"Reviewer requested changes: {result.review_notes}; state: {run_record(root, run_id)['state']}"
        if result.review_verdict != "APPROVED":
            block_run(root, run_id, "review_missing_structured_verdict", "build")
            raise ValueError("Reviewer did not provide APPROVED or CHANGES_REQUESTED; no claim issued")
        mark_reviewed(root, run_id, before, "build")
        checkpoint(root, run_id, f"reviewed:{before}", "build")
        return f"Reviewer passed; next: claim --run-id {run_id} --mode build"
    if args.action == "claim":
        if record.get("reviewed_snapshot") != _snapshot(flow.worktree, _base(record)):
            raise ValueError("Fresh successful read-only review required for current diff")
        _progress(f"Running {len(record['checks'])} required check(s) for run {run_id}...")
        claim = flow.completion_claim(tuple(str(item) for item in record["criteria"]), _run_checks(record, False), "build")
        retained = asdict(claim)
        retained["reviewed_snapshot"] = record["reviewed_snapshot"]
        retain_claim(root, run_id, retained, "build")
        return _claim_report(root, run_record(root, run_id))
    if args.action == "integrate":
        if not args.yes or record["state"] != "completed" or not isinstance(record.get("claim"), dict):
            raise ValueError("Integration requires reviewed CompletionClaim, explicit SDD approval and --yes")
        if record["claim"].get("reviewed_snapshot") != _snapshot(flow.worktree, _base(record)):
            raise ValueError("Verified patch changed since CompletionClaim; do not integrate")
        _progress(f"Integrating SDD-approved patch for run {run_id}...")
        if record["repository"]["kind"] == "non_git":
            outcome, paths = integrate_non_git(root, run_id, record["allowlist"], record["claim"]["reviewed_snapshot"])
            if outcome != "integrated_non_git_backup_retained":
                raise ValueError(f"{outcome}: main files, allowlist or reviewed snapshot changed; no integration approved")
            _remove_local_config(root, run_id)
            return f"Integration: {outcome} | weaker non-Git isolation; backup retained at {_base(record)} | paths: {paths}"
        result = integrate(root, run_id, flow.worktree)
        if result.outcome == "INTEGRATION_BLOCKED_MAIN_DRIFT":
            raise ValueError("INTEGRATION_BLOCKED_MAIN_DRIFT: main tree, base, index or allowed paths changed; no patch applied")
        _remove_local_config(root, run_id)
        return f"Integration: {result.outcome} | paths: {result.changed_paths}"
    if args.action == "cancel":
        if not args.yes:
            raise ValueError("Cancellation requires --yes")
        cancel_run(root, run_id, "build")
        if record["repository"]["kind"] == "git":
            cleanup_worktree(root, run_id, flow.worktree)
        else:
            shutil.rmtree(flow.worktree)
        _remove_local_config(root, run_id)
        return f"Cancelled run {run_id}; compact state retained"
    raise ValueError(f"Unsupported run action: {args.action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planrunway-exec", description="Explicit single-task Exec lifecycle; no automatic canonical task completion")
    parser.add_argument("action", nargs="?", choices=("inspect", "start", "status", "resume", "propose", "approve", "implement", "review", "claim", "claim-show", "integrate", "cancel"))
    parser.add_argument("--version", action="version", version=f"planrunway-exec {__version__}")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--task", help="Canonical MILESTONE-TASK display ID")
    parser.add_argument("--run-id", help="Run UUID from start")
    parser.add_argument("--mode", choices=("plan", "build"), help="Explicit current OpenCode mode; build required for every mutation")
    parser.add_argument("--revision", help="Exact task brief SHA-256 reported by inspect")
    parser.add_argument("--allow", action="append", default=[], help="SDD-approved relative path or glob; repeatable")
    parser.add_argument("--check", action="append", default=[], help="Required NAME=COMMAND, no shell; repeatable")
    parser.add_argument("--fast-check", action="append", default=[], help="Fast NAME=COMMAND, no shell; repeatable")
    parser.add_argument("--reviewer-wrapper", help="Executable filesystem-read-only wrapper for reviewer")
    parser.add_argument("--opencode", default="opencode")
    parser.add_argument("--model", help="Explicit OpenCode PROVIDER/MODEL; required for start")
    parser.add_argument("--quota-signal", action="append", default=[], help="Explicit unambiguous provider quota signal; repeatable")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--machine-id", default=os.uname().nodename)
    parser.add_argument("--allow-no-git", action="store_true", help="Opt into weaker backup-protected non-Git isolation")
    parser.add_argument("--discard-local", action="store_true", help="Explicitly discard unsynchronized local worktree on cross-machine restart")
    parser.add_argument("--proposal-id", help="Retained SDD proposal identifier")
    parser.add_argument("--yes", action="store_true", help="Explicit SDD approval for reviewed proposal/claim")
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 0
    root = args.root.resolve()
    try:
        if args.action == "inspect":
            context = task_context(root, args.task or "")
            print(f"[planrunway-exec] Task: {context['task_id']} | revision: {context['revision']}")
            for criterion in context["criteria"]:
                print(f"[planrunway-exec] Criterion: {criterion}")
            print("[planrunway-exec] Next: approve revision, allowlist, and checks before start --mode build")
        elif args.action == "start":
            run_id = _start(root, args)
            print(f"[planrunway-exec] Created isolated run {run_id}; next: propose --run-id {run_id} --mode build")
        elif args.action == "status":
            record = run_record(root, args.run_id or "")
            print(f"[planrunway-exec] Run: {record['run_id']} | {record['state']} | task {record['task_id']}")
            next_step = {
                "proposing_tests": "propose --mode build",
                "awaiting_test_approval": f"review proposed tests, then approve --proposal-id {record.get('proposal_id')} --mode build --yes",
                "implementing": "implement --mode build",
                "testing": "review failed checks before retrying",
                "reviewing": "review --mode build" if not record.get("reviewed_snapshot") else "claim --mode build",
                "suspended_quota": "resume --mode build only after provider quota is available",
                "completed": "claim-show" if record.get("claim") else "inspect missing CompletionClaim; do not integrate",
                "failed": "review failure and retry budget; create a new approved request",
                "blocked": "review blocker; do not integrate",
                "needs_replan": "review changed task criteria and start a new approved request",
            }.get(str(record["state"]), "review run state")
            suffix = f" --run-id {record['run_id']}" if record["state"] in {"proposing_tests", "awaiting_test_approval", "implementing", "reviewing", "suspended_quota", "completed"} else ""
            print(f"[planrunway-exec] Next: {next_step}{suffix}")
        elif args.action == "claim-show":
            print(f"[planrunway-exec] {_claim_report(root, run_record(root, args.run_id or ''))}")
        else:
            print(f"[planrunway-exec] {_advance(root, args)}")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"[planrunway-exec] ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
