"""Durable, dependency-free lifecycle state for one execution request."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


FORMAT_VERSION = 1
DEFAULT_RETRY_BUDGET = 2
ACTIVE_STATES = {"proposing_tests", "awaiting_test_approval", "implementing", "testing", "reviewing", "suspended_quota"}
TERMINAL_STATES = {"completed", "failed", "blocked", "needs_replan"}
ATTEMPT_RESULTS = {"retryable_failure", "blocked", "needs_replan", "quota"}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Execution {field} must be a non-empty string")
    return value


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _state_dir(root: Path) -> Path:
    return root / ".prway" / "execution" / "runtime" / "state"


def _state_path(root: Path, run_id: str) -> Path:
    return _state_dir(root) / f"{run_id}.json"


def _load(root: Path, run_id: str) -> dict[str, object]:
    path = _state_path(root, run_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Unknown execution run: {run_id}") from error
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"Malformed execution run: {run_id}")
    _text(value.get("run_id"), "run_id")
    if value.get("run_id") != run_id or value.get("state") not in ACTIVE_STATES | TERMINAL_STATES:
        raise ValueError(f"Malformed execution run: {run_id}")
    return value


def _save(root: Path, record: dict[str, object]) -> None:
    _atomic_json(_state_path(root, _text(record.get("run_id"), "run_id")), record)


def run_record(root: Path, run_id: str) -> dict[str, object]:
    """Return durable run state for controller validation without exposing its storage path."""
    return _load(root, run_id)


def retain_claim(root: Path, run_id: str, claim: dict[str, object], mode: str | None) -> None:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] != "completed" or record.get("claim") is not None:
        raise ValueError("Completion claim is not expected in current execution state")
    record["claim"] = claim
    _save(root, record)


def cancel_run(root: Path, run_id: str, mode: str | None) -> None:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] not in ACTIVE_STATES:
        raise ValueError("Only an active run can be cancelled")
    record["state"] = "needs_replan"
    record["events"].append({"kind": "cancelled"})
    _save(root, record)


def block_run(root: Path, run_id: str, reason: str, mode: str | None) -> None:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] not in ACTIVE_STATES:
        raise ValueError("Only an active run can be blocked")
    record["state"] = "blocked"
    record["events"].append({"kind": "blocked", "reason": _text(reason, "reason")})
    _save(root, record)


def mark_reviewed(root: Path, run_id: str, snapshot: str, mode: str | None) -> None:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] != "reviewing":
        raise ValueError("Read-only review is not expected in current state")
    record["reviewed_snapshot"] = _text(snapshot, "snapshot")
    _save(root, record)


def _require_build(mode: str | None) -> None:
    if mode != "build":
        raise ValueError("Execution mutation requires OpenCode Build mode")


def _active_runs(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(_state_dir(root).glob("*.json")):
        records.append(_load(root, path.stem))
    return [record for record in records if record["state"] in ACTIVE_STATES]


def create_run(root: Path, request: dict[str, object], mode: str | None) -> dict[str, object]:
    """Create a run paused for a retained SDD acceptance-test conclusion."""
    _require_build(mode)
    run_id = _text(request.get("run_id"), "request.run_id")
    _text(request.get("task_id"), "request.task_id")
    _text(request.get("task_revision"), "request.task_revision")
    _text(request.get("machine_id"), "request.machine_id")
    allowlist = request.get("allowlist")
    if not isinstance(allowlist, list) or not allowlist or not all(isinstance(item, str) and item for item in allowlist):
        raise ValueError("Execution request.allowlist must be a non-empty string array")
    retry_budget = request.get("retry_budget", DEFAULT_RETRY_BUDGET)
    if not isinstance(retry_budget, int) or retry_budget < 0:
        raise ValueError("Execution request.retry_budget must be a non-negative integer")
    repository = request.get("repository")
    if not isinstance(repository, dict) or repository.get("kind") not in {"git", "non_git"}:
        raise ValueError("Execution request.repository.kind must be git or non_git")
    if repository["kind"] == "git":
        _text(repository.get("base_revision"), "request.repository.base_revision")
    else:
        _text(repository.get("backup_ref"), "request.repository.backup_ref")
    if _state_path(root, run_id).exists():
        raise ValueError(f"Execution run already exists: {run_id}")
    active = _active_runs(root)
    if active:
        raise ValueError(f"Execution run already active: {active[0]['run_id']}")
    record: dict[str, object] = {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "task_id": request["task_id"],
        "task_revision": request["task_revision"],
        "machine_id": request["machine_id"],
        "repository": repository,
        "allowlist": allowlist,
        "retry_budget": retry_budget,
        "retries_consumed": 0,
        "state": "proposing_tests",
        "events": [],
    }
    for key in ("checks", "fast_checks", "criteria", "reviewer_wrapper", "opencode", "timeout_seconds", "quota_signals", "model"):
        if key in request:
            record[key] = request[key]
    _save(root, record)
    return record


def propose_tests(root: Path, run_id: str, proposal_id: str, mode: str | None, proposal_text: str | None = None) -> dict[str, object]:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] != "proposing_tests":
        raise ValueError("Acceptance-test proposal is not expected in current execution state")
    record["proposal_id"] = _text(proposal_id, "proposal_id")
    if proposal_text is not None:
        record["proposal_text"] = _text(proposal_text, "proposal_text")
    record["state"] = "awaiting_test_approval"
    _save(root, record)
    return record


def accept_tests(root: Path, run_id: str, proposal_id: str, task_revision: str, mode: str | None) -> dict[str, object]:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] != "awaiting_test_approval" or record.get("proposal_id") != proposal_id:
        raise ValueError("Execution requires its retained SDD acceptance-test conclusion")
    if record["task_revision"] != task_revision:
        record["state"] = "needs_replan"
        record["events"].append({"kind": "needs_replan", "reason": "task_revision_changed"})
    else:
        record["accepted_proposal_id"] = proposal_id
        record["state"] = "implementing"
    _save(root, record)
    return record


def invocation_allowed(root: Path, run_id: str, role: str, mode: str | None) -> None:
    """Enforce retained SDD approval before a write-capable agent starts."""
    _require_build(mode)
    record = _load(root, run_id)
    required_state = {"proposal": "proposing_tests", "implementer": "implementing", "reviewer": "reviewing"}.get(role)
    if required_state is None or record["state"] != required_state:
        raise ValueError(f"OpenCode {role} invocation is not expected in current execution state")
    if role == "implementer" and not isinstance(record.get("accepted_proposal_id"), str):
        raise ValueError("Implementation requires its retained SDD acceptance-test conclusion")


def transition(root: Path, run_id: str, target: str, mode: str | None) -> dict[str, object]:
    """Advance deterministic implementation, test, review, and completion boundaries."""
    _require_build(mode)
    record = _load(root, run_id)
    transitions = {
        "implementing": {"testing"},
        "testing": {"reviewing"},
        "reviewing": {"completed"},
    }
    if target not in transitions.get(record["state"], set()):
        raise ValueError(f"Invalid execution state transition: {record['state']} to {target}")
    record["state"] = target
    _save(root, record)
    return record


def checkpoint(root: Path, run_id: str, reference: str, mode: str | None) -> dict[str, object]:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] not in {"implementing", "testing", "reviewing"}:
        raise ValueError("Execution checkpoint is not expected in current state")
    record["checkpoint"] = _text(reference, "checkpoint")
    _save(root, record)
    return record


def record_attempt(root: Path, run_id: str, result: str, fingerprint: str, reason: str, mode: str | None) -> dict[str, object]:
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] not in {"implementing", "testing", "reviewing"} or result not in ATTEMPT_RESULTS:
        raise ValueError("Execution attempt is not expected in current state")
    event = {"kind": "attempt", "result": result, "fingerprint": _text(fingerprint, "fingerprint"), "reason": _text(reason, "reason")}
    record["events"].append(event)
    if result == "quota":
        record["resume_state"] = record["state"]
        record["state"] = "suspended_quota"
    elif result in {"blocked", "needs_replan"}:
        record["state"] = result
    else:
        previous = record.get("last_failure_fingerprint")
        record["last_failure_fingerprint"] = fingerprint
        if previous == fingerprint:
            record["state"] = "blocked"
            record["events"].append({"kind": "blocked", "reason": "no_progress"})
        else:
            record["retries_consumed"] = int(record["retries_consumed"]) + 1
            record["state"] = "failed" if int(record["retries_consumed"]) > int(record["retry_budget"]) else "implementing"
    _save(root, record)
    return record


def resume(root: Path, run_id: str, task_revision: str, machine_id: str, mode: str | None) -> dict[str, object]:
    """Resume from a compact checkpoint or restart safely on a different machine."""
    _require_build(mode)
    record = _load(root, run_id)
    if record["state"] not in ACTIVE_STATES:
        raise ValueError("Execution run is not resumable")
    if record["task_revision"] != task_revision:
        record["state"] = "needs_replan"
        record["events"].append({"kind": "needs_replan", "reason": "task_revision_changed"})
    elif record["machine_id"] == machine_id:
        if record["state"] == "suspended_quota":
            record["state"] = record["resume_state"]
    else:
        record["retries_consumed"] = int(record["retries_consumed"]) + 1
        record["machine_id"] = _text(machine_id, "machine_id")
        record.pop("checkpoint", None)
        record.pop("reviewed_snapshot", None)
        if int(record["retries_consumed"]) > int(record["retry_budget"]):
            record["state"] = "failed"
        elif not isinstance(record.get("accepted_proposal_id"), str):
            record["state"] = "proposing_tests"
            record.pop("proposal_id", None)
            record.pop("proposal_text", None)
        else:
            record["state"] = "implementing"
        record["events"].append({"kind": "cross_machine_restart"})
    _save(root, record)
    return record


def resume_quota(root: Path, run_id: str, task_revision: str, machine_id: str, mode: str | None) -> dict[str, object]:
    record = _load(root, run_id)
    if record["state"] != "suspended_quota":
        raise ValueError("Execution run is not suspended for quota")
    return resume(root, run_id, task_revision, machine_id, mode)
