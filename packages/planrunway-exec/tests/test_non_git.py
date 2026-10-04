from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from planrunway_exec import non_git


def test_copy_failure_rolls_back_all_applied_non_git_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    state = root / ".prway" / "execution" / "runtime" / "local"
    state.mkdir(parents=True)
    (root / "allowed.txt").write_text("before\n", encoding="utf-8")
    worktree = non_git.backup_and_worktree(root, "run-1")
    (worktree / "allowed.txt").write_text("after\n", encoding="utf-8")
    (worktree / "other.txt").write_text("new\n", encoding="utf-8")
    original_copy = non_git.shutil.copyfileobj
    calls = 0

    def interrupted(source: object, destination: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-file failure")
        original_copy(source, destination)

    monkeypatch.setattr(non_git.shutil, "copyfileobj", interrupted)
    outcome, _ = non_git.integrate(root, "run-1", ["allowed.txt", "other.txt"], non_git.snapshot(worktree, non_git.backup_path(root, "run-1")))
    assert outcome == "INTEGRATION_BLOCKED_MAIN_DRIFT"
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "before\n"
    assert not (root / "other.txt").exists()
    assert non_git.backup_path(root, "run-1").is_dir()
