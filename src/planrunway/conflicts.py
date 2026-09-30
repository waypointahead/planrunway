"""Detect and deliberately resolve only Git conflicts with an intact side to restore."""
from __future__ import annotations

import os
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


CONFLICT_MARKERS = (b"<<<<<<<", b"=======", b">>>>>>>")
CANONICAL_GLOBS = (
    "meta/milestones/*.json",
    "meta/tasks/*.json",
    "execution/tasks/*.json",
    "execution/runs/*/final_run.json",
)


@dataclass(frozen=True)
class Conflict:
    path: Path
    kind: str
    git_unmerged: bool


def _canonical_paths(state: Path) -> list[Path]:
    return sorted({path for pattern in CANONICAL_GLOBS for path in state.glob(pattern)})


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def detect(root: Path) -> list[Conflict]:
    state = root / ".prway"
    unmerged = {
        root / line.decode("utf-8")
        for line in _git(root, "diff", "--name-only", "--diff-filter=U", "--", ".prway").stdout.splitlines()
    }
    paths = set(_canonical_paths(state)) | unmerged
    conflicts: list[Conflict] = []
    for path in sorted(paths):
        marker = path.is_file() and any(token in path.read_bytes() for token in CONFLICT_MARKERS)
        if marker or path in unmerged:
            if "/meta/" in f"/{path.relative_to(root).as_posix()}":
                kind = "planning entity"
            elif "/execution/tasks/" in f"/{path.relative_to(root).as_posix()}":
                kind = "evidence entity"
            else:
                kind = "immutable final evidence"
            conflicts.append(Conflict(path=path, kind=kind, git_unmerged=path in unmerged))
    if conflicts:
        return conflicts
    seen_display_ids: dict[tuple[str, str], Path] = {}
    seen_runs: dict[str, Path] = {}
    for path in _canonical_paths(state):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            conflicts.append(Conflict(path=path, kind="malformed canonical record", git_unmerged=False))
            continue
        if not isinstance(record, dict):
            conflicts.append(Conflict(path=path, kind="malformed canonical record", git_unmerged=False))
            continue
        if path.parent.name in {"milestones", "tasks"} and "meta" in path.parts:
            scope = "milestone" if path.parent.name == "milestones" else str(record.get("milestone_id", ""))
            display_id = record.get("display_id")
            if isinstance(display_id, str) and record.get("state") != "archived":
                key = (scope, display_id)
                if key in seen_display_ids:
                    conflicts.append(Conflict(path=path, kind="duplicate display identity", git_unmerged=False))
                else:
                    seen_display_ids[key] = path
        if path.parent.name == "tasks" and "execution" in path.parts:
            run_id = record.get("final_run")
            if isinstance(run_id, str):
                if run_id in seen_runs:
                    conflicts.append(Conflict(path=path, kind="duplicate final-run identity", git_unmerged=False))
                else:
                    seen_runs[run_id] = path
    return conflicts


def human_intervention_message(root: Path, conflicts: list[Conflict]) -> str:
    count = len(conflicts)
    noun = "conflict" if count == 1 else "conflicts"
    first = conflicts[0].path.relative_to(root).as_posix()
    return (
        f"HUMAN_INTERVENTION_REQUIRED: {count} blocking PlanRunway {noun}. "
        f"First affected file: {first}. No planning changes, status, or render can continue. "
        "Next: run planrunway conflict-resolve --root ."
    )


def _manual_recovery(conflict: Conflict) -> str:
    if conflict.kind == "duplicate display identity":
        return "Choose which entity keeps display ID, then rename or archive other entity through PlanRunway after Git state is clean."
    if conflict.kind == "duplicate final-run identity":
        return "Choose one final run to retain; create new evidence with a new run ID for other task, or reopen it."
    if conflict.kind == "malformed canonical record":
        return "Restore valid record from a known Git revision or repair JSON from reviewed source; do not guess missing evidence."
    if conflict.kind == "planning entity":
        return "Restore valid planning entity from a known Git revision or choose one reviewed version, then run planrunway lint."
    if conflict.kind == "evidence entity":
        return "Restore valid evidence entity from a known Git revision; do not fabricate final evidence or acceptance results."
    return "Resolve file deliberately with Git or restore it from a reviewed backup, then run planrunway lint."


def describe(root: Path, conflicts: list[Conflict]) -> str:
    lines = [
        "[planrunway] Conflict resolution required",
        "",
        f"Blocking items: {len(conflicts)}",
        "PlanRunway has made no changes. Review each item before selecting a strategy.",
    ]
    for index, conflict in enumerate(conflicts, start=1):
        relative = conflict.path.relative_to(root).as_posix()
        lines.extend(("", f"{index}. {conflict.kind}", f"   File: {relative}"))
        if conflict.git_unmerged:
            lines.extend((
                "   A human must choose which product decision survives.",
                "   Options:",
                "   - ours: keep version from branch checked out when merge started.",
                "   - theirs: keep version from branch being merged or rebased.",
                "   - cancel: leave Git conflict and files unchanged.",
            ))
        else:
            options = {
                "duplicate display identity": "rename: assign unique --rename DISPLAY_ID; archive: archive this duplicate entity.",
                "duplicate final-run identity": "reopen: clear this task's final run and return it to in_progress.",
                "malformed canonical record": "restore: copy validated JSON from --restore GIT_REVISION.",
            }
            lines.extend((
                "   Automatic Git-side restore is unavailable; a product decision is still required.",
                f"   Next: {_manual_recovery(conflict)}",
            ))
            if conflict.kind in options:
                lines.extend(("   Guided repair options:", f"   - {options[conflict.kind]}", "   - cancel: make no change."))
    lines.extend((
        "",
        "Batch safety:",
        "- Use --strategy ours or --strategy theirs only after choosing product decision for every item.",
        "- For more than one Git conflict, --all is required. It applies same chosen strategy to all listed Git conflicts.",
        "- After a successful resolution, choose whether files are staged. PlanRunway never creates a commit.",
    ))
    return "\n".join(lines) + "\n"


def _load_record(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"CANNOT_SAFELY_RESOLVE: canonical record is not an object: {path}")
    return value


def _stage(root: Path, paths: list[Path]) -> None:
    result = _git(root, "add", "--", *(path.relative_to(root).as_posix() for path in paths))
    if result.returncode:
        raise ValueError(f"Unable to stage resolved files: {result.stderr.decode('utf-8', errors='replace').strip()}")


def resolve_semantic(root: Path, conflict: Conflict, action: str, value: str | None, stage: bool) -> list[Path]:
    if conflict.git_unmerged:
        raise ValueError("Use ours or theirs for unresolved Git conflict")
    record = _load_record(conflict.path)
    if action == "rename" and conflict.kind == "duplicate display identity":
        if not value:
            raise ValueError("rename requires --rename DISPLAY_ID")
        record["display_id"] = value
    elif action == "archive" and conflict.kind == "duplicate display identity":
        record["state"] = "archived"
    elif action == "reopen" and conflict.kind == "duplicate final-run identity":
        record["historical_run"] = record.get("final_run")
        record["final_run"] = None
        record["state"] = "in_progress"
    else:
        raise ValueError(f"CANNOT_SAFELY_RESOLVE: {action} is not safe for {conflict.kind}")
    _atomic_bytes(conflict.path, (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    if stage:
        _stage(root, [conflict.path])
    return [conflict.path]


def restore_from_revision(root: Path, conflict: Conflict, revision: str, stage: bool) -> list[Path]:
    if conflict.kind != "malformed canonical record":
        raise ValueError("restore is available only for malformed canonical record")
    relative = conflict.path.relative_to(root).as_posix()
    source = _git(root, "show", f"{revision}:{relative}")
    if source.returncode:
        raise ValueError(f"CANNOT_SAFELY_RESOLVE: {revision} has no readable version of {relative}")
    try:
        parsed = json.loads(source.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"CANNOT_SAFELY_RESOLVE: {revision} version is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"CANNOT_SAFELY_RESOLVE: {revision} version is not a record object")
    _atomic_bytes(conflict.path, source.stdout)
    if stage:
        _stage(root, [conflict.path])
    return [conflict.path]


def _atomic_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def resolve(root: Path, conflicts: list[Conflict], strategy: str, stage: bool) -> list[Path]:
    if strategy not in {"ours", "theirs"}:
        raise ValueError("conflict-resolve strategy must be ours or theirs")
    unsafe = [conflict for conflict in conflicts if not conflict.git_unmerged]
    if unsafe:
        raise ValueError("CANNOT_SAFELY_RESOLVE: at least one conflict has no intact Git side")
    resolved: list[Path] = []
    originals: dict[Path, bytes] = {}
    stage_index = "2" if strategy == "ours" else "3"
    try:
        for conflict in conflicts:
            relative = conflict.path.relative_to(root).as_posix()
            source = _git(root, "show", f":{stage_index}:{relative}")
            if source.returncode:
                raise ValueError(f"CANNOT_SAFELY_RESOLVE: Git has no {strategy} version for {relative}")
            originals[conflict.path] = conflict.path.read_bytes() if conflict.path.exists() else b""
            _atomic_bytes(conflict.path, source.stdout)
            resolved.append(conflict.path)
        if stage:
            result = _git(root, "add", "--", *(path.relative_to(root).as_posix() for path in resolved))
            if result.returncode:
                raise ValueError(f"Unable to stage resolved files: {result.stderr.decode('utf-8', errors='replace').strip()}")
    except BaseException:
        for path, content in originals.items():
            _atomic_bytes(path, content)
        raise
    return resolved
