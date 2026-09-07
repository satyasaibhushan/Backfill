# Backfill

A private workspace for work you want done in the background.

Write the instructions, choose a project, and leave it in the queue. Backfill runs
it when account capacity allows, saves the result, and brings it back for review.
Request a revision with feedback or approve the result. Approval never publishes,
merges, or deploys changes.

## What the product does

- Real tasks with instructions, a working folder, priority, and saved results.
- Automatic selection between the two connected subscription accounts.
- Shared project allowances and a personal reserve, expressed as percentages.
- One-time, daily, and weekly work. Repeated work waits for review before repeating.
- Pause one task or all work until a date. Pausing is a management action.
- Persistent queue and run history. Interrupted work needs attention after restart.
- The same task submission API for the dashboard, CLI, and other automation tools.

The main screen shows Session and Weekly headroom. Internal provider bucket names,
native token accounting, and provider-specific spending switches stay out of task forms.

## Run

Requires Python 3.12+ and the signed-in native executors. Account limits come from
Codex's installed app-server and the configured CodexBar reader for Claude.

```sh
uv sync --locked
backfill serve
backfill dashboard
```

The dashboard listens on `127.0.0.1:8431`. The API also uses a private Unix socket.
A dashboard link creates an eight-hour local session; credentials never appear in
the page. For a devbox, use an authenticated SSH tunnel. See
[deployment](docs/DEVBOX_SETUP.md).

## Submit work from another tool

```sh
backfill task --json task.json
```

```json
{
  "title": "Summarize this project",
  "instructions": "Read the project and explain its purpose, main components, and open questions. Cite files.",
  "folder": "/path/on/execution/host",
  "priority": "normal",
  "provider": "auto",
  "allowance": 5,
  "access": "read",
  "schedule": "once"
}
```

The corresponding owner-authenticated HTTP endpoint is `POST /v2/tasks`.
`GET /v2/tasks/{id}` returns instructions, current state, output, and run history.
`POST /v2/tasks/{id}/actions` accepts `pause`, `resume`, `retry`, `revise`, `approve`,
and `cancel`. Revisions include `feedback`; pauses may include an ISO timestamp in `until`.

External executors can also use the independent quota API and native process guards.
See [quota API](docs/QUOTA_API.md) and [guarded execution](docs/GUARDED_EXECUTION.md).

## Allowances and permissions

The default personal reserve is 30%. A task may use up to 5 percentage points of
each current quota window; a project defaults to 10. These are **estimated** from
account movement while the task runs. Foreground usage on the same account is
charged conservatively too. Native token counters are a separate internal safeguard,
not a conversion into subscription percentages.

Fresh readings and a safety buffer govern admission. Running work stops at a quota
boundary, pause, missing telemetry, or internal execution limit. An already-running
request can overshoot. One background task runs at a time. Delayed provider readings
mean this is conservative quota control, not an exact billing limit.

Research tasks use read tools. Editing tasks use the working folder and the native
executor's permissions. Backfill never enables an approval-bypass mode. Native
permission denials appear as tasks needing attention. Use dedicated working folders
for changes; Backfill does not create or manage repository worktrees.

## Development

```sh
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests cover quota accounting, native process termination, durable scheduling,
percentage ceilings, permissions, and the submit → execute → review → revise flow
through both real guard transports with deterministic executors.
