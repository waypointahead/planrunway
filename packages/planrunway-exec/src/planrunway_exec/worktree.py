"""Controller-owned isolated Git worktrees for active Exec runs."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .state import _require_build, run_record


def worktree_path(root: Path, run_id: str) -> Path:
    return root / ".prway" / "execution" / "runtime" / "local" / "worktrees" / run_id


def create_worktree(root: Path, run_id: str, mode: str | None) -> Path:
    """Materialize an active Git run at its recorded base before any agent starts."""
    _require_build(mode)
    record = run_record(root, run_id)
    if record["state"] != "proposing_tests":
        raise ValueError("Worktree creation is not expected in current execution state")
    repository = record["repository"]
    if not isinstance(repository, dict) or repository.get("kind") != "git":
        raise ValueError("Isolated worktree requires a Git execution request")
    base_revision = repository.get("base_revision")
    if not isinstance(base_revision, str) or not base_revision:
        raise ValueError("Git execution request is missing base_revision")
    target = worktree_path(root, run_id)
    if target.exists():
        raise ValueError(f"Execution worktree already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    verified = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base_revision}^{{commit}}"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if verified.returncode:
        raise ValueError(f"Execution base revision is unavailable: {base_revision}")
    created = subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), base_revision],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if created.returncode:
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError(f"Unable to create execution worktree: {created.stderr.strip()}")
    return target


def restore_missing_worktree(root: Path, run_id: str, mode: str | None) -> Path:
    """Cross-machine restart from synchronized base, never from guessed local changes."""
    _require_build(mode)
    record = run_record(root, run_id)
    repository = record["repository"]
    target = worktree_path(root, run_id)
    if record["state"] not in {"implementing", "proposing_tests"} or not isinstance(repository, dict) or repository.get("kind") != "git" or target.exists():
        raise ValueError("Cross-machine restart requires a missing Git worktree and restartable state")
    subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True, check=True)
    result = subprocess.run(["git", "worktree", "add", "--detach", str(target), str(repository["base_revision"])], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"Unable to restore execution worktree: {result.stderr.strip()}")
    return target
