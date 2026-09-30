# Evidence Schema 0.3.0

All records are UTF-8 JSON objects. Every record has `format_version: 1`; unknown
formats fail closed. Timestamps are RFC 3339 UTC strings. IDs and paths are
repository-relative opaque strings unless a field specifies otherwise.

## Repository Policy

`.prway/execution/evidence_policy.json`:

```json
{
  "format_version": 1,
  "allowed_artifact_types": ["coverage-report"],
  "allowed_artifact_path_prefixes": ["reports/"],
  "command_log_retention": "summary_only"
}
```

Artifact references require `type`, `path`, `sha256`, and `description`; their
type and path must match this sole repository policy. No command output belongs
in a record.

## Final Run

`.prway/execution/runs/<run-id>/final_run.json`:

```json
{
  "format_version": 1,
  "run_id": "run-opaque-id",
  "task_id": "MS3-T01a",
  "requirement_revision": "sha256:<digest>",
  "recorded_at": "2026-09-27T00:00:00Z",
  "state": "final",
  "repository": {"kind": "git", "base_revision": "<sha>", "observed_head": "<sha>", "changed_files": ["relative/path"], "patch_sha256": "<digest>"},
  "checks": [{"id": "pytest", "result": "pass", "summary": "16 passed", "evidence_ref": "command:1"}],
  "criteria": [{"id": "criterion-1", "result": "satisfied", "summary": "covered", "evidence_ref": "check:pytest"}],
  "accepted_test_conclusion": {"proposal_id": "proposal-opaque-id", "decision": "accepted", "decided_at": "2026-09-27T00:00:00Z", "reason": "approved test plan", "test_plan": ["pytest"]},
  "git_association": {"state": "pending"},
  "environment": {"python": "3.11", "platform": "linux"},
  "artifacts": []
}
```

`state` is `final`, `historical`, `legacy`, or `unverified`. Only one `final`
run exists per task revision. `historical`, `legacy`, and `unverified` runs do
not satisfy completion. `checks` has one record for each declared required
check; `criteria` has one for each declared acceptance criterion. Check results
are `pass`, `fail`, `deferred`, or `skipped`; criterion results are `satisfied`,
`not_satisfied`, or `deferred`. Each carries a short summary and evidence
reference.

For `repository.kind: "non_git"`, replace Git fields with
`{"kind":"non_git","file_snapshot_sha256":"<digest>"}`. This is valid
without a Git repository.

`git_association.state` is `exact`, `range`, `pending`, or `inconclusive`.
`exact` supplies `commit`; `range` supplies `from` and `to`; pending and
inconclusive need only their state. Association is orientation metadata and
does not block otherwise valid final evidence.

## Events

`.prway/execution/events/<task-id>.jsonl` is append-only. Event records are:

```json
{"format_version":1,"kind":"attempt","recorded_at":"2026-09-27T00:00:00Z","task_id":"MS3-T01a","result":"rejected","reason":"criterion incomplete"}
{"format_version":1,"kind":"manual_confirmation","recorded_at":"2026-09-27T00:00:00Z","task_id":"MS3-T01a","operator":"operator-label","decision":"confirmed","notes":"manual check passed"}
```

Attempt events retain only time, result, and rejection reason. Manual decisions
are `confirmed` or `rejected`; rejection returns task to `in_progress`. The
operator is an unauthenticated MS3 label.

## Accepted Test Conclusion

The embedded `accepted_test_conclusion` is only retained when `decision` is
`accepted`; it includes proposal identity, concise accepted test plan, decision

## Rendering

`.prway/execution/evidence_status.md` is regenerated from current final-run
records. It shows task display ID, final/historical/legacy/unverified evidence,
review or manual state, and Git orientation. It never requires a human to know
a run UUID and is not source of truth.
