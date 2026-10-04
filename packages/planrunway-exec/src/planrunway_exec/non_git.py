"""Backup-protected isolated execution for an explicitly non-Git repository."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .controller import paths_allowed
from .worktree import worktree_path


def backup_path(root: Path, run_id: str) -> Path:
    return root / ".prway" / "execution" / "runtime" / "local" / "backups" / run_id


def _ignore(directory: str, entries: list[str]) -> set[str]:
    path = Path(directory)
    return {"local"} if path.name == "runtime" and "local" in entries else set()


def _files(root: Path) -> dict[str, str]:
    result = {}
    for directory, subdirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        if parent.relative_to(root).parts == (".prway", "execution", "runtime"):
            subdirs[:] = [name for name in subdirs if name != "local"]
        for name in subdirs + files:
            path = parent / name
            if path.is_symlink():
                raise ValueError(f"Non-Git isolation refuses symlink: {path.relative_to(root)}")
            if path.is_file() and path.relative_to(root).parts[:3] != (".prway", "execution", "runtime"):
                result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def backup_and_worktree(root: Path, run_id: str) -> Path:
    # Refuse symlinks before any copy can follow one outside the project.
    _files(root)
    backup = backup_path(root, run_id)
    target = worktree_path(root, run_id)
    if backup.exists() or target.exists():
        raise ValueError("Non-Git backup or worktree already exists")
    backup.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(root, backup, ignore=_ignore, symlinks=True)
        shutil.copytree(backup, target, symlinks=True)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        raise
    return target


def changes(worktree: Path, backup: Path) -> tuple[str, ...]:
    old, new = _files(backup), _files(worktree)
    return tuple(sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path)))


def snapshot(worktree: Path, backup: Path) -> str:
    entries = _files(worktree)
    changed = changes(worktree, backup)
    digest = hashlib.sha256()
    for path in changed:
        digest.update(f"{path}\0{entries.get(path, '<deleted>')}\n".encode())
    return digest.hexdigest()


def integrate(root: Path, run_id: str, allowlist: list[str], reviewed_snapshot: str) -> tuple[str, tuple[str, ...]]:
    backup = backup_path(root, run_id)
    worktree = worktree_path(root, run_id)
    if not backup.is_dir() or not worktree.is_dir():
        return "INTEGRATION_BLOCKED_MAIN_DRIFT", ()
    try:
        if _files(root) != _files(backup):
            return "INTEGRATION_BLOCKED_MAIN_DRIFT", ()
        paths = changes(worktree, backup)
        if not paths_allowed(paths, allowlist) or snapshot(worktree, backup) != reviewed_snapshot:
            return "INTEGRATION_BLOCKED_MAIN_DRIFT", paths
        # Preflight all paths before first write. The retained backup permits deliberate rollback.
        for relative in paths:
            source, target = worktree / relative, root / relative
            if source.is_symlink() or target.is_symlink() or (source.exists() and not source.is_file()) or (target.exists() and not target.is_file()):
                return "INTEGRATION_BLOCKED_MAIN_DRIFT", paths
        applied = []
        created_directories: list[Path] = []
        try:
            for relative in paths:
                source, target = worktree / relative, root / relative
                if source.exists():
                    missing = []
                    parent = target.parent
                    while parent != root and not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created_directories.extend(reversed(missing))
                    descriptor, temp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
                    try:
                        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
                            shutil.copyfileobj(input_file, output_file)
                            output_file.flush()
                            os.fsync(output_file.fileno())
                        os.replace(temp, target)
                    finally:
                        Path(temp).unlink(missing_ok=True)
                else:
                    target.unlink()
                applied.append(relative)
        except OSError:
            for relative in reversed(applied):
                original, target = backup / relative, root / relative
                if original.exists():
                    shutil.copy2(original, target)
                else:
                    target.unlink(missing_ok=True)
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            return "INTEGRATION_BLOCKED_MAIN_DRIFT", paths
        shutil.rmtree(worktree)
        return "integrated_non_git_backup_retained", paths
    except (OSError, ValueError):
        return "INTEGRATION_BLOCKED_MAIN_DRIFT", ()
