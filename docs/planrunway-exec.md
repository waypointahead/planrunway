# PlanRunway Exec Guide

PlanRunway Exec is optional, separately installed controlled execution for one
approved task. Core remains canonical source of planning state. Exec never marks
a task done, auto-commits, or integrates without an explicit operator decision.

## Install And Enable

Install verified Core and Exec release assets separately:

```bash
sha256sum -c planrunway-<core-version>.sha256
sha256sum -c planrunway-exec-<exec-version>.sha256
uv tool install --reinstall ./planrunway-<core-version>-py3-none-any.whl
uv tool install --reinstall ./planrunway_exec-<exec-version>-py3-none-any.whl
```

Enable Exec deliberately in a schema `0.4.0` repository:

```bash
planrunway config-reconfigure --tier controlled-exec --yes
```

Core init and migration never select `controlled-exec` automatically. Exec
requires OpenCode on `PATH` and explicit `--model PROVIDER/MODEL` for each run.
On Linux/WSL, install `bubblewrap` (`bwrap`) and use the separately installed
`planrunway-exec-review` wrapper. Keep project outside `/tmp`; the wrapper mounts
the project read-only and gives OpenCode private temporary storage.

## One Approved Task

```bash
planrunway-exec inspect --root . --task MS1-T1
planrunway-exec start --root . --mode build --task MS1-T1 \
  --revision SHA256_FROM_INSPECT --allow 'src/example.py' \
  --check 'full=python -m pytest -q' \
  --fast-check 'quick=python -m pytest -q tests/test_example.py' \
  --model PROVIDER/MODEL \
  --reviewer-wrapper "$(command -v planrunway-exec-review)"
planrunway-exec propose --root . --run-id RUN_ID --mode build
planrunway-exec approve --root . --run-id RUN_ID --mode build --proposal-id PROPOSAL_ID --yes
planrunway-exec implement --root . --run-id RUN_ID --mode build
planrunway-exec review --root . --run-id RUN_ID --mode build
planrunway-exec claim --root . --run-id RUN_ID --mode build
planrunway-exec claim-show --root . --run-id RUN_ID
planrunway-exec integrate --root . --run-id RUN_ID --mode build --yes
```

Before `approve`, review proposal against task criteria. Before `integrate`,
review CompletionClaim and approve patch. `claim` displays every criterion and
required check, changed paths and diff SHA-256. `claim-show` reads retained claim
data after worktree cleanup. Exec prints a short progress line before agents or
checks run.

Task-revision changes, unapproved paths, failed checks, reviewer modification,
or main-tree drift block integration. `--mode plan` and absent mode refuse every
execution mutation; `status` and `inspect` remain read-only.

## Recovery And Non-Git Projects

`resume --run-id RUN_ID --mode build` restores interrupted local runs. A
cross-machine restart with retained local worktree requires
`--discard-local --yes` and consumes retry budget. `cancel --run-id RUN_ID
--mode build --yes` removes obsolete worktree while retaining compact run state.

For non-Git projects only, `start --allow-no-git` uses an isolated copy and
retained rollback backup. This is weaker isolation. Missing backups, unapproved
paths, or changed main files block integration.
