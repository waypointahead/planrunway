"""Entity-scoped PlanRunway planning metadata and derived navigation views."""
from __future__ import annotations

import json
import hashlib
import os
import re
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
        review = milestone.get("closure_review")
        if review is not None and (not isinstance(review, dict) or review.get("format_version") != 1):
            raise ValueError(f"Malformed milestone closure review: {display_id}")
        goal_review = milestone.get("goal_review")
        if goal_review is not None and (not isinstance(goal_review, dict) or goal_review.get("format_version") != 1):
            raise ValueError(f"Malformed milestone goal review: {display_id}")
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
        revision = task.get("delivery_revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("Canonical task record has invalid delivery revision")
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
        if active_milestone is None:
            active_milestone = next((item for item in ordered_milestones if item["state"] == "pending"), None)
        active_task = next((item for item in ordered_tasks if active_milestone and item["milestone_id"] == active_milestone["model_id"] and item["state"] == "awaiting_manual"), None)
        if active_task is None:
            active_task = next((item for item in ordered_tasks if active_milestone and item["milestone_id"] == active_milestone["model_id"] and item["state"] == "pending"), None)
    if active_task is not None:
        return by_id[str(active_task["milestone_id"])], active_task
    active_milestone = next((item for item in ordered_milestones if item["state"] == "in_progress"), None)
    if active_milestone is None:
        active_milestone = next((item for item in ordered_milestones if item["state"] == "pending"), None)
    return active_milestone, None


def create_milestone(state: Path, display_id: str, title: str) -> None:
    if not display_id or not title:
        raise ValueError("milestone-create requires --id and --title")
    milestones, _ = _records(state)
    if any(item.get("display_id") == display_id for item in milestones):
        raise ValueError(f"Duplicate milestone ID: {display_id}")
    model_id = str(uuid.uuid4())
    _save_record(state, {"kind": "milestone", "model_id": model_id, "display_id": display_id, "title": title, "state": "pending", "order_key": _next_order(milestones)})


def create_task(state: Path, milestone_display_id: str, display_id: str, title: str, before: str | None = None, after: str | None = None) -> str:
    if not milestone_display_id or not display_id or not title:
        raise ValueError("task-create requires --milestone, --id, and --title")
    if before and after:
        raise ValueError("task-create accepts only one of --before or --after")
    milestones, tasks = _records(state)
    milestone = next((item for item in milestones if item.get("display_id") == milestone_display_id), None)
    if milestone is None:
        raise ValueError(f"Unknown milestone: {milestone_display_id}")
    siblings = _ordered([item for item in tasks if item.get("milestone_id") == milestone["model_id"]])
    duplicate = next((item for item in siblings if item.get("display_id") == display_id), None)
    if duplicate is not None:
        raise ValueError(
            f"Duplicate task ID: {milestone_display_id}-{display_id}; existing canonical record is "
            f"meta/tasks/{duplicate['model_id']}.json. Next: choose a unique --id or update the existing task with task-set-state."
        )
    placement = "appended"
    if before or after:
        anchor = next((index for index, item in enumerate(siblings) if item.get("display_id") == (before or after)), None)
        if anchor is None:
            raise ValueError(f"Unknown task insertion target: {before or after}")
        index = anchor if before else anchor + 1
        lower = _order(siblings[index - 1]) if index else 0
        upper = _order(siblings[index]) if index < len(siblings) else 10**ORDER_WIDTH - 1
        if upper - lower < 2:
            raise ValueError("Task insertion order space is exhausted at this position; run an explicit ordering normalization")
        order_key = str((lower + upper) // 2).zfill(ORDER_WIDTH)
        placement = f"before {before}" if before else f"after {after}"
    else:
        order_key = _next_order(siblings)
    model_id = str(uuid.uuid4())
    _save_record(state, {"kind": "task", "model_id": model_id, "milestone_id": milestone["model_id"], "display_id": display_id, "title": title, "state": "pending", "order_key": order_key})
    return placement


def _target(state: Path, kind: str, display_id: str, milestone_display_id: str | None) -> dict[str, object]:
    milestones, tasks = _records(state)
    records = milestones if kind == "milestone" else tasks
    milestone_id: str | None = None
    if kind == "task":
        matching_milestones = [item for item in milestones if item.get("display_id") == milestone_display_id]
        if not matching_milestones:
            raise ValueError(f"Unknown milestone: {milestone_display_id}")
        if len(matching_milestones) != 1:
            raise ValueError(f"Ambiguous milestone: {milestone_display_id}")
        milestone = matching_milestones[0]
        milestone_id = _text(milestone, "model_id")
    matching_records = [item for item in records if item.get("display_id") == display_id and (kind == "milestone" or item.get("milestone_id") == milestone_id)]
    if not matching_records:
        raise ValueError(f"Unknown {kind}: {display_id}")
    if len(matching_records) != 1:
        raise ValueError(f"Ambiguous {kind}: {display_id}")
    return matching_records[0]


def set_state(state: Path, kind: str, display_id: str, target_state: str, milestone_display_id: str | None = None) -> None:
    record = _target(state, kind, display_id, milestone_display_id)
    states = MILESTONE_STATES if kind == "milestone" else TASK_STATES
    transitions = ({"pending": {"in_progress", "blocked"}, "in_progress": {"blocked", "ready_to_close"}, "blocked": {"pending", "in_progress"}, "ready_to_close": {"done", "in_progress"}} if kind == "milestone" else {"pending": {"in_progress", "blocked"}, "in_progress": {"blocked", "awaiting_manual", "done"}, "blocked": {"pending", "in_progress"}, "awaiting_manual": {"done", "in_progress"}})
    if target_state not in states or target_state not in transitions.get(record.get("state"), set()):
        raise ValueError(f"Invalid {kind} state transition: {record.get('state')} to {target_state}")
    if kind == "milestone" and target_state in {"ready_to_close", "done"}:
        blockers = milestone_close_blockers(state, display_id)
        if blockers:
            raise ValueError("MILESTONE_CLOSURE_BLOCKED: " + "; ".join(blockers))
    record["state"] = target_state
    _save_record(state, record)


def _closure_scope(tasks: list[dict[str, object]]) -> str:
    scope = [
        {key: task[key] for key in ("model_id", "display_id", "title", "state", "order_key")}
        for task in _ordered(tasks)
        if task["state"] != "archived"
    ]
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def milestone_close_blockers(state: Path, display_id: str) -> list[str]:
    milestone = _target(state, "milestone", display_id, None)
    _, tasks = _records(state)
    active = [task for task in tasks if task["milestone_id"] == milestone["model_id"] and task["state"] != "archived"]
    blockers = [
        f"Unfinished task: {display_id}-{task['display_id']} ({task['state']}); "
        + (f"next: planrunway task-set-state --milestone {display_id} --id {task['display_id']} --state in_progress"
           if task["state"] in {"pending", "blocked"}
           else f"review task evidence and validation: .prway/execution/tasks/{display_id}/{task['display_id']}.md")
        for task in _ordered(active) if task["state"] != "done"
    ]
    if not active:
        blockers.append(f"MISSING: milestone has no deliverable tasks. Next: planrunway task-create --milestone {display_id} --id TASK_ID --title TITLE")
    blockers.extend(milestone_goal_blockers(state, display_id, milestone, active))
    review = milestone.get("closure_review")
    next_review = ("Review .prway/technical/testing_approach.md and make sure whole milestone "
                   "is represented as applicable. "
                   f"Next: planrunway milestone-review --id {display_id} --help")
    guidance = "Missing: closure review against .prway/technical/testing_approach.md. " + next_review
    if not isinstance(review, dict) or review.get("format_version") != 1:
        blockers.append(guidance)
        return blockers
    policy = state / "technical" / "testing_approach.md"
    if not policy.is_file() or not policy.read_bytes().strip():
        blockers.append("Missing or empty .prway/technical/testing_approach.md; review project testing policy")
    elif review.get("testing_approach_sha256") != hashlib.sha256(policy.read_bytes()).hexdigest():
        blockers.append("Stale closure review: testing_approach.md changed. " + next_review)
    if review.get("task_scope_sha256") != _closure_scope(active):
        blockers.append("Stale closure review: milestone task scope changed. " + next_review)
    if review.get("milestone_model_id") != milestone["model_id"] or review.get("whole_milestone") is not True:
        blockers.append("Incomplete closure review: whole-milestone scope is unconfirmed. " + next_review)
    manual = review.get("manual_applicability")
    checks = review.get("manual_checks")
    reason = review.get("reason")
    if (not isinstance(review.get("operator"), str) or not review["operator"].strip()
        or not isinstance(reason, str) or not reason.strip()
        or not isinstance(manual, str) or manual not in {"applicable", "not_applicable"}
        or not isinstance(checks, list) or any(not isinstance(check, str) or not check.strip() for check in checks)
        or (manual == "applicable" and not checks) or (manual == "not_applicable" and checks)):
        blockers.append("Incomplete closure review: manual applicability or rationale is missing. " + next_review)
    return blockers


def _goal_brief(state: Path, display_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", display_id):
        raise ValueError("Milestone display ID is not a safe brief filename")
    return state / "vision" / "milestones" / f"{display_id}.md"


def _goal_scope(state: Path, milestone_id: str, tasks: list[dict[str, object]]) -> str:
    scope = []
    for task in _ordered(tasks):
        if task["state"] == "archived":
            continue
        task_id = _text(task, "display_id")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", task_id):
            raise ValueError("Task display ID is not a safe brief filename")
        brief = state / "execution" / "tasks" / milestone_id / f"{task_id}.md"
        scope.append((task["model_id"], task_id, task["title"], task["order_key"], task.get("delivery_revision", 0),
                      hashlib.sha256(brief.read_bytes()).hexdigest() if brief.is_file() else None))
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def milestone_goal_blockers(state: Path, display_id: str, milestone: dict[str, object] | None = None, active: list[dict[str, object]] | None = None) -> list[str]:
    if milestone is None:
        milestone = _target(state, "milestone", display_id, None)
    if active is None:
        _, tasks = _records(state)
        active = [task for task in tasks if task["milestone_id"] == milestone["model_id"] and task["state"] != "archived"]
    brief = _goal_brief(state, display_id)
    if not brief.is_file() or not brief.read_bytes().strip():
        return [f"MISSING: milestone goal brief .prway/vision/milestones/{display_id}.md; describe goal and acceptance promises before reviewing coverage"]
    next_review = f"Next: planrunway milestone-goal-review --id {display_id} --help"
    review = milestone.get("goal_review")
    if not isinstance(review, dict) or review.get("format_version") != 1:
        return [f"MISSING: milestone goal completeness review. Check whether tasks together fulfill .prway/vision/milestones/{display_id}.md. {next_review}"]
    blockers = []
    if review.get("milestone_model_id") != milestone["model_id"] or review.get("whole_milestone") is not True or not isinstance(review.get("operator"), str) or not review["operator"].strip() or not isinstance(review.get("reason"), str) or not review["reason"].strip():
        blockers.append(f"MISSING: valid whole-milestone goal completeness review. {next_review}")
    if review.get("brief_sha256") != hashlib.sha256(brief.read_bytes()).hexdigest() or review.get("task_scope_sha256") != _goal_scope(state, display_id, active):
        blockers.append(f"MISSING: current milestone goal completeness review; goal or task scope changed. {next_review}")
    uncovered = review.get("uncovered")
    if review.get("coverage") == "missing" and isinstance(uncovered, list) and uncovered and all(isinstance(item, str) and item.strip() for item in uncovered):
        blockers.append(f"MISSING: milestone goal promises: {'; '.join(uncovered)}. Next: planrunway task-create --milestone {display_id} --id TASK_ID --title TITLE; then review coverage again")
    elif review.get("coverage") != "complete" or uncovered != []:
        blockers.append(f"MISSING: valid milestone goal coverage decision. {next_review}")
    return blockers


def record_milestone_goal_review(state: Path, display_id: str, operator: str, reason: str, coverage: str, uncovered: list[str], whole_milestone: bool) -> None:
    milestone = _target(state, "milestone", display_id, None)
    if milestone["state"] in {"done", "archived"}:
        raise ValueError("Reopen milestone before recording goal coverage")
    brief = _goal_brief(state, display_id)
    if not brief.is_file() or not brief.read_bytes().strip():
        raise ValueError(f"MISSING: milestone goal brief .prway/vision/milestones/{display_id}.md")
    if not whole_milestone or not operator.strip() or not reason.strip() or coverage not in {"complete", "missing"} or (coverage == "complete" and uncovered) or (coverage == "missing" and not uncovered) or any(not item.strip() for item in uncovered):
        raise ValueError("Goal review requires --whole-milestone, --operator, --reason and --coverage complete (no gaps) or missing with --uncovered for each gap")
    _, tasks = _records(state)
    active = [task for task in tasks if task["milestone_id"] == milestone["model_id"] and task["state"] != "archived"]
    if coverage == "complete" and (not active or any(task["state"] != "done" for task in active)):
        raise ValueError("Goal coverage can be confirmed only after all deliverable milestone tasks are done")
    milestone["goal_review"] = {
        "format_version": 1, "milestone_model_id": milestone["model_id"], "whole_milestone": True,
        "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
        "task_scope_sha256": _goal_scope(state, display_id, [task for task in tasks if task["milestone_id"] == milestone["model_id"]]),
        "coverage": coverage, "uncovered": uncovered, "operator": operator, "reason": reason,
    }
    _save_record(state, milestone)


def record_milestone_review(state: Path, display_id: str, operator: str, reason: str, manual_applicability: str, manual_checks: list[str], whole_milestone: bool) -> None:
    milestone = _target(state, "milestone", display_id, None)
    if milestone["state"] in {"done", "archived"}:
        raise ValueError("Reopen milestone before recording a new closure review")
    if not whole_milestone or not operator.strip() or not reason.strip():
        raise ValueError("milestone-review requires --whole-milestone, --operator and --reason")
    if manual_applicability not in {"applicable", "not_applicable"} or (manual_applicability == "applicable" and not manual_checks) or (manual_applicability == "not_applicable" and manual_checks) or any(not check.strip() for check in manual_checks):
        raise ValueError("Applicable manual validation requires --manual-check; not_applicable requires a reason and no manual checks")
    policy = state / "technical" / "testing_approach.md"
    if not policy.is_file() or not policy.read_bytes().strip():
        raise ValueError("Missing or empty .prway/technical/testing_approach.md")
    _, tasks = _records(state)
    milestone["closure_review"] = {
        "format_version": 1, "milestone_model_id": milestone["model_id"], "whole_milestone": True,
        "testing_approach_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
        "task_scope_sha256": _closure_scope([task for task in tasks if task["milestone_id"] == milestone["model_id"]]),
        "operator": operator, "reason": reason, "manual_applicability": manual_applicability,
        "manual_checks": manual_checks,
    }
    _save_record(state, milestone)


def change_record(state: Path, kind: str, action: str, display_id: str, milestone_display_id: str | None = None, successor: str | None = None) -> None:
    record = _target(state, kind, display_id, milestone_display_id)
    if action == "reopen":
        if record.get("state") != "done":
            raise ValueError(f"Only done {kind} can reopen")
        record["state"] = "pending"
        if kind == "task":
            record["delivery_revision"] = int(record.get("delivery_revision", 0)) + 1
    else:
        record["state"] = "archived"
        if action == "supersede":
            if not successor:
                raise ValueError("supersede requires --successor")
            record["relation"] = {"type": "superseded_by", "value": successor}
    _save_record(state, record)


def rename_record(state: Path, kind: str, display_id: str, new_display_id: str, milestone_display_id: str | None = None) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", new_display_id):
        raise ValueError("Display ID must start with a letter and contain only letters or digits")
    target = _target(state, kind, display_id, milestone_display_id)
    if target.get("state") == "archived":
        raise ValueError(f"Cannot rename archived {kind}: {display_id}")
    milestones, tasks = _records(state)
    records = milestones if kind == "milestone" else [item for item in tasks if item["milestone_id"] == target["milestone_id"]]
    collision = next((item for item in records if item["model_id"] != target["model_id"] and item.get("display_id") == new_display_id), None)
    if collision is not None:
        raise ValueError(
            f"Cannot rename {kind} to {new_display_id}; canonical record meta/{kind}s/{collision['model_id']}.json already owns that ID"
        )
    target["display_id"] = new_display_id
    _save_record(state, target)


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
