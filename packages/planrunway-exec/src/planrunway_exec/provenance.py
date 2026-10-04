"""Bounded execution provenance for later Core drift analysis."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .controller import CompletionClaim, changed_paths
from .state import run_record


FORMAT_VERSION = 1


@dataclass(frozen=True)
class ProvenanceContribution:
    status: str
    context: dict[str, object] | None


def commit_trailers(root: Path, run_id: str) -> str:
    record = run_record(root, run_id)
    task_id = str(record["task_id"])
    milestone_id = task_id.split("-", 1)[0]
    return f"PlanRunway-Task: {task_id}\nPlanRunway-Milestone: {milestone_id}\nPlanRunway-Run: {run_id}\n"


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


def record_provenance(root: Path, run_id: str, worktree: Path, claim: CompletionClaim | None = None) -> ProvenanceContribution:
    """Persist actual Git scope only; never retain prompts, transcripts, or command output."""
    record = run_record(root, run_id)
    if record["state"] == "suspended_quota":
        return ProvenanceContribution("suspended", None)
    repository = record.get("repository")
    if not isinstance(repository, dict) or repository.get("kind") != "git" or not isinstance(repository.get("base_revision"), str):
        return ProvenanceContribution("unavailable", None)
    try:
        changed = changed_paths(worktree, repository["base_revision"])
        context: dict[str, object] = {
            "format_version": FORMAT_VERSION,
            "run_id": run_id,
            "task_id": record["task_id"],
            "milestone_id": str(record["task_id"]).split("-", 1)[0],
            "task_revision": record["task_revision"],
            "base_revision": repository["base_revision"],
            "changed_files": list(changed),
            "trailers": commit_trailers(root, run_id).splitlines(),
        }
        if claim is not None:
            context["completion_claim"] = asdict(claim)
            context["evidence_summary"] = {
                "criteria_satisfied": len(claim.criterion_results),
                "checks_passed": sum(check.passed for check in claim.checks),
                "checks_total": len(claim.checks),
            }
        encoded = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        context["provenance_sha256"] = hashlib.sha256(encoded).hexdigest()
        _atomic_json(root / ".prway" / "execution" / "runs" / run_id / "provenance.json", context)
        return ProvenanceContribution("available", context)
    except (OSError, ValueError):
        return ProvenanceContribution("unavailable", None)
