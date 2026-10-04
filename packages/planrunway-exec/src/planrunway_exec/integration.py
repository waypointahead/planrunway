"""Conservative verified-patch integration for an isolated Exec worktree."""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .controller import changed_paths
from .state import run_record
from .worktree import worktree_path


INTEGRATION_BLOCKED_MAIN_DRIFT = "INTEGRATION_BLOCKED_MAIN_DRIFT"


@dataclass(frozen=True)
class IntegrationResult:
    outcome: str
    changed_paths: tuple[str, ...]


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], cwd=root, text=True, capture_output=True, check=False)


def _main_is_clean(root: Path) -> bool:
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode:
        return False
    # Compact Exec ledger and local worktrees are operational metadata, not product or semantic-plan drift.
    paths = (line[3:] for line in status.stdout.splitlines() if len(line) >= 4)
    return all(path.startswith(".prway/execution/runtime/") for path in paths)


def _allowed(paths: tuple[str, ...], allowlist: list[object]) -> bool:
    return all(isinstance(pattern, str) and pattern for pattern in allowlist) and all(
        any(fnmatch.fnmatchcase(path, pattern) for pattern in allowlist if isinstance(pattern, str))
        for path in paths
    )


def _untracked_paths(root: Path) -> tuple[str, ...]:
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode:
        raise ValueError(f"Unable to inspect worktree: {status.stderr.strip()}")
    return tuple(line[3:] for line in status.stdout.splitlines() if line.startswith("?? "))


def _remove_empty_directories(directories: list[Path]) -> None:
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass


def _copy_untracked(source_root: Path, target_root: Path, paths: tuple[str, ...]) -> list[Path]:
    written: list[Path] = []
    created_directories: list[Path] = []
    try:
        for relative in paths:
            raw_source = source_root / relative
            source = raw_source.resolve()
            target = (target_root / relative).resolve()
            if raw_source.is_symlink() or source_root not in source.parents or target_root not in target.parents or source.is_dir() or target.exists():
                raise ValueError("Unsafe untracked integration path")
            missing = []
            parent = target.parent
            while parent != target_root and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            created_directories.extend(reversed(missing))
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            try:
                with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
                    shutil.copyfileobj(source_file, target_file)
                    target_file.flush()
                    os.fsync(target_file.fileno())
                os.replace(temporary, target)
                written.append(target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
    except BaseException:
        for path in reversed(written):
            path.unlink(missing_ok=True)
        _remove_empty_directories(created_directories)
        raise
    return created_directories


def cleanup_worktree(root: Path, run_id: str, worktree: Path) -> None:
    """Remove only controller-owned worktree paths after integration or invalidation."""
    if worktree != worktree_path(root, run_id):
        raise ValueError("Refusing to remove a non-controller worktree")
    removed = _git(root, "worktree", "remove", "--force", str(worktree))
    if removed.returncode and worktree.exists():
        raise ValueError(f"Unable to remove execution worktree: {removed.stderr.strip()}")


def integrate(root: Path, run_id: str, worktree: Path) -> IntegrationResult:
    """Apply whole verified diff without rebasing, filtering, or creating a commit."""
    record = run_record(root, run_id)
    repository = record.get("repository")
    if not isinstance(repository, dict) or repository.get("kind") != "git":
        return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, ())
    base_revision = repository.get("base_revision")
    allowlist = record.get("allowlist")
    if not isinstance(base_revision, str) or not isinstance(allowlist, list):
        return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, ())
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode or head.stdout.strip() != base_revision or not _main_is_clean(root):
        return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, ())
    try:
        paths = changed_paths(worktree, base_revision)
        if not _allowed(paths, allowlist):
            return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, paths)
        patch = _git(worktree, "diff", "--binary", base_revision, "--")
        if patch.returncode:
            return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, paths)
        checked = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn"], cwd=root, input=patch.stdout, text=True, capture_output=True, check=False)
        if checked.returncode:
            return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, paths)
        untracked = _untracked_paths(worktree)
        created_directories = _copy_untracked(worktree, root, untracked)
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn"], cwd=root, input=patch.stdout, text=True, capture_output=True, check=False)
        if applied.returncode:
            for relative in untracked:
                (root / relative).unlink(missing_ok=True)
            _remove_empty_directories(created_directories)
            return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, paths)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return IntegrationResult(INTEGRATION_BLOCKED_MAIN_DRIFT, ())
    try:
        cleanup_worktree(root, run_id, worktree)
    except (OSError, ValueError):
        return IntegrationResult("integrated_cleanup_required", paths)
    return IntegrationResult("integrated", paths)
