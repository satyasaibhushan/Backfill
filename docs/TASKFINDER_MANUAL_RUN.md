# One manual Task Finder run

Task Finder owns projects, tasks, Markdown, updates and source references. Backfill
owns guarded execution and results. Reuse Task Finder's existing agent run records
and `runner/update` consumer; do not introduce another task store or dashboard.

## Current implementation and gaps

Backfill's existing `connect-app` / `connect-project` connection is scoped to one
project. `run-app` registers work through `POST /v2/external/tasks`, uses a stable
request ID, requests quota admission, then runs the native executor through the
existing guard. External tasks are excluded from the automatic queue runner.
Quota waiting, shared project allowances, provider selection, revocation, saved
progress and human review already exist.

Task Finder's current runner already converts its run ID, prompt and settings to
this envelope, consumes JSONL output and posts `budgetTask`, output and state back
through its authenticated, leased `runner/update` API. It maps Backfill `review`
to `awaiting_review`. Its existing service polls and may retry waiting runs;
that service must not be installed or enabled for this manual slice. Its setup
also creates a project connection, which needs separate authorization.

The owner API already has `POST /api/agent/conversations` with a task target,
`POST /api/agent/conversations/{id}/messages` to enqueue, and review/pause actions.
`POST /api/agent/runner/update` requires the existing runner identity and run lease.
Today that lease comes from `runner/poll`, which calls autoQueue and selects queued
or waiting runs. A manual-only consumer therefore needs a one-shot claim of an
explicit run ID, with no queue scan/autoQueue; it cannot safely reuse poll unchanged.
The simplest proposed addition on the Task Finder side is
`POST /api/agent/runner/claim` with `{ "id": "explicit-run-uuid" }`, returning the
same leased run shape for the existing update API. This is a proposal, not an
implemented endpoint in Backfill or a change to Task Finder in this checkout.

The reported earlier integration failure has no verified technical cause. Current
gaps are a discoverable validation/status path, bounded project/task context,
one-shot claiming, and the consumer's append-only task updates and output links.
This change supplies validation/status and context/event correlation. Task Finder
owns its task-context API, eligibility check and task-update/link writes. The
consumer integration must still be verified against that implementation.

## Contract

Manually select an explicitly execution-eligible Task Finder task. Have Task
Finder resolve the project's inherited repository/workflow references and task
overrides, including origin project IDs, and render a bounded Markdown snapshot.
Keep the approved instructions separate from that reference data. The wire
projection is deliberately small:

```json
{
  "request_id": "taskfinder-run-unique-id",
  "task": {
    "title": "Inspect the selected project",
    "instructions": "Review the referenced files and return findings and proposed changes for my review.",
    "folder": "/workspace/selected-project",
    "provider": "auto",
    "access": "read",
    "allowance": 5,
    "priority": "normal"
  },
  "context_snapshot": {
    "version": 1,
    "task_id": "taskfinder-task-id",
    "markdown": "Task description, recent updates, project plan, and resolved references with their origins."
  }
}
```

Use the existing Task Finder run UUID as `request_id` in production. The identity
is 8–120 ASCII letters/digits or `_.:-`; `task_id` uses the existing Backfill key
rules (1–100 characters). Context is at most 20,000 characters and the resulting
instructions at most 50,000. Registration JSON is at most 256 KiB. Unknown fields,
invalid permissions, nonfinite allowances, unsupported versions and schedules
are rejected with sanitized errors. `context_snapshot` is optional, so existing
Task Finder runner requests continue to work.

The execution host's working folder and permissions remain explicitly selected
by the caller. The snapshot cannot override either, pick a provider account,
grant tool access, approve work, or trigger fetching/commands. It is serialized
into existing task instructions as untrusted reference data. Changing the context
or instructions under an existing request ID returns 409. Retry the same snapshot
after a lost response; use a new run ID for newly approved instructions/context.
The app credential, rather than a caller-supplied project, determines the budget.

## Happy path

With an already authorized private connection file on the execution host:

```sh
# Pure validation: no credential access, registration, account reading or execution.
backfill run-app --dry-run --json selected-task.json

# Inspect the selected connection without creating a credential or task.
backfill app-status --connection /private/path/app-connection.json

# Only after the user presses Run for this snapshot; this may consume capacity.
backfill run-app --connection /private/path/app-connection.json --json selected-task.json

# Inspect a saved result using the Backfill task_id from the stream.
backfill app-status --connection /private/path/app-connection.json --task-id BACKFILL_TASK_ID
```

Invoke once, through an argv array (never a constructed shell command), and
consume stdout as JSONL. The connection path stays on the execution host. No
connection/pairing, credentials, schedule or polling is created by these commands.
Dry-run reports validation and limits without echoing instructions or context;
it does not check folder existence, connection health or quota admission.

Every progress/result event carries `request_id` and Backfill `task_id`; when a
snapshot is supplied it also carries `source_task_id`. An initial progress event
reports `state: running` after admission, before starting the guarded executor.
Existing `type`, `state`, `output`, `provider` and `reason` fields are preserved.

The Task Finder runner/consumer must check that these IDs match its selected
task/run before writing. Keep its existing lease authentication for updates.
Update run metadata for progress, and append a bounded Markdown task update for
a result, plus validated structured PR/output links. Task Finder assigns update
IDs, time, actor and origin; those are never accepted from model output. Output
is untrusted text and does not authorize actions or prove linked PRs exist.

| Backfill result | Task Finder consumer action |
| --- | --- |
| `review` or previously approved `done` | Save output; `awaiting_review` for this task |
| `waiting` | Save quota/connection reason; user can manually Run later |
| `failed` | Save reason/output; user reviews before retry |
| `paused`, `cancelled`, permission approval reason | Save output; request human input |

Do not mark the task complete or accept an approval in the consumer. Backfill's
existing owner-authenticated approval/revision action remains the review path;
approval never publishes, merges or deploys. An app credential has no approval
endpoint. Revisions require human feedback and another manual Run; there is no
automatic retry loop here. DevDock supplies the environment; Shiplog can report
the resulting Task Finder evidence. Slate is unnecessary for this slice.

## Verification limits

Tests use mock credentials/transports and deterministic native executors. They
cover context correlation, review/revision, idempotency, preserved account/quota
guards, malformed/oversized payloads, revoked credentials and read/edit boundaries.
A mocked Task Finder consumer receives the same task's result without reexecution.
No real model request, live Task Finder task write, new credential, service setup,
production deployment, merge or publication is part of verification.
