"""Versioned, dependency-free final-evidence record validation and storage."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


FORMAT_VERSION = 1
FINAL_STATES = {"final", "historical", "legacy", "unverified"}
CHECK_RESULTS = {"pass", "fail", "deferred", "skipped"}
CRITERION_RESULTS = {"satisfied", "not_satisfied", "deferred"}
ASSOCIATION_STATES = {"exact", "range", "pending", "inconclusive"}
TASK_STATES = {"pending", "in_progress", "blocked", "awaiting_manual", "done", "legacy", "unverified"}


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Evidence field {field} must be a non-empty string")
    return value



def _validate_result_records(records: object, name: str, results: set[str]) -> None:
    if not isinstance(records, list):
        raise ValueError(f"Evidence field {name} must be an array")
    ids: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Evidence {name}[{index}] must be an object")
        identifier = _require_text(record.get("id"), f"{name}[{index}].id")
        if identifier in ids:
            raise ValueError(f"Evidence {name} contains duplicate id: {identifier}")
        ids.add(identifier)
        if record.get("result") not in results:
            raise ValueError(f"Evidence {name}[{index}] has unsupported result")
        _require_text(record.get("summary"), f"{name}[{index}].summary")
        _require_text(record.get("evidence_ref"), f"{name}[{index}].evidence_ref")


def validate_final_run(record: object) -> dict[str, object]:
    if not isinstance(record, dict) or record.get("format_version") != FORMAT_VERSION:
        raise ValueError("Unsupported final evidence format")
    for field in ("run_id", "task_id", "requirement_revision", "recorded_at"):
        _require_text(record.get(field), field)
    if record.get("state") not in FINAL_STATES:
        raise ValueError("Evidence state must be final, historical, legacy, or unverified")
    repository = record.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("Evidence repository must be an object")
    if repository.get("kind") == "git":
        for field in ("base_revision", "observed_head", "patch_sha256"):
            _require_text(repository.get(field), f"repository.{field}")
        if not isinstance(repository.get("changed_files"), list) or not all(
            isinstance(path, str) and path for path in repository["changed_files"]
        ):
            raise ValueError("Evidence repository.changed_files must be an array of paths")
    elif repository.get("kind") == "non_git":
        _require_text(repository.get("file_snapshot_sha256"), "repository.file_snapshot_sha256")
    else:
        raise ValueError("Evidence repository.kind must be git or non_git")
    _validate_result_records(record.get("checks"), "checks", CHECK_RESULTS)
    _validate_result_records(record.get("criteria"), "criteria", CRITERION_RESULTS)
    conclusion = record.get("accepted_test_conclusion")
    if not isinstance(conclusion, dict) or conclusion.get("decision") != "accepted":
        raise ValueError("Evidence requires an accepted acceptance-test conclusion")
    for field in ("proposal_id", "decided_at", "reason"):
        _require_text(conclusion.get(field), f"accepted_test_conclusion.{field}")
    if not isinstance(conclusion.get("test_plan"), list) or not conclusion["test_plan"]:
        raise ValueError("Evidence accepted_test_conclusion.test_plan must be a non-empty array")
    association = record.get("git_association")
    if not isinstance(association, dict) or association.get("state") not in ASSOCIATION_STATES:
        raise ValueError("Evidence git_association has unsupported state")
    if association["state"] == "exact":
        _require_text(association.get("commit"), "git_association.commit")
    if association["state"] == "range":
        _require_text(association.get("from"), "git_association.from")
        _require_text(association.get("to"), "git_association.to")
    if not isinstance(record.get("environment"), dict):
        raise ValueError("Evidence environment must be an object")
    if not isinstance(record.get("artifacts", []), list):
        raise ValueError("Evidence artifacts must be an array")
    return record


def fingerprint_files(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".prway" not in item.parts):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
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


def _atomic_record(path: Path, value: object) -> None:
    """Same evidence entity edits must become ordinary Git conflicts, never field-wise merges."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def store_final_run(state: Path, record: object) -> Path:
    validated = validate_final_run(record)
    run_path = state / "execution" / "runs" / str(validated["run_id"]) / "final_run.json"
    if run_path.exists():
        raise ValueError(f"Final run already exists: {validated['run_id']}")
    _atomic_json(run_path, validated)
    return run_path


def append_event(state: Path, task_id: str, event: dict[str, object]) -> Path:
    if event.get("format_version") != FORMAT_VERSION or event.get("task_id") != task_id:
        raise ValueError("Event must have current format version and matching task ID")
    if event.get("kind") == "manual_confirmation":
        if event.get("decision") not in {"confirmed", "rejected"}:
            raise ValueError("Manual confirmation decision must be confirmed or rejected")
        _require_text(event.get("operator"), "event.operator")
        _require_text(event.get("notes"), "event.notes")
    elif event.get("kind") == "attempt":
        _require_text(event.get("result"), "event.result")
        _require_text(event.get("reason"), "event.reason")
    else:
        raise ValueError("Unsupported evidence event")
    path = state / "execution" / "events" / f"{task_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return path


def _tasks_path(state: Path) -> Path:
    return state / "execution" / "tasks.json"


def _task_path(state: Path, task_id: str) -> Path:
    return state / "execution" / "tasks" / f"{task_id}.json"


def _load_tasks(state: Path) -> dict[str, object]:
    path = _tasks_path(state)
    if path.exists():
        raise ValueError("Legacy task evidence registry requires planrunway migrate --yes")
    tasks: dict[str, object] = {}
    for record_path in sorted((state / "execution" / "tasks").glob("*.json")):
        task = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(task, dict) or task.get("format_version") != FORMAT_VERSION or task.get("task_id") != record_path.stem:
            raise ValueError(f"Task evidence record is malformed: {record_path.name}")
        tasks[record_path.stem] = task
    return {"format_version": FORMAT_VERSION, "tasks": tasks}


def _save_task(state: Path, task_id: str, task: dict[str, object]) -> None:
    task["format_version"] = FORMAT_VERSION
    task["task_id"] = task_id
    _atomic_record(_task_path(state, task_id), task)


def register_task(state: Path, task_id: str, criteria: list[str], checks: list[str], requirement_revision: str) -> None:
    _require_text(task_id, "task_id")
    _require_text(requirement_revision, "requirement_revision")
    if not criteria or not checks or len(criteria) != len(set(criteria)) or len(checks) != len(set(checks)):
        raise ValueError("Task criteria and checks must be non-empty unique IDs")
    registry = _load_tasks(state)
    tasks = registry["tasks"]
    assert isinstance(tasks, dict)
    if task_id in tasks:
        raise ValueError(f"Task already exists: {task_id}")
    tasks[task_id] = {
        "state": "pending",
        "criteria": criteria,
        "checks": checks,
        "requirement_revision": requirement_revision,
        "final_run": None,
    }
    _save_task(state, task_id, tasks[task_id])


def complete_task(state: Path, record: object, awaiting_manual: bool = False) -> None:
    validated = validate_final_run(record)
    registry = _load_tasks(state)
    tasks = registry["tasks"]
    assert isinstance(tasks, dict)
    task = tasks.get(validated["task_id"])
    if not isinstance(task, dict):
        raise ValueError(f"Unknown task: {validated['task_id']}")
    if task.get("requirement_revision") != validated["requirement_revision"]:
        raise ValueError("Final evidence is stale for current task requirements")
    if {item["id"] for item in validated["checks"]} != set(task.get("checks", [])):
        raise ValueError("Final evidence does not cover declared required checks")
    if {item["id"] for item in validated["criteria"]} != set(task.get("criteria", [])):
        raise ValueError("Final evidence does not cover declared acceptance criteria")
    if any(item["result"] != "pass" for item in validated["checks"]):
        raise ValueError("Final evidence has a non-passing required check")
    if any(item["result"] != "satisfied" for item in validated["criteria"]):
        raise ValueError("Final evidence has an unsatisfied acceptance criterion")
    store_final_run(state, validated)
    task["final_run"] = validated["run_id"]
    task["state"] = "awaiting_manual" if awaiting_manual else "done"
    _save_task(state, str(validated["task_id"]), task)


def confirm_task(state: Path, task_id: str, operator: str, notes: str, confirmed: bool) -> None:
    registry = _load_tasks(state)
    tasks = registry["tasks"]
    assert isinstance(tasks, dict)
    task = tasks.get(task_id)
    if not isinstance(task, dict) or task.get("state") != "awaiting_manual":
        raise ValueError("Only a task awaiting manual confirmation can be confirmed or rejected")
    append_event(state, task_id, {
        "format_version": FORMAT_VERSION,
        "kind": "manual_confirmation",
        "task_id": task_id,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "operator": operator,
        "decision": "confirmed" if confirmed else "rejected",
        "notes": notes,
    })
    task["state"] = "done" if confirmed else "in_progress"
    _save_task(state, task_id, task)


def revise_task(state: Path, task_id: str, requirement_revision: str) -> None:
    registry = _load_tasks(state)
    tasks = registry["tasks"]
    assert isinstance(tasks, dict)
    task = tasks.get(task_id)
    if not isinstance(task, dict):
        raise ValueError(f"Unknown task: {task_id}")
    if task.get("requirement_revision") == requirement_revision:
        return
    task["requirement_revision"] = requirement_revision
    if task.get("state") == "done":
        task["state"] = "in_progress"
        task["historical_run"] = task.get("final_run")
        task["final_run"] = None
    _save_task(state, task_id, task)


def bind_git_association(state: Path, task_id: str, association: dict[str, object]) -> None:
    if association.get("state") not in ASSOCIATION_STATES:
        raise ValueError("Git association has unsupported state")
    if association["state"] == "exact":
        _require_text(association.get("commit"), "git_association.commit")
    if association["state"] == "range":
        _require_text(association.get("from"), "git_association.from")
        _require_text(association.get("to"), "git_association.to")
    registry = _load_tasks(state)
    tasks = registry["tasks"]
    assert isinstance(tasks, dict)
    task = tasks.get(task_id)
    if not isinstance(task, dict) or not isinstance(task.get("final_run"), str):
        raise ValueError("Task has no final run to bind")
    path = state / "execution" / "runs" / task["final_run"] / "final_run.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    validate_final_run(record)
    record["git_association"] = association
    _atomic_json(path, record)


def lint_tasks(state: Path) -> None:
    registry = _load_tasks(state)
    tasks = registry["tasks"]
    assert isinstance(tasks, dict)
    for task_id, task in tasks.items():
        if not isinstance(task, dict) or task.get("state") not in TASK_STATES:
            raise ValueError(f"Task evidence record is malformed: {task_id}")
        if task["state"] not in {"done", "awaiting_manual"}:
            continue
        run_id = task.get("final_run")
        if not isinstance(run_id, str):
            raise ValueError(f"Completed task lacks final evidence: {task_id}")
        path = state / "execution" / "runs" / run_id / "final_run.json"
        if not path.is_file():
            raise ValueError(f"Completed task final evidence is missing: {task_id}")
        record = validate_final_run(json.loads(path.read_text(encoding="utf-8")))
        if record["state"] != "final" or record["task_id"] != task_id:
            raise ValueError(f"Completed task final evidence is invalid: {task_id}")
        if record["requirement_revision"] != task.get("requirement_revision"):
            raise ValueError(f"Completed task final evidence is stale: {task_id}")
        if {item["id"] for item in record["checks"]} != set(task.get("checks", [])) or any(
            item["result"] != "pass" for item in record["checks"]
        ):
            raise ValueError(f"Completed task required checks are incomplete: {task_id}")
        if {item["id"] for item in record["criteria"]} != set(task.get("criteria", [])) or any(
            item["result"] != "satisfied" for item in record["criteria"]
        ):
            raise ValueError(f"Completed task criteria are incomplete: {task_id}")


def render_evidence_status(state: Path, write: bool = False) -> str:
    registry = _load_tasks(state)
    tasks = registry["tasks"]
    assert isinstance(tasks, dict)
    lines = ["# Evidence Status", ""]
    if not tasks:
        lines.append("No task evidence recorded.")
    for task_id in sorted(tasks):
        task = tasks[task_id]
        assert isinstance(task, dict)
        run_id = task.get("final_run")
        detail = "no final evidence"
        if task.get("state") in {"legacy", "unverified"}:
            detail = f"{task['state']} evidence; no fabricated final run"
        if isinstance(run_id, str):
            path = state / "execution" / "runs" / run_id / "final_run.json"
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
                association = record.get("git_association", {}) if isinstance(record, dict) else {}
                association_state = association.get("state", "invalid") if isinstance(association, dict) else "invalid"
                checks = record.get("checks", []) if isinstance(record, dict) else []
                counts = {result: 0 for result in CHECK_RESULTS}
                if isinstance(checks, list):
                    for check in checks:
                        if isinstance(check, dict) and check.get("result") in counts:
                            counts[check["result"]] += 1
                detail = (
                    f"final run {run_id}; Git association: {association_state}; checks "
                    f"pass={counts['pass']} fail={counts['fail']} deferred={counts['deferred']} skipped={counts['skipped']}"
                )
        elif isinstance(task.get("historical_run"), str):
            detail = f"historical evidence: run {task['historical_run']}"
        lines.append(f"- {task_id}: {task.get('state', 'invalid')}; {detail}")
    content = "\n".join(lines) + "\n"
    if write:
        path = state / "execution" / "evidence_status.md"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    return content


def migrate_legacy_tasks(state: Path) -> bool:
    path = _tasks_path(state)
    if not path.exists():
        return False
    registry = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or registry.get("format_version") != FORMAT_VERSION or not isinstance(registry.get("tasks"), dict):
        raise ValueError("Unsupported task evidence registry format")
    backup = path.with_suffix(".legacy.json")
    if backup.exists():
        raise ValueError("Task evidence migration backup already exists")
    path.replace(backup)
    try:
        for task_id, task in registry["tasks"].items():
            if not isinstance(task_id, str) or not isinstance(task, dict):
                raise ValueError("Task evidence registry is malformed")
            _save_task(state, task_id, dict(task))
    except BaseException:
        for record_path in (state / "execution" / "tasks").glob("*.json"):
            record_path.unlink()
        backup.replace(path)
        raise
    return True
