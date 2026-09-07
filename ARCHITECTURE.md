# Architecture

Backfill has a small task application above an independently usable quota core.

- `tasks.py`: durable task, project, review, schedule, and percentage-allocation state.
- `execution.py`: one queued task at a time, driven through native guarded transports.
- `quota.py`, `forecast.py`: account windows, personal reserves, safe admission.
- `governor.py`, `guard.py`, `watchdog.py`: native accounting and process interruption.
- `meter.py`, `providers/`: account telemetry with identity and freshness checks.
- `main.py`: authenticated v1 quota API, v2 task API, and the static dashboard.

The task application can be disabled with `BACKFILL_AUTOMATION_ENABLED=false`.
The quota API and guards remain usable by independent callers. Task discovery and
external workflow definitions submit actual work through the same API as the UI.

## State

SQLite stores account observations, grants, native usage, projects, task instructions,
attempts, results, review feedback, and schedules. Existing v1/v2 quota databases are
preserved. Task tables are additive. The old `backfill.db` is never rewritten.

A task moves from queued or waiting to running, then review or needs-attention.
A reviewed one-off task is complete. Reviewed repeating tasks receive their next due
time, skipping missed occurrences. A revision queues a new attempt with the previous
result and feedback. A restart marks unfinished attempts interrupted instead of
silently duplicating work. Task creation never starts paused.

Account percentages are distinct from native tokens. Task and project percentage
charges use account movement for the matching provider window/reset. The process
monitor checks current allocations while native guards independently enforce fresh
account reserves and bounded execution. Unknown native spending retains its hold.

## Security

The server binds loopback and a private Unix socket, with owner credentials and
worker-scoped credentials. Browser sessions are HttpOnly, SameSite, and same-origin
for mutations. The dashboard renders result text without executing model-provided HTML.
Native executor permissions remain enabled. Results are reviewable; approval does not
perform external writes. A process watchdog survives a crashed wrapper.
