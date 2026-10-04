"""Deterministic single-agent execution flow before main-worktree integration."""
from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .opencode import Invocation, InvocationResult, OpenCodeAdapter
from .state import accept_tests, invocation_allowed, propose_tests, record_attempt, run_record, transition


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    fast: bool = False
    timeout_seconds: float = 300


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    output_sha256: str


@dataclass(frozen=True)
class CompletionClaim:
    run_id: str
    task_id: str
    task_revision: str
    accepted_proposal_id: str
    criterion_results: tuple[tuple[str, bool], ...]
    checks: tuple[CheckResult, ...]
    changed_paths: tuple[str, ...]
    diff_sha256: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"Git command failed: {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout


def changed_paths(worktree: Path, base_revision: str) -> tuple[str, ...]:
    output = _git(worktree, "diff", "--name-only", base_revision, "--")
    paths = {path for path in output.splitlines() if path}
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    for line in status.splitlines():
        if line.startswith("?? "):
            paths.add(line[3:])
    return tuple(sorted(paths))


def paths_allowed(paths: Sequence[str], allowlist: Sequence[str]) -> bool:
    return all(any(fnmatch.fnmatchcase(path, pattern) for pattern in allowlist) for path in paths)


def run_checks(worktree: Path, checks: Sequence[Check], fast: bool) -> tuple[CheckResult, ...]:
    selected = [check for check in checks if check.fast == fast]
    results: list[CheckResult] = []
    for check in selected:
        if not check.name or not check.command or check.timeout_seconds <= 0:
            raise ValueError("Checks require a name, command, and positive timeout")
        try:
            result = subprocess.run(check.command, cwd=worktree, text=True, capture_output=True, timeout=check.timeout_seconds, check=False)
            output = f"{result.stdout}\n{result.stderr}"
            results.append(CheckResult(check.name, result.returncode == 0, _digest(output)))
        except subprocess.TimeoutExpired as error:
            output = f"{error.stdout or ''}\n{error.stderr or ''}"
            results.append(CheckResult(check.name, False, _digest(output)))
    return tuple(results)


class SingleAgentController:
    """Coordinates isolated agent calls; T03 owns applying its verified diff."""

    def __init__(self, root: Path, run_id: str, worktree: Path, adapter: OpenCodeAdapter) -> None:
        self.root = root
        self.run_id = run_id
        self.worktree = worktree
        self.adapter = adapter

    def propose(self, prompt: str, timeout_seconds: float, mode: str | None) -> InvocationResult:
        return self.adapter.invoke_for_run(self.root, self.run_id, Invocation("proposal", prompt, self.worktree, timeout_seconds), mode)

    def accept_proposal(self, proposal_id: str, task_revision: str, mode: str | None) -> None:
        accept_tests(self.root, self.run_id, proposal_id, task_revision, mode)

    def retain_proposal(self, proposal_id: str, mode: str | None) -> None:
        propose_tests(self.root, self.run_id, proposal_id, mode)

    def implement(self, prompt: str, timeout_seconds: float, mode: str | None) -> InvocationResult:
        result = self.adapter.invoke_for_run(self.root, self.run_id, Invocation("implementer", prompt, self.worktree, timeout_seconds), mode)
        if result.outcome == "completed":
            transition(self.root, self.run_id, "testing", mode)
        else:
            self._record_agent_failure(result, mode)
        return result

    def fast_checks(self, checks: Sequence[Check], mode: str | None) -> tuple[CheckResult, ...]:
        record = run_record(self.root, self.run_id)
        if record["state"] != "testing":
            raise ValueError("Fast checks are not expected in current execution state")
        results = run_checks(self.worktree, checks, fast=True)
        if not all(result.passed for result in results):
            self._record_check_failure(results, mode)
        else:
            # Reviewer state is reached only after deterministic fast checks pass.
            transition(self.root, self.run_id, "reviewing", mode)
        return results

    def review(self, prompt: str, timeout_seconds: float, mode: str | None) -> InvocationResult:
        result = self.adapter.invoke_for_run(self.root, self.run_id, Invocation("reviewer", prompt, self.worktree, timeout_seconds), mode)
        if result.outcome != "completed":
            self._record_agent_failure(result, mode)
        return result

    def completion_claim(self, criteria: Sequence[str], checks: Sequence[Check], mode: str | None) -> CompletionClaim:
        record = run_record(self.root, self.run_id)
        if record["state"] != "reviewing":
            raise ValueError("Completion claim requires a successful reviewer stage")
        full_results = run_checks(self.worktree, checks, fast=False)
        if not all(result.passed for result in full_results):
            self._record_check_failure(full_results, mode)
            raise ValueError("Required checks failed")
        repository = record["repository"]
        if not isinstance(repository, dict):
            raise ValueError("Completion claim requires a repository snapshot")
        if repository.get("kind") == "non_git":
            from .non_git import backup_path, changes, snapshot

            backup = backup_path(self.root, self.run_id)
            if not backup.is_dir() or str(backup) != repository.get("backup_ref"):
                raise ValueError("Completion claim requires retained non-Git backup")
            paths = changes(self.worktree, backup)
            diff_hash = snapshot(self.worktree, backup)
        elif repository.get("kind") == "git" and isinstance(repository.get("base_revision"), str):
            paths = changed_paths(self.worktree, repository["base_revision"])
            diff = _git(self.worktree, "diff", repository["base_revision"], "--")
            untracked_paths = _untracked_paths(self.worktree)
            untracked = "\n".join(
                f"{path}\0{(self.worktree / path).read_bytes().hex()}"
                for path in paths
                if not (self.worktree / path).is_dir() and path in untracked_paths
            )
            diff_hash = _digest(f"{diff}\n{untracked}")
        else:
            raise ValueError("Completion claim requires a valid repository snapshot")
        allowlist = record["allowlist"]
        if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist) or not paths_allowed(paths, allowlist):
            raise ValueError("Execution diff violates request allowlist")
        proposal_id = record.get("accepted_proposal_id")
        if not isinstance(proposal_id, str):
            raise ValueError("Completion claim requires retained SDD acceptance-test conclusion")
        if not criteria or not all(isinstance(criterion, str) and criterion for criterion in criteria):
            raise ValueError("Completion claim requires every acceptance criterion")
        transition(self.root, self.run_id, "completed", mode)
        return CompletionClaim(
            run_id=self.run_id,
            task_id=str(record["task_id"]),
            task_revision=str(record["task_revision"]),
            accepted_proposal_id=proposal_id,
            criterion_results=tuple((criterion, True) for criterion in criteria),
            checks=full_results,
            changed_paths=paths,
            diff_sha256=diff_hash,
        )

    def _record_agent_failure(self, result: InvocationResult, mode: str | None) -> None:
        outcome = "quota" if result.outcome == "quota" else "blocked" if result.outcome in {"timeout", "malformed_output", "permission_requested"} else "retryable_failure"
        record_attempt(self.root, self.run_id, outcome, result.output_sha256, result.outcome, mode)

    def _record_check_failure(self, results: Sequence[CheckResult], mode: str | None) -> None:
        fingerprint = _digest("|".join(f"{result.name}:{result.output_sha256}" for result in results))
        record_attempt(self.root, self.run_id, "retryable_failure", fingerprint, "required_check_failed", mode)


def _untracked_paths(worktree: Path) -> set[str]:
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    return {line[3:] for line in status.splitlines() if line.startswith("?? ")}
