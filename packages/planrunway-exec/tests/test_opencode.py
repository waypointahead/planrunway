from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PACKAGE))

from planrunway_exec.opencode import Invocation, OpenCodeAdapter, sanitized_environment
from planrunway_exec.state import accept_tests, create_run, propose_tests


class Process:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0, timeout: bool = False) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.pid = 123
        self.calls = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.calls += 1
        if self.timeout and self.calls == 1:
            raise subprocess.TimeoutExpired("opencode", timeout)
        return self.stdout, self.stderr


def factory(process: Process, captured: dict[str, object]):
    def create(*args: object, **kwargs: object) -> Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process
    return create


def invocation(tmp_path: Path, role: str = "implementer") -> Invocation:
    return Invocation(role=role, prompt="work", worktree=tmp_path, timeout_seconds=1)


def test_environment_drops_ambient_opencode_values() -> None:
    result = sanitized_environment({"PATH": "/bin", "OPENCODE_SERVER_PASSWORD": "secret", "OPENCODE_MODEL": "x"}, frozenset({"OPENCODE_MODEL"}))
    assert result == {"PATH": "/bin", "OPENCODE_MODEL": "x"}


def test_adapter_uses_fresh_pure_json_process_and_keeps_only_evidence(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    adapter = OpenCodeAdapter(environment={"PATH": "/bin", "OPENCODE_SERVER_PASSWORD": "secret"}, popen_factory=factory(Process('{"type":"message.completed"}\n'), captured))

    result = adapter.invoke(invocation(tmp_path))

    assert result.outcome == "completed"
    assert result.event_types == ("message.completed",)
    assert captured["args"] == (["opencode", "run", "--format", "json", "--pure", "--dir", str(tmp_path), "work"],)
    assert captured["kwargs"] == {"cwd": tmp_path, "env": {"PATH": "/bin"}, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "start_new_session": True}


def test_adapter_pins_model_and_ignores_inherited_opencode_configuration(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    adapter = OpenCodeAdapter(model="provider/model", environment={"PATH": "/bin", "XDG_CONFIG_HOME": "/isolated/config", "OPENCODE_CONFIG": "/private/config"}, popen_factory=factory(Process('{"type":"step_finish"}\n'), captured))
    assert adapter.invoke(invocation(tmp_path)).outcome == "completed"
    assert captured["args"] == (["opencode", "run", "--format", "json", "--pure", "--dir", str(tmp_path), "--model", "provider/model", "work"],)
    assert captured["kwargs"]["env"] == {"PATH": "/bin", "XDG_CONFIG_HOME": "/isolated/config"}


@pytest.mark.parametrize(("stdout", "outcome"), [("not json\n", "malformed_output"), ('{"type":"permission.requested"}\n', "permission_requested")])
def test_adapter_returns_controlled_structured_failures(tmp_path: Path, stdout: str, outcome: str) -> None:
    adapter = OpenCodeAdapter(popen_factory=factory(Process(stdout), {}))
    assert adapter.invoke(invocation(tmp_path)).outcome == outcome


def test_adapter_normalizes_only_configured_quota_signal(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter(quota_signals=("SUBSCRIPTION_LIMIT",), popen_factory=factory(Process('{"type":"error"}\n', "SUBSCRIPTION_LIMIT"), {}))
    assert adapter.invoke(invocation(tmp_path)).outcome == "quota"


def test_timeout_kills_entire_process_group(tmp_path: Path) -> None:
    killed: list[tuple[int, object]] = []
    adapter = OpenCodeAdapter(popen_factory=factory(Process("", timeout=True), {}), kill_process_group=lambda pid, sig: killed.append((pid, sig)))
    assert adapter.invoke(invocation(tmp_path)).outcome == "timeout"
    assert killed == [(123, signal.SIGTERM)]


def test_reviewer_requires_explicit_read_only_wrapper(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter(popen_factory=factory(Process('{"type":"message.completed"}\n'), {}))
    with pytest.raises(ValueError, match="read-only"):
        adapter.invoke(invocation(tmp_path, "reviewer"))

    reviewed = OpenCodeAdapter(reviewer_prefix=("sandbox",), popen_factory=factory(Process('{"type":"message.completed"}\n'), {}))
    assert reviewed.invoke(invocation(tmp_path, "reviewer")).outcome == "completed"


def test_implementer_requires_retained_sdd_approval(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    request = {
        "run_id": "run-1",
        "task_id": "MS4-T02a",
        "task_revision": "revision-1",
        "machine_id": "host-a",
        "allowlist": ["packages/planrunway-exec/**"],
        "repository": {"kind": "git", "base_revision": "abc123"},
    }
    create_run(root, request, "build")
    adapter = OpenCodeAdapter(popen_factory=factory(Process('{"type":"message.completed"}\n'), {}))
    with pytest.raises(ValueError, match="not expected"):
        adapter.invoke_for_run(root, "run-1", invocation(tmp_path), "build")

    propose_tests(root, "run-1", "proposal-1", "build")
    accept_tests(root, "run-1", "proposal-1", "revision-1", "build")
    assert adapter.invoke_for_run(root, "run-1", invocation(tmp_path), "build").outcome == "completed"
