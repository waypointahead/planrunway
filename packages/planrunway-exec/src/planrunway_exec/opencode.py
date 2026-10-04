"""Isolated, short-lived OpenCode process adapter."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .state import invocation_allowed


ROLES = {"proposal", "implementer", "reviewer"}
DEFAULT_PERMISSION_EVENTS = frozenset({"permission.requested", "permission_request"})


@dataclass(frozen=True)
class Invocation:
    role: str
    prompt: str
    worktree: Path
    timeout_seconds: float


@dataclass(frozen=True)
class InvocationResult:
    outcome: str
    exit_code: int | None
    event_types: tuple[str, ...]
    output_sha256: str
    configuration_sha256: str
    proposal_text: str | None = None
    review_verdict: str | None = None
    review_notes: str | None = None


def sanitized_environment(
    environment: Mapping[str, str] | None = None,
    allowed_opencode_variables: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Drop ambient OpenCode state; only policy names can reintroduce it."""
    source = os.environ if environment is None else environment
    return {
        name: value
        for name, value in source.items()
        if not name.startswith("OPENCODE_") or name in allowed_opencode_variables
    }


def _fingerprint(configuration: Mapping[str, object]) -> str:
    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_events(output: str) -> tuple[tuple[str, ...], bool]:
    event_types: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return (), False
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return (), False
        event_types.append(event["type"])
    return tuple(event_types), bool(event_types)


class OpenCodeAdapter:
    """Launch one fresh OpenCode process and reduce its output to candidate evidence."""

    def __init__(
        self,
        executable: str = "opencode",
        environment: Mapping[str, str] | None = None,
        allowed_opencode_variables: frozenset[str] = frozenset(),
        permission_event_types: frozenset[str] = DEFAULT_PERMISSION_EVENTS,
        quota_signals: tuple[str, ...] = (),
        reviewer_prefix: Sequence[str] | None = None,
        model: str | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        kill_process_group: Callable[[int, signal.Signals], None] = os.killpg,
    ) -> None:
        self.executable = executable
        self.environment = sanitized_environment(environment, allowed_opencode_variables)
        self.allowed_opencode_variables = allowed_opencode_variables
        self.permission_event_types = permission_event_types
        self.quota_signals = quota_signals
        self.reviewer_prefix = tuple(reviewer_prefix) if reviewer_prefix is not None else None
        self.model = model
        self.popen_factory = popen_factory
        self.kill_process_group = kill_process_group

    def invoke(self, invocation: Invocation) -> InvocationResult:
        if invocation.role not in ROLES:
            raise ValueError(f"Unknown OpenCode role: {invocation.role}")
        if not invocation.prompt:
            raise ValueError("OpenCode prompt must be non-empty")
        if invocation.timeout_seconds <= 0:
            raise ValueError("OpenCode timeout must be positive")
        if not invocation.worktree.is_dir():
            raise ValueError(f"OpenCode worktree does not exist: {invocation.worktree}")
        if invocation.role == "reviewer" and not self.reviewer_prefix:
            raise ValueError("Reviewer requires an explicit read-only process wrapper")

        configuration = {
            "allowed_opencode_variables": sorted(self.allowed_opencode_variables),
            "executable": self.executable,
            "permission_event_types": sorted(self.permission_event_types),
            "quota_signals": self.quota_signals,
            "reviewer_prefix": self.reviewer_prefix,
            "role": invocation.role,
            "model": self.model,
        }
        command = [self.executable, "run", "--format", "json", "--pure", "--dir", str(invocation.worktree), invocation.prompt]
        if self.model:
            command[-1:-1] = ["--model", self.model]
        if invocation.role == "reviewer":
            command = [*self.reviewer_prefix, *command]
        try:
            process = self.popen_factory(
                command,
                cwd=invocation.worktree,
                env=self.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            return self._result("failed", None, (), str(error), configuration)
        try:
            stdout, stderr = process.communicate(timeout=invocation.timeout_seconds)
        except subprocess.TimeoutExpired:
            self.kill_process_group(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill_process_group(process.pid, signal.SIGKILL)
                process.communicate()
            return self._result("timeout", None, (), "", configuration)

        event_types, valid = _parse_events(stdout)
        combined = f"{stdout}\n{stderr}"
        if any(signal_text in combined for signal_text in self.quota_signals):
            return self._result("quota", process.returncode, event_types if valid else (), combined, configuration)
        if not valid:
            return self._result("malformed_output", process.returncode, (), combined, configuration)
        if any(event_type in self.permission_event_types for event_type in event_types):
            return self._result("permission_requested", process.returncode, event_types, combined, configuration)
        result = self._result("completed" if process.returncode == 0 else "failed", process.returncode, event_types, combined, configuration)
        if invocation.role in {"proposal", "reviewer"} and result.outcome == "completed":
            text = []
            for line in stdout.splitlines():
                event = json.loads(line)
                part = event.get("part")
                if event.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str):
                    text.append(part["text"])
            proposal = "\n".join(text).strip()
            if invocation.role == "proposal" and proposal and len(proposal) <= 4096:
                return InvocationResult(result.outcome, result.exit_code, result.event_types, result.output_sha256, result.configuration_sha256, proposal)
            if invocation.role == "reviewer" and proposal == "APPROVED":
                return InvocationResult(result.outcome, result.exit_code, result.event_types, result.output_sha256, result.configuration_sha256, None, "APPROVED")
            if invocation.role == "reviewer" and proposal.startswith("CHANGES_REQUESTED:") and 18 < len(proposal) <= 4096:
                return InvocationResult(result.outcome, result.exit_code, result.event_types, result.output_sha256, result.configuration_sha256, None, "CHANGES_REQUESTED", proposal.split(":", 1)[1].strip())
        return result

    def invoke_for_run(self, root: Path, run_id: str, invocation: Invocation, mode: str | None) -> InvocationResult:
        invocation_allowed(root, run_id, invocation.role, mode)
        return self.invoke(invocation)

    @staticmethod
    def _result(
        outcome: str,
        exit_code: int | None,
        event_types: tuple[str, ...],
        output: str,
        configuration: Mapping[str, object],
    ) -> InvocationResult:
        return InvocationResult(
            outcome=outcome,
            exit_code=exit_code,
            event_types=event_types,
            output_sha256=hashlib.sha256(output.encode()).hexdigest(),
            configuration_sha256=_fingerprint(configuration),
        )
