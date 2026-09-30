"""Entity-scoped PlanRunway planning metadata and derived navigation views."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path


SCHEMA_VERSION = "0.4.0"
MILESTONE_STATES = {"pending", "in_progress", "blocked", "ready_to_close", "done", "archived"}
TASK_STATES = {"pending", "in_progress", "blocked", "awaiting_manual", "done", "archived"}
ORDER_WIDTH = 36
ORDER_STEP = 10**24


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
    """Use one canonical line so any two same-entity edits conflict in ordinary Git."""
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


def _manifest_path(state: Path) -> Path:
    return state / "meta" / "plan.json"


def _manifest(state: Path) -> dict[str, object]:
    path = _manifest_path(state)
    if not path.is_file():
        raise ValueError("Missing canonical planning manifest")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value != {"schema_version": SCHEMA_VERSION}:
        raise ValueError("Unsupported canonical planning metadata format")
    return value


def initialize_planning(state: Path) -> None:
    _atomic_json(_manifest_path(state), {"schema_version": SCHEMA_VERSION})
    for directory in (state / "meta" / "milestones", state / "meta" / "tasks"):
        directory.mkdir(parents=True, exist_ok=True)


def _record_paths(state: Path, kind: str) -> list[Path]:
    return sorted((state / "meta" / f"{kind}s").glob("*.json"))


def _records(state: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    _manifest(state)
    records: list[list[dict[str, object]]] = []
    for kind in ("milestone", "task"):
        group: list[dict[str, object]] = []
        for path in _record_paths(state, kind):
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("kind") != kind or value.get("model_id") != path.stem:
                raise ValueError(f"Malformed canonical {kind} record: {path.name}")
            group.append(value)
        records.append(group)
    return records[0], records[1]


def _text(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Canonical planning record has invalid {field}")
    return value


def _order(record: dict[str, object]) -> int:
    value = _text(record, "order_key")
    if len(value) != ORDER_WIDTH or not value.isdigit():
        raise ValueError("Canonical planning record has invalid sparse order key")
    return int(value)


def _ordered(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(records, key=lambda item: (_order(item), _text(item, "model_id")))


def _record_path(state: Path, kind: str, model_id: str) -> Path:
    return state / "meta" / f"{kind}s" / f"{model_id}.json"


def _save_record(state: Path, record: dict[str, object]) -> None:
    kind = _text(record, "kind")
    if kind not in {"milestone", "task"}:
        raise ValueError("Unsupported canonical record kind")
    _atomic_record(_record_path(state, kind, _text(record, "model_id")), record)


def _next_order(records: list[dict[str, object]]) -> str:
    highest = max((_order(record) for record in records), default=0)
    if highest > 10**ORDER_WIDTH - ORDER_STEP:
        raise ValueError("Order key space exhausted; run an explicit ordering normalization")
    return str(highest + ORDER_STEP).zfill(ORDER_WIDTH)


def lint_planning(state: Path) -> None:
    milestones, tasks = _records(state)
    milestone_ids: set[str] = set()
    milestone_display_ids: set[str] = set()
    for milestone in milestones:
        identifier = _text(milestone, "model_id")
        display_id = _text(milestone, "display_id")
        if identifier in milestone_ids or (milestone.get("state") != "archived" and display_id in milestone_display_ids):
            raise ValueError("Canonical milestone records have duplicate identity")
        _text(milestone, "title")
        _order(milestone)
        if milestone.get("state") not in MILESTONE_STATES:
            raise ValueError("Canonical milestone records are invalid")
        milestone_ids.add(identifier)
        if milestone.get("state") != "archived":
            milestone_display_ids.add(display_id)
    task_ids: set[str] = set()
    task_refs: set[tuple[str, str]] = set()
    for task in tasks:
        identifier = _text(task, "model_id")
        milestone_id = _text(task, "milestone_id")
        reference = (milestone_id, _text(task, "display_id"))
        if identifier in task_ids or (task.get("state") != "archived" and reference in task_refs) or milestone_id not in milestone_ids:
            raise ValueError("Canonical task records have duplicate or unknown identity")
        _text(task, "title")
        _order(task)
        if task.get("state") not in TASK_STATES:
            raise ValueError("Canonical task records are invalid")
        task_ids.add(identifier)
        if task.get("state") != "archived":
            task_refs.add(reference)


def render_status(state: Path, write: bool = False) -> str:
    milestones, tasks = _records(state)
    lines = ["# PlanRunway Execution Status", "", f"Plan system version: {SCHEMA_VERSION}", ""]
    for milestone in _ordered(milestones):
        lines.extend([f"## {milestone['display_id']} | {milestone['title']} | {milestone['state']}", ""])
        matching = [task for task in tasks if task["milestone_id"] == milestone["model_id"]]
        lines.extend(f"- {milestone['display_id']}-{task['display_id']} | {task['state']} | {task['title']}" for task in _ordered(matching))
        lines.append("")
    content = "\n".join(lines)
    if write:
        path = state / "execution" / "execution_status.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return content


def active_status(state: Path) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    milestones, tasks = _records(state)
    if not milestones:
        return None, None
    ordered_milestones = _ordered(milestones)
    by_id = {str(item["model_id"]): item for item in milestones}
    ordered_tasks = sorted(tasks, key=lambda item: (_order(by_id[str(item["milestone_id"])]), _order(item), _text(item, "model_id")))
    active_task = next((item for item in ordered_tasks if item["state"] == "in_progress"), None)
    if active_task is None:
        active_milestone = next((item for item in ordered_milestones if item["state"] == "in_progress"), None)
        active_task = next((item for item in ordered_tasks if active_milestone and item["milestone_id"] == active_milestone["model_id"] and item["state"] == "pending"), None)
    if active_task is not None:
        return by_id[str(active_task["milestone_id"])], active_task
    return next((item for item in ordered_milestones if item["state"] == "in_progress"), None), None


def create_milestone(state: Path, display_id: str, title: str) -> None:
    if not display_id or not title:
        raise ValueError("milestone-create requires --id and --title")
    milestones, _ = _records(state)
    if any(item.get("display_id") == display_id for item in milestones):
        raise ValueError(f"Duplicate milestone ID: {display_id}")
    model_id = str(uuid.uuid4())
    _save_record(state, {"kind": "milestone", "model_id": model_id, "display_id": display_id, "title": title, "state": "pending", "order_key": _next_order(milestones)})


def create_task(state: Path, milestone_display_id: str, display_id: str, title: str) -> None:
    if not milestone_display_id or not display_id or not title:
        raise ValueError("task-create requires --milestone, --id, and --title")
    milestones, tasks = _records(state)
    milestone = next((item for item in milestones if item.get("display_id") == milestone_display_id), None)
    if milestone is None:
        raise ValueError(f"Unknown milestone: {milestone_display_id}")
    siblings = [item for item in tasks if item.get("milestone_id") == milestone["model_id"]]
    if any(item.get("display_id") == display_id for item in siblings):
        raise ValueError(f"Duplicate task ID: {milestone_display_id}-{display_id}")
    model_id = str(uuid.uuid4())
    _save_record(state, {"kind": "task", "model_id": model_id, "milestone_id": milestone["model_id"], "display_id": display_id, "title": title, "state": "pending", "order_key": _next_order(siblings)})


def _target(state: Path, kind: str, display_id: str, milestone_display_id: str | None) -> dict[str, object]:
    milestones, tasks = _records(state)
    records = milestones if kind == "milestone" else tasks
    milestone_id: str | None = None
    if kind == "task":
        milestone = next((item for item in milestones if item.get("display_id") == milestone_display_id), None)
        if milestone is None:
            raise ValueError(f"Unknown milestone: {milestone_display_id}")
        milestone_id = _text(milestone, "model_id")
    record = next((item for item in records if item.get("display_id") == display_id and (kind == "milestone" or item.get("milestone_id") == milestone_id)), None)
    if record is None:
        raise ValueError(f"Unknown {kind}: {display_id}")
    return record


def set_state(state: Path, kind: str, display_id: str, target_state: str, milestone_display_id: str | None = None) -> None:
    record = _target(state, kind, display_id, milestone_display_id)
    states = MILESTONE_STATES if kind == "milestone" else TASK_STATES
    transitions = ({"pending": {"in_progress", "blocked"}, "in_progress": {"blocked", "ready_to_close"}, "blocked": {"pending", "in_progress"}, "ready_to_close": {"done", "in_progress"}} if kind == "milestone" else {"pending": {"in_progress", "blocked"}, "in_progress": {"blocked", "awaiting_manual", "done"}, "blocked": {"pending", "in_progress"}, "awaiting_manual": {"done", "in_progress"}})
    if target_state not in states or target_state not in transitions.get(record.get("state"), set()):
        raise ValueError(f"Invalid {kind} state transition: {record.get('state')} to {target_state}")
    record["state"] = target_state
    _save_record(state, record)


def change_record(state: Path, kind: str, action: str, display_id: str, milestone_display_id: str | None = None, successor: str | None = None) -> None:
    record = _target(state, kind, display_id, milestone_display_id)
    if action == "reopen":
        if record.get("state") != "done":
            raise ValueError(f"Only done {kind} can reopen")
        record["state"] = "pending"
    else:
        record["state"] = "archived"
        if action == "supersede":
            if not successor:
                raise ValueError("supersede requires --successor")
            record["relation"] = {"type": "superseded_by", "value": successor}
    _save_record(state, record)


def prepare_review(state: Path, milestone_id: str) -> None:
    milestones, tasks = _records(state)
    milestone = next((item for item in milestones if item["display_id"] == milestone_id), None)
    if milestone is None:
        raise ValueError(f"Unknown milestone: {milestone_id}")
    summary = "\n".join(f"- {milestone_id}-{item['display_id']} | {item['state']} | {item['title']}" for item in _ordered([item for item in tasks if item["milestone_id"] == milestone["model_id"]]))
    for directory, suffix, heading in (("releases", "release_notes", "Release Notes"), ("manual_testing", "manual_testing_guide", "Manual Testing Guide")):
        path = state / directory / f"{milestone_id}_{suffix}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"# {milestone['title']} - {heading}\n\nMilestone: {milestone_id}\n\n## Tasks\n\n{summary}\n", encoding="utf-8")


def reorder(state: Path, kind: str, display_id: str, milestone_display_id: str | None, before: str | None, after: str | None) -> None:
    if bool(before) == bool(after):
        raise ValueError("reorder requires exactly one of --before or --after")
    target = _target(state, kind, display_id, milestone_display_id)
    milestones, tasks = _records(state)
    sequence = milestones if kind == "milestone" else [item for item in tasks if item["milestone_id"] == target["milestone_id"]]
    sequence = [item for item in _ordered(sequence) if item["model_id"] != target["model_id"]]
    anchor = next((index for index, item in enumerate(sequence) if item.get("display_id") == (before or after)), None)
    if anchor is None:
        raise ValueError(f"Unknown {kind} reorder target: {before or after}")
    index = anchor if before else anchor + 1
    lower = _order(sequence[index - 1]) if index else 0
    upper = _order(sequence[index]) if index < len(sequence) else 10**ORDER_WIDTH - 1
    if upper - lower < 2:
        raise ValueError("Order key space exhausted at this position; run an explicit ordering normalization")
    target["order_key"] = str((lower + upper) // 2).zfill(ORDER_WIDTH)
    _save_record(state, target)


def migrate_legacy_planning(state: Path) -> bool:
    path = _manifest_path(state)
    if not path.is_file():
        raise ValueError("Missing canonical planning manifest")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value == {"schema_version": SCHEMA_VERSION}:
        return False
    if not isinstance(value, dict) or value.get("schema_version") != "0.3.0" or not isinstance(value.get("milestones"), list) or not isinstance(value.get("tasks"), list):
        raise ValueError("Unsupported planning migration source")
    legacy_milestones = [item for item in value["milestones"] if isinstance(item, dict)]
    legacy_tasks = [item for item in value["tasks"] if isinstance(item, dict)]
    backup = path.with_suffix(".0.3.0.json")
    if backup.exists():
        raise ValueError("Planning migration backup already exists")
    path.replace(backup)
    try:
        initialize_planning(state)
        # Legacy order keys were opaque strings; preserve serialized list order once.
        for kind, records in (("milestone", legacy_milestones), ("task", legacy_tasks)):
            for index, record in enumerate(records, start=1):
                migrated = dict(record)
                migrated["kind"] = kind
                migrated["order_key"] = str(index * ORDER_STEP).zfill(ORDER_WIDTH)
                _save_record(state, migrated)
    except BaseException:
        path.unlink(missing_ok=True)
        backup.replace(path)
        raise
    return True


def glossary(state: Path, action: str, term: str | None = None, definition: str | None = None) -> str:
    path = state / "vision" / "glossary.md"
    content = path.read_text(encoding="utf-8") if path.exists() else "# Glossary\n"
    headings = [line[3:].strip() for line in content.splitlines() if line.startswith("## ")]
    if action == "validate":
        if len(headings) != len(set(headings)):
            raise ValueError("Glossary contains duplicate terms")
        return content
    if action == "list":
        return "\n".join(headings) + ("\n" if headings else "")
    if not term or not definition:
        raise ValueError("glossary-add requires --term and --definition")
    if term in headings:
        raise ValueError(f"Glossary term already exists: {term}")
    path.write_text(content.rstrip() + f"\n\n## {term}\n\n{definition}\n", encoding="utf-8")
    return term
