from __future__ import annotations

import argparse
import contextlib
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .conflicts import describe as describe_conflicts
from .conflicts import detect as detect_conflicts
from .conflicts import human_intervention_message, resolve as resolve_conflicts, resolve_semantic, restore_from_revision
from .evidence import bind_git_association, complete_task, confirm_task, lint_tasks, migrate_legacy_tasks, register_task, render_evidence_status, revise_task
from .planning import SCHEMA_VERSION, active_status, change_record, create_milestone, create_task, glossary, initialize_planning, lint_planning, migrate_legacy_planning, prepare_review, reorder, render_status, set_state


ROOT_NAME = ".prway"
RELEASE_URL = "https://github.com/waypointahead/planrunway/releases"
MARKER_BEGIN = "<!-- planrunway:begin -->"
MARKER_END = "<!-- planrunway:end -->"
LEGACY_MANAGED_BLOCK = (
    f"{MARKER_BEGIN}\n"
    "Read `.prway/system/bin/planrunway` before working in this repository.\n"
    f"{MARKER_END}\n"
)
MANAGED_BLOCK = (
    "Read `.prway/system/ground_rules.md` before working in this repository.\n\n"
    f"{MARKER_BEGIN}\n"
    "Read `.prway/system/bin/planrunway` before working in this repository.\n"
    f"{MARKER_END}\n"
)


def _root(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError(f"Root does not exist: {path}")
    return path


def _state(root: Path) -> Path:
    return root / ROOT_NAME


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _repository_lock(root: Path):
    lock = root / ".prway.lock"
    if lock.is_symlink():
        raise ValueError(f"Refusing symlink lock: {lock}")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)


def _agents_path(root: Path) -> Path:
    return root / "AGENTS.md"


def _agents_content(root: Path) -> str | None:
    path = _agents_path(root)
    if path.is_symlink():
        raise ValueError(f"Refusing symlink instruction file: {path}")
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _managed_agents_state(content: str | None) -> str:
    if content is None:
        return "absent"
    begins = content.count(MARKER_BEGIN)
    ends = content.count(MARKER_END)
    if begins == 0 and ends == 0:
        return "foreign"
    if begins == 1 and ends == 1 and (content == MANAGED_BLOCK or content.endswith("\n" + MANAGED_BLOCK)):
        return "managed"
    if begins == 1 and ends == 1 and (content == LEGACY_MANAGED_BLOCK or content.endswith("\n" + LEGACY_MANAGED_BLOCK)):
        return "legacy_managed"
    raise ValueError("Malformed, duplicate, or colliding PlanRunway markers in AGENTS.md")


def _append_managed_block(root: Path, content: str | None) -> None:
    path = _agents_path(root)
    _atomic_write(path, MANAGED_BLOCK if content is None else content + "\n" + MANAGED_BLOCK)


def _remove_managed_block(root: Path, content: str) -> None:
    path = _agents_path(root)
    block = MANAGED_BLOCK if content.endswith(MANAGED_BLOCK) else LEGACY_MANAGED_BLOCK
    if content == block:
        path.unlink()
    else:
        _atomic_write(path, content[: -len("\n" + block)])


def cmd_init(root: Path) -> int:
    state = _state(root)
    if state.is_symlink():
        print(f"[planrunway] ERROR: Refusing symlink planning root: {state}", file=sys.stderr)
        return 1
    try:
        with _repository_lock(root):
            agents = _agents_content(root)
            agents_state = _managed_agents_state(agents)
            if state.exists():
                _load_envelope(root)
                if agents_state not in {"managed", "legacy_managed"}:
                    raise ValueError("Existing .prway does not match managed AGENTS.md integration")
                print(f"[planrunway] {ROOT_NAME} is already initialized")
                return 0
            if agents_state in {"managed", "legacy_managed"}:
                raise ValueError("Managed AGENTS.md block exists without .prway")
            temporary = Path(tempfile.mkdtemp(prefix=".prway.", dir=root))
            try:
                (temporary / "system" / "bin").mkdir(parents=True)
                for directory in ("vision", "technical", "execution", "meta", "documentation"):
                    (temporary / directory).mkdir()
                _write_json(temporary / "compatibility.json", {
                    "format_version": 1,
                    "minimum_cli_version": __version__,
                    "release_url": RELEASE_URL,
                    "schema_version": SCHEMA_VERSION,
                })
                _write_json(temporary / "config.json", {
                    "profile_schema_version": 1,
                    "tier_id": "manual-core",
                    "drift": {"auto_check_on_task_done": False},
                })
                _write_json(temporary / "execution" / "evidence_policy.json", {
                    "format_version": 1,
                    "allowed_artifact_types": [],
                    "allowed_artifact_path_prefixes": [],
                    "command_log_retention": "summary_only",
                })
                (temporary / "execution" / "runs").mkdir()
                (temporary / "execution" / "events").mkdir()
                (temporary / "execution" / "tasks").mkdir()
                initialize_planning(temporary)
                (temporary / ".gitignore").write_text(
                    "# Derived local views; regenerate with `planrunway render`.\n"
                    "execution/execution_status.md\n"
                    "execution/evidence_status.md\n"
                    "# One-time migration rollback sources.\n"
                    "meta/plan.0.3.0.json\n"
                    "execution/tasks.legacy.json\n",
                    encoding="utf-8",
                )
                _install_embedded_runtime(temporary)
                (temporary / "user_instructions.md").write_text(
                    "# Project Instructions\n\nAdd repository-specific instructions here.\n",
                    encoding="utf-8",
                )
                (temporary / "vision" / "core_vision.md").write_text(
                    f"# Product Vision\n\nPlan system version: {SCHEMA_VERSION}\n", encoding="utf-8"
                )
                _append_managed_block(root, agents)
                os.replace(temporary, state)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                if agents is None:
                    _agents_path(root).unlink(missing_ok=True)
                elif _agents_path(root).exists():
                    _atomic_write(_agents_path(root), agents)
                raise
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print(f"[planrunway] Initialized {ROOT_NAME} with manual-core profile")
    return 0


def _install_embedded_runtime(state: Path) -> None:
    (state / "system" / "product.json").write_text(
        json.dumps({"name": "planrunway", "version": __version__, "schema_version": SCHEMA_VERSION}, indent=2) + "\n",
        encoding="utf-8",
    )
    embedded = state / "system" / "bin" / "planrunway"
    source = importlib.resources.files("planrunway").joinpath("runtime/planrunway")
    with importlib.resources.as_file(source) as runtime:
        shutil.copyfile(runtime, embedded)
    embedded.chmod(0o755)
    ground_rules = state / "system" / "ground_rules.md"
    source = importlib.resources.files("planrunway").joinpath("runtime/ground_rules.md")
    with importlib.resources.as_file(source) as runtime:
        shutil.copyfile(runtime, ground_rules)


def _semver(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ValueError("version must be a semantic version")
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError("version must be a semantic version")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _load_envelope(root: Path) -> dict[str, object]:
    path = _state(root) / "compatibility.json"
    if not path.is_file():
        raise ValueError(f"Missing compatibility envelope: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format_version") != 1:
        raise ValueError("Unsupported compatibility envelope format")
    try:
        minimum = _semver(value.get("minimum_cli_version"))
        installed = _semver(__version__)
    except ValueError as error:
        raise ValueError(f"Invalid compatibility envelope: {error}") from error
    release_url = value.get("release_url")
    if not isinstance(release_url, str) or not release_url.startswith("https://"):
        raise ValueError("Invalid compatibility envelope: missing release_url")
    if installed < minimum:
        raise ValueError(
            f"PlanRunway CLI {__version__} is below required {value['minimum_cli_version']} for {root}. "
            f"Install a compatible release from: {release_url}"
        )
    return value


def cmd_lint(root: Path) -> int:
    try:
        conflicts = detect_conflicts(root)
        if conflicts:
            raise ValueError(human_intervention_message(root, conflicts))
        _load_envelope(root)
        config = _state(root) / "config.json"
        if not config.is_file():
            raise ValueError(f"Missing Core configuration: {config}")
        if json.loads(config.read_text(encoding="utf-8")).get("tier_id") != "manual-core":
            raise ValueError("Unsupported MS3 automation profile")
        lint_tasks(_state(root))
        lint_planning(_state(root))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print("[planrunway] PASS core")
    return 0


def cmd_conflict_resolve(root: Path, strategy: str | None, resolve_all: bool, stage: bool | None, rename: str | None, archive: bool, reopen: bool, restore: str | None) -> int:
    try:
        _load_envelope(root)
        conflicts = detect_conflicts(root)
        if not conflicts:
            print("[planrunway] No unresolved PlanRunway conflicts")
            return 0
        print(describe_conflicts(root, conflicts), end="")
        semantic_actions = sum(bool(item) for item in (rename, archive, reopen, restore))
        if semantic_actions and strategy:
            raise ValueError("Choose either --strategy or one semantic repair action")
        if not semantic_actions and len(conflicts) == 1 and not conflicts[0].git_unmerged and sys.stdin.isatty():
            kind = conflicts[0].kind
            if kind == "duplicate display identity":
                choice = input("Choose repair [rename/archive/cancel]: ").strip().lower()
                if choice == "rename":
                    rename = input("New unique display ID: ").strip()
                elif choice == "archive":
                    archive = True
                elif choice in {"", "cancel"}:
                    print("[planrunway] Resolution cancelled; no files changed")
                    return 0
                else:
                    raise ValueError("Choose rename, archive, or cancel")
            elif kind == "duplicate final-run identity":
                choice = input("Choose repair [reopen/cancel]: ").strip().lower()
                if choice == "reopen":
                    reopen = True
                elif choice in {"", "cancel"}:
                    print("[planrunway] Resolution cancelled; no files changed")
                    return 0
                else:
                    raise ValueError("Choose reopen or cancel")
            elif kind == "malformed canonical record":
                choice = input("Choose repair [restore/cancel]: ").strip().lower()
                if choice == "restore":
                    restore = input("Reviewed Git revision: ").strip()
                elif choice in {"", "cancel"}:
                    print("[planrunway] Resolution cancelled; no files changed")
                    return 0
                else:
                    raise ValueError("Choose restore or cancel")
            semantic_actions = sum(bool(item) for item in (rename, archive, reopen, restore))
        if semantic_actions:
            if len(conflicts) != 1:
                raise ValueError("Resolve one semantic conflict at a time; do not use --all")
            if semantic_actions != 1:
                raise ValueError("Choose exactly one semantic repair action")
            if stage is None and not sys.stdin.isatty():
                print("[planrunway] ERROR: conflict-resolve requires --stage or --no-stage outside a TTY", file=sys.stderr)
                return 2
            if restore:
                resolved = restore_from_revision(root, conflicts[0], restore, False)
            else:
                action = "rename" if rename else "archive" if archive else "reopen"
                resolved = resolve_semantic(root, conflicts[0], action, rename, False)
        else:
            if len(conflicts) > 1 and not resolve_all:
                print("[planrunway] HUMAN_INTERVENTION_REQUIRED: select --all to resolve this batch with one strategy", file=sys.stderr)
                return 1
            if strategy is None and sys.stdin.isatty():
                choice = input("Choose strategy [ours/theirs/cancel]: ").strip().lower()
                if choice in {"", "cancel"}:
                    print("[planrunway] Resolution cancelled; no files changed")
                    return 0
                strategy = choice
            if strategy is None:
                print("[planrunway] ERROR: conflict-resolve requires --strategy ours or --strategy theirs outside a TTY", file=sys.stderr)
                return 2
            if stage is None and not sys.stdin.isatty():
                print("[planrunway] ERROR: conflict-resolve requires --stage or --no-stage outside a TTY", file=sys.stderr)
                return 2
            resolved = resolve_conflicts(root, conflicts, strategy, False)
        lint_tasks(_state(root))
        lint_planning(_state(root))
        print("[planrunway] Selected resolution is valid:")
        for path in resolved:
            print(f"- {path.relative_to(root).as_posix()}")
        if stage is None:
            stage = input("Stage validated PlanRunway files with git add? [y/N]: ").strip().lower() in {"y", "yes"}
        if stage:
            result = subprocess.run(["git", "add", "--", *(path.relative_to(root).as_posix() for path in resolved)], cwd=root, text=True, capture_output=True, check=False)
            if result.returncode:
                raise ValueError(f"Unable to stage resolved files: {result.stderr.strip()}")
            return cmd_lint(root)
        print("[planrunway] Files left unstaged. Run git add for listed files, then planrunway lint.")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1


def _conflict_gate(root: Path) -> int:
    conflicts = detect_conflicts(root)
    if not conflicts:
        return 0
    print(f"[planrunway] ERROR: {human_intervention_message(root, conflicts)}", file=sys.stderr)
    return 1


def cmd_status(root: Path) -> int:
    if cmd_lint(root):
        return 1
    milestone, task = active_status(_state(root))
    if milestone is None:
        print("[planrunway] No milestones configured")
    else:
        print(f"[planrunway] Active milestone: {milestone['display_id']} | {milestone['state']} | {milestone['title']}")
        if task is not None:
            print(f"[planrunway] Active task: {milestone['display_id']}-{task['display_id']} | {task['state']} | {task['title']}")
    return 0


def cmd_migrate(root: Path, yes: bool) -> int:
    if not yes:
        print("[planrunway] ERROR: migrate requires --yes", file=sys.stderr)
        return 2
    try:
        with _repository_lock(root):
            state = _state(root)
            _load_envelope(root)
            planning_changed = migrate_legacy_planning(state)
            evidence_changed = migrate_legacy_tasks(state)
            ignore_path = state / ".gitignore"
            generated_ignores = (
                "# Derived local views; regenerate with `planrunway render`.\n"
                "execution/execution_status.md\n"
                "execution/evidence_status.md\n"
                "# One-time migration rollback sources.\n"
                "meta/plan.0.3.0.json\n"
                "execution/tasks.legacy.json\n"
            )
            existing_ignores = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
            existing_lines = existing_ignores.splitlines()
            missing_ignores = [line for line in generated_ignores.splitlines() if line and line not in existing_lines]
            if missing_ignores:
                ignore_path.write_text(existing_ignores.rstrip() + "\n" + "\n".join(missing_ignores) + "\n", encoding="utf-8")
            envelope_path = state / "compatibility.json"
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            envelope["schema_version"] = SCHEMA_VERSION
            _write_json(envelope_path, envelope)
            _install_embedded_runtime(state)
            lint_tasks(state)
            lint_planning(state)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    detail = "migrated canonical entity records" if planning_changed or evidence_changed else "already current"
    print(f"[planrunway] Embedded Core runtime upgraded; {detail}")
    return 0


def cmd_uninstall(root: Path, dry_run: bool, yes: bool) -> int:
    state = _state(root)
    if state.is_symlink():
        print(f"[planrunway] ERROR: Refusing symlink planning root: {state}", file=sys.stderr)
        return 1
    if not state.exists():
        print("[planrunway] Nothing to uninstall")
        return 0
    try:
        with _repository_lock(root):
            _load_envelope(root)
            agents = _agents_content(root)
            if _managed_agents_state(agents) not in {"managed", "legacy_managed"}:
                raise ValueError("Existing .prway does not match managed AGENTS.md integration")
            assert agents is not None
            if dry_run:
                print(f"[planrunway] Would back up and remove: {state}")
                print(f"[planrunway] Would remove managed block:\n{MANAGED_BLOCK}", end="")
                return 0
            if not yes:
                print("[planrunway] ERROR: uninstall requires --yes", file=sys.stderr)
                return 2
            backup = root / ".prway-backups" / str(time.time_ns())
            backup.parent.mkdir(mode=0o700, exist_ok=True)
            shutil.copytree(state, backup)
            try:
                _remove_managed_block(root, agents)
                shutil.rmtree(state)
            except BaseException:
                if not _agents_path(root).exists():
                    _atomic_write(_agents_path(root), agents)
                raise
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print(f"[planrunway] Uninstalled .prway; backup retained at {backup}")
    return 0


def cmd_task_register(root: Path, task_id: str | None, criteria: list[str], checks: list[str], revision: str | None) -> int:
    try:
        _load_envelope(root)
        register_task(_state(root), task_id or "", criteria, checks, revision or "")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print(f"[planrunway] Registered task {task_id}")
    return 0


def cmd_task_complete(root: Path, evidence: str | None, awaiting_manual: bool) -> int:
    try:
        _load_envelope(root)
        if not evidence:
            raise ValueError("task-complete requires --evidence PATH")
        complete_task(_state(root), json.loads(Path(evidence).read_text(encoding="utf-8")), awaiting_manual)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print("[planrunway] Task awaits manual confirmation" if awaiting_manual else "[planrunway] Task completed")
    return 0


def cmd_task_confirm(root: Path, task_id: str | None, operator: str | None, notes: str | None, reject: bool) -> int:
    try:
        _load_envelope(root)
        confirm_task(_state(root), task_id or "", operator or "", notes or "", confirmed=not reject)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print("[planrunway] Task reopened" if reject else "[planrunway] Task manually confirmed")
    return 0


def cmd_task_revise(root: Path, task_id: str | None, revision: str | None) -> int:
    try:
        _load_envelope(root)
        revise_task(_state(root), task_id or "", revision or "")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print(f"[planrunway] Revised task {task_id}")
    return 0


def cmd_task_bind_git(root: Path, task_id: str | None, association_state: str | None, commit: str | None, start: str | None, end: str | None) -> int:
    try:
        _load_envelope(root)
        association = {"state": association_state or ""}
        if commit:
            association["commit"] = commit
        if start:
            association["from"] = start
        if end:
            association["to"] = end
        bind_git_association(_state(root), task_id or "", association)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print(f"[planrunway] Bound Git association for {task_id}")
    return 0


def cmd_planning_mutation(root: Path, action: str, identifier: str | None, milestone: str | None, title: str | None, state: str | None, successor: str | None) -> int:
    try:
        _load_envelope(root)
        planning_root = _state(root)
        if action == "milestone-create":
            create_milestone(planning_root, identifier or "", title or "")
        elif action == "task-create":
            create_task(planning_root, milestone or "", identifier or "", title or "")
        elif action == "milestone-set-state":
            set_state(planning_root, "milestone", identifier or "", state or "")
        elif action == "task-set-state":
            set_state(planning_root, "task", identifier or "", state or "", milestone)
        elif action.endswith("-reorder"):
            kind = action.removesuffix("-reorder")
            reorder(planning_root, kind, identifier or "", milestone, _MUTATION_BEFORE, _MUTATION_AFTER)
        else:
            kind, operation = action.split("-", 1)
            change_record(planning_root, kind, operation, identifier or "", milestone, successor)
        lint_planning(planning_root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        return 1
    print(f"[planrunway] Updated planning: {action}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="planrunway")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", choices=("init", "lint", "status", "migrate", "uninstall", "conflict-resolve", "task-register", "task-complete", "task-confirm", "task-revise", "task-bind-git", "milestone-create", "task-create", "milestone-set-state", "task-set-state", "milestone-archive", "task-archive", "milestone-supersede", "task-supersede", "milestone-reopen", "task-reopen", "milestone-reorder", "task-reorder", "prepare-review", "glossary-add", "glossary-list", "glossary-validate", "render", "validate"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--task-id")
    parser.add_argument("--criterion", action="append", default=[])
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--requirement-revision")
    parser.add_argument("--evidence")
    parser.add_argument("--awaiting-manual", action="store_true")
    parser.add_argument("--operator")
    parser.add_argument("--notes")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--association-state")
    parser.add_argument("--commit")
    parser.add_argument("--from")
    parser.add_argument("--to")
    parser.add_argument("--id")
    parser.add_argument("--milestone")
    parser.add_argument("--title")
    parser.add_argument("--state")
    parser.add_argument("--successor")
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--term")
    parser.add_argument("--definition")
    parser.add_argument("--rename")
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--reopen", action="store_true")
    parser.add_argument("--restore", metavar="GIT_REVISION")
    parser.add_argument("--strategy", choices=("ours", "theirs"))
    parser.add_argument("--all", action="store_true")
    stage_group = parser.add_mutually_exclusive_group()
    stage_group.add_argument("--stage", dest="stage", action="store_true")
    stage_group.add_argument("--no-stage", dest="stage", action="store_false")
    parser.set_defaults(stage=None)
    args = parser.parse_args()
    try:
        root = _root(args.root)
    except ValueError as error:
        print(f"[planrunway] ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    if args.command not in {"init", "uninstall", "migrate", "lint", "validate", "conflict-resolve"}:
        try:
            if _conflict_gate(root):
                raise SystemExit(1)
        except OSError as error:
            print(f"[planrunway] ERROR: {error}", file=sys.stderr)
            raise SystemExit(1) from error
    if args.command == "conflict-resolve":
        result = cmd_conflict_resolve(root, args.strategy, args.all, args.stage, args.rename, args.archive, args.reopen, args.restore)
    elif args.command == "migrate":
        result = cmd_migrate(root, args.yes)
    elif args.command == "uninstall":
        result = cmd_uninstall(root, args.dry_run, args.yes)
    elif args.command == "task-register":
        result = cmd_task_register(root, args.task_id, args.criterion, args.check, args.requirement_revision)
    elif args.command == "task-complete":
        result = cmd_task_complete(root, args.evidence, args.awaiting_manual)
    elif args.command == "task-confirm":
        result = cmd_task_confirm(root, args.task_id, args.operator, args.notes, args.reject)
    elif args.command == "task-revise":
        result = cmd_task_revise(root, args.task_id, args.requirement_revision)
    elif args.command == "task-bind-git":
        result = cmd_task_bind_git(root, args.task_id, args.association_state, args.commit, args.__dict__["from"], args.to)
    elif args.command in {"milestone-create", "task-create", "milestone-set-state", "task-set-state", "milestone-archive", "task-archive", "milestone-supersede", "task-supersede", "milestone-reopen", "task-reopen", "milestone-reorder", "task-reorder"}:
        global _MUTATION_BEFORE, _MUTATION_AFTER
        _MUTATION_BEFORE, _MUTATION_AFTER = args.before, args.after
        result = cmd_planning_mutation(root, args.command, args.id, args.milestone, args.title, args.state, args.successor)
    elif args.command == "prepare-review":
        try:
            _load_envelope(root)
            prepare_review(_state(root), args.milestone or "")
            result = 0
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"[planrunway] ERROR: {error}", file=sys.stderr)
            result = 1
    elif args.command.startswith("glossary-"):
        try:
            _load_envelope(root)
            output = glossary(_state(root), args.command.removeprefix("glossary-"), args.term, args.definition)
            if output:
                print(output, end="" if output.endswith("\n") else "\n")
            result = 0
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"[planrunway] ERROR: {error}", file=sys.stderr)
            result = 1
    elif args.command == "render":
        if cmd_lint(root):
            result = 1
        else:
            render_evidence_status(_state(root), write=True)
            render_status(_state(root), write=True)
            print("[planrunway] Rendered local status views")
            result = 0
    elif args.command == "validate":
        result = cmd_lint(root)
    else:
        result = {"init": cmd_init, "lint": cmd_lint, "status": cmd_status}[args.command](root)
    raise SystemExit(result)


if __name__ == "__main__":
    main()
