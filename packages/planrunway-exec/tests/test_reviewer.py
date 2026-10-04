from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from planrunway_exec import reviewer


def test_readonly_wrapper_refuses_tmp_worktree_before_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/bwrap")
    with pytest.raises(ValueError, match="outside /tmp"):
        reviewer.reviewer_command(["opencode", "run", "--dir", "/tmp/project/worktree"], Path.home())


def test_readonly_wrapper_refuses_worktree_inside_writable_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/bwrap")
    with pytest.raises(ValueError, match="outside writable OpenCode"):
        reviewer.reviewer_command(["opencode", "run", "--dir", "/home/operator/.cache/opencode/project"], Path("/home/operator"))


def test_readonly_wrapper_mounts_project_ro_and_restricts_writes_to_opencode_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self).endswith("opencode"))
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    command = reviewer.reviewer_command(["opencode", "run", "--dir", "/home/operator/project/.prway/execution/runtime/local/worktrees/run"], Path("/home/operator"))
    assert command[:5] == ["bwrap", "--die-with-parent", "--ro-bind", "/", "/"]
    assert "--tmpfs" in command and "/tmp" in command
    assert "--bind" in command
    assert command[-4:] == ["opencode", "run", "--dir", "/home/operator/project/.prway/execution/runtime/local/worktrees/run"]
