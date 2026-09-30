# PlanRunway Ground Rules

This file is mandatory entry point for a new or resumed planning session. Read it before making a
planning or implementation decision.

## Bootstrap Trigger

User can request context restoration by writing:

`Bootstrap project context.`

Do not begin implementation until bootstrap is complete.

## Bootstrap Procedure

1. Read `.prway/system/ground_rules.md` and optional `.prway/user_instructions.md`.
2. Run `planrunway lint --root .`. If it reports `HUMAN_INTERVENTION_REQUIRED`, resolve that before
   normal work. If it reports incompatible repository state, stop and ask operator to run explicit
   migration.
3. Read `.prway/vision/core_vision.md`, `.prway/technical/tech_stack.md`, and
   `.prway/technical/testing_approach.md` when present.
4. Run `planrunway status --root .` to identify active milestone and task. Read matching project
   milestone and task material when present.
5. Restore product goal, current delivery state, active work, relevant technical constraints, and
   testing approach before proposing implementation.

## Initial Product Discovery

If vision, technical preferences, or testing approach are missing, empty, placeholder-only, or too
vague to support work, do not guess. Ask operator for these three inputs in order:

1. Product vision: user/problem, intended outcome, constraints, and success signals.
2. Technical preferences: stack, architecture boundaries, dependencies, deployment/runtime limits.
3. Testing approach: required automated checks, manual checks, and acceptance evidence.

Record approved answers in:

- `.prway/vision/core_vision.md`
- `.prway/technical/tech_stack.md`
- `.prway/technical/testing_approach.md`

After all three are adequate, ask permission before proposing milestone and task grid. Create
approved records only through `planrunway milestone-create` and `planrunway task-create`; use
validated lifecycle and reorder commands rather than editing canonical JSON.

## Working Rules

- PlanRunway state is reviewable repository state, not proof that product code is correct.
- `done` requires declared evidence and applicable human confirmation; never change status merely
  to make lint pass.
- Use `planrunway lint` and `planrunway status` after work. Run `planrunway render` only to refresh
  local derived views.
- When canonical conflicts exist, stop normal progress and use `planrunway conflict-resolve`.
- Do not create Git commits. Staging remains explicit and optional.
- Keep project-specific instructions in `.prway/user_instructions.md`, not this system file.
