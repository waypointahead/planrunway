# PlanRunway

**Keep product intent and delivery state in the repository.**

PlanRunway is a local tool for people who build software with coding agents and expect to keep evolving their projects over time, whether regularly or intermittently.

PlanRunway extends specification-driven work beyond individual specs and coding tasks. It is organized around product vision and the product's evolution through milestones.

It keeps vision, milestones, tasks, evidence, and review artifacts in repository files, so recorded product and delivery context can outlast individual chats, coding sessions, tools, models, and agent harnesses.

```text
Vision -> milestones -> tasks -> evidence -> review
```

It is useful when work pauses, priorities change, you alternate between projects, or a different agent continues the work later.

## Product State Over Time

Individual specs can describe individual changes well. Longer-lived projects also need to retain what is currently important, what work exists, what changed direction, and what was reviewed.

PlanRunway models that state explicitly.

Milestones provide a higher-level delivery structure above individual tasks. They can be reordered as priorities change, so the project can be seen as a set of meaningful, manageable product stages rather than a continuous queue of tasks.

Tasks can be created, reordered, reopened, archived, or superseded. Evidence attaches to tasks, while review artifacts summarize milestone-level results.

Canonical records have stable UUID identities. Human-facing display IDs and delivery order are separate, so priorities can change without breaking existing references.

## Start With Product Context

Install a verified wheel from [GitHub Releases](https://github.com/waypointahead/planrunway/releases) with Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
sha256sum -c planrunway-<version>.sha256
uv tool install --reinstall ./planrunway-<version>-py3-none-any.whl
```

In the repository root:

```bash
planrunway init
```

`init` installs PlanRunway repository guidance, including a managed link in `AGENTS.md` to the ground rules under `.prway/system/`.

Then tell an agent that follows those instructions:

> Bootstrap project context.

The ground rules guide the agent through a short discovery flow. It validates current repository state, asks for missing product vision, technical preferences, and testing approach, and records approved answers in readable repository files.

Before creating proposed delivery records, the agent is instructed to ask for approval.

For an existing project, this establishes a starting point rather than reconstructing its history. PlanRunway starts from current repository state and what you explicitly confirm; it does not recover the full original intent automatically.

## Turn Intent Into Delivery Work

After discovery, the agent can propose a small milestone and task grid for review. After approval, it can record canonical entities through PlanRunway CLI and create human-facing project briefs alongside them when useful.

```text
.prway/
  vision/                    project-authored vision and milestone scope
  technical/                 project-authored stack and testing approach
  execution/tasks/MS*/       optional project-authored task briefs

  meta/milestones/*.json     canonical milestone lifecycle and order
  meta/tasks/*.json          canonical task lifecycle and order

  execution/tasks/*.json     task evidence
  execution/runs/            immutable final evidence
```

Human-facing Markdown briefs keep product intent, scope, and implementation context readable. Lifecycle, ordering, and evidence use structured records where deterministic behavior matters.

Later sessions can recover the active milestone and task from validated repository state instead of inferring current priorities from code or old conversations.

An agent can use the same repository context to discuss a requested change, propose updates, and apply explicitly approved operations.

For example:

> We no longer need CSV export. Replace it with scheduled JSON delivery.

PlanRunway itself does not interpret arbitrary natural-language requests. Agents translate approved intent into a small, validated CLI surface, so deterministic state changes stay deterministic.

The same operations are available directly through the CLI:

```bash
planrunway milestone-create --id MS1 --title "First milestone"
planrunway task-create --milestone MS1 --id T01 --title "Define acceptance criteria"
planrunway task-set-state --milestone MS1 --id T01 --state in_progress
planrunway status
```

## Change Direction Without Losing History

Plans change during implementation and review.

Work may move earlier or later. Completed work may need to reopen after feedback. One task may replace another.

PlanRunway records those changes explicitly:

```bash
planrunway task-reorder --milestone MS1 --id T01 --before T02
planrunway task-supersede --milestone MS1 --id T01 --successor T03
planrunway task-reopen --milestone MS1 --id T01
```

Stable identity remains separate from delivery order, so work can move without changing existing references and superseded work can remain visible in project state.

Agents use the same validated operations as humans. Canonical ordering and lifecycle metadata should not be edited directly.

## Resume In A Later Session

When work resumes, PlanRunway gives the next session explicit repository state to inspect, including:

- current product vision,
- milestones and their order,
- task state and priority,
- technical preferences,
- testing approach,
- task evidence,
- milestone review artifacts,
- archived or superseded work.

The goal is not to preserve every conversation, but the product and delivery state that future work depends on.

This is particularly useful when you switch between projects often or return after the original context is no longer fresh.

## Work Across Branches

Planning and evidence are entity-scoped, so independent changes to different entities can merge through normal Git workflows.

When branches make competing changes to the same canonical entity, PlanRunway keeps that conflict visible instead of choosing a product decision automatically.

```bash
planrunway lint
```

`planrunway lint` blocks normal progress while canonical state is conflicted.

Interactive recovery is available with:

```bash
planrunway conflict-resolve
```

The resolver shows affected files, explains the conflict and its consequences, presents safe choices, validates the selected resolution, and asks whether the resolved files should be staged.

It does not choose between competing product decisions automatically and does not create Git commits.

Collaboration and recovery coverage includes:

- independent planning and evidence changes,
- competing changes to the same entity,
- duplicate identities,
- competing final runs,
- supersession conflicts,
- rebases,
- malformed records,
- multi-conflict batches.

Rendered views are derived local output and can be regenerated from canonical state:

```bash
planrunway render
```

For scripts and non-interactive agents:

```bash
planrunway lint
planrunway conflict-resolve --strategy ours --stage
planrunway render
```

`ours` keeps the version from the branch that was checked out when the merge started. `theirs` keeps the version from the branch being merged or rebased.

If no safe automated repair exists, the resolver identifies the manual recovery boundary, preserves the unresolved state, and does not stage anything.

## Structured State

PlanRunway uses readable documents for product context and structured canonical records where reliable tool behavior matters.

Human-facing briefs are encouraged where they help people and agents understand intent, scope, and implementation context. Operations such as reordering, reopening, or superseding work use defined semantics and validation rather than relying on edits across several planning documents.

This gives milestones and tasks explicit identity, lifecycle, ordering, and conflict behavior while keeping the repository inspectable and version-controlled.

## Working Alongside Existing Tools

PlanRunway Core keeps its state under `.prway/` and owns only its bounded managed block in the root `AGENTS.md`. It does not take ownership of the rest of the development environment.

Core provides:

- local planning state,
- milestone and task lifecycle operations,
- explicit delivery order,
- task evidence,
- milestone review artifacts,
- manual confirmation points,
- conflict recovery,
- compatibility checks,
- repository ownership boundaries.

Core does not:

- launch coding agents,
- create worktrees,
- orchestrate implementation,
- merge code,
- create Git commits,
- provide hosted project management,
- replace source control, CI, issue trackers, or test frameworks.

It is intended to work alongside the coding agents and development tools you already use.

## License

PlanRunway is licensed under [MPL-2.0](LICENSE).
