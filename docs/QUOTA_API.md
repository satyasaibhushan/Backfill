# Backfill

Backfill has an account meter for both providers, task/project budget enforcement,
and a private local dashboard. Native executor adapters enforce changes during runs.
See [guarded execution](docs/GUARDED_EXECUTION.md) for the current integration contract,
known interruption limits, and examples. The older reservation API below remains supported.

```sh
uv sync
uv run backfill serve
# In another terminal:
uv run backfill dashboard
```

The dashboard shows both account windows, task and project token ceilings, native runs,
reserved/uncertain consumption, and pause-until controls. Account meters refresh every
45 seconds. Budgets and priority belong to the owner; task credentials cannot change them.


Backfill allocates shared model quota to cooperating workloads. A caller supplies
a workload key and an estimated cost, receives a short-lived reservation, and
reports usage afterward. Backfill protects interactive capacity using usage
history, a minimum reserve, and safety buffers.

It is a local Python service, SQLite ledger, JSON CLI, and quota dashboard. Task discovery,
cron, prompts, workflows, repositories, browser verification, and code-review UIs belong
to its callers.

## Try it

Requires Python 3.12+ on Linux or macOS.

```sh
uv sync --locked --group dev
uv run backfill demo
```

The deterministic demo uses the production ledger and an isolated temporary database.
It makes no provider requests. Its JSONL output shows:

- spare capacity admitting a large background summary;
- a low-priority request waiting while important work still fits;
- increased interactive usage holding bulk work back;
- stale readings blocking new grants;
- a session reset leaving the weekly constraint intact;
- a fresh weekly reset allowing work again;
- owner pause and different reserves learned from quiet and busy histories.

## Start the service

```sh
uv run backfill init
uv run backfill serve
```

The default data directory is `~/.local/share/backfill`, mode `0700`. The API uses
`quota.sock`, mode `0600`, and an authenticated loopback dashboard at
`http://127.0.0.1:8431/`. Run `backfill dashboard` to open a one-use login link.
Set `BACKFILL_DASHBOARD_PORT=0` to disable the TCP listener. An existing directory
with broader permissions is rejected without changing it. Choose a new private data
directory with `--data-dir` or `BACKFILL_DATA_DIR` when needed.

`owner.token` controls policy and registration. Worker credentials only access their
own workload. Do not distribute the owner credential to workers. Programs running
as the same unrestricted Unix user can still read that user's files; scoped tokens
are API authorization, not an operating-system sandbox.

## Owner interface

Commands return one JSON object; `demo` returns JSONL. JSON bodies come from stdin
by default, or `--json file.json`. All global flags precede the command.

```sh
printf '%s\n' '{"policy":{"timezone":"Asia/Kolkata"}}' | backfill account personal
printf '%s\n' '{"account":"personal","priority":10}' | backfill workload history-summary
printf '%s\n' '{"account":"personal","priority":90}' | backfill workload task-finder
backfill probe codex --account personal
backfill status
```

Workload registration returns a credential **file path**, never the token in CLI
output. Its default location is `DATA_DIR/credentials/KEY.token`. Supply
`--token-out /private/path` to choose another new file. The HTTP registration endpoint
returns the new token once, for callers that manage credentials themselves.
Updating an existing workload preserves its credential. `rotate-token KEY --token-out
NEW_FILE` invalidates the previous token.

Priority is fixed by the owner, from 0 to 100. Higher priorities can access more of
the priority buffer. This is admission control, not a task queue: existing
reservations are not seized from other workers. Workloads of equal priority acquire
available capacity in arrival order. Swarm children share their parent's workload
key and credential but must use unique request IDs.

`account` and `workload` replace the corresponding policy/configuration. Omitted
fields return to documented defaults. To pause one workload, set its existing account,
priority, and `paused: true`; to pause all workloads on an account, set that account's
policy `paused: true`. Unpause explicitly. Scheduling a future unpause belongs to the
caller's scheduler. Changing a workload's account is rejected; use a new key.

## Executor interface

The key is an accounting label, not a provider API key. Keep provider authentication
in the executor. Backfill neither proxies model traffic nor starts processes.

```sh
printf '%s\n' '{"request_id":"summary-part-17","costs":{"codex.primary":0.5,"codex_bengalfox.primary":0,"codex_bengalfox.secondary":0},"ttl_seconds":60}' |
  backfill --credential /private/history-summary.token acquire history-summary
```

Window names come from `status` or `probe`; do not hard-code this example's names.
Include **every observed window** in `costs`. Use zero only for a bucket the selected
model will not consume. Costs use each window's explicit `unit`. For subscription
readers, one `quota_points` unit is one percentage point of that window's limit.
Tokens and percentage points are not interchangeable. The executor supplies
conservative per-window cost estimates and constrains each request accordingly.

An accepted response contains `decision: "granted"`, `grant_id`, `expires_at`, and
the headroom calculation. `decision: "wait"` gives a machine-readable reason.
CLI exit codes: `0` success/granted, `2` wait, `1` input, authorization, or service error.
A denial's retry time is a suggested recheck, not a promise of capacity.

The executor must:

1. Acquire before a bounded unit of work. No service connection means no new spending.
2. Keep request IDs unique across children and persist the grant ID before starting.
   Retry a lost response with the same request ID and identical parameters.
3. Stay within the reservation and finish/report before expiry. Use short model
   requests, request limits, and executor-owned cancellation. Do not launch a long
   autonomous session under a small reservation.
4. Check `status KEY --grant GRANT_ID` before starting further work on an existing grant. Stop starting
   requests when its `can_spend` becomes false. Policy changes cannot cancel an
   already in-flight request.
5. Report cumulative per-window consumption, with a unique report ID. Partial reports
   set `final: false`; the default final report releases the unused reservation.
6. Acquire a new grant for the next unit. Backfill never silently renews an expired grant.

```sh
printf '%s\n' '{"report_id":"summary-part-17-done","consumed":{"codex.primary":0.3,"codex_bengalfox.primary":0,"codex_bengalfox.secondary":0},"final":true}' |
  backfill --credential /private/history-summary.token report history-summary GRANT_ID
```

`release KEY GRANT_ID` attests there is no remaining in-flight or unreported spending.
It releases only unused allowance, retaining previously reported consumption.
A crashed worker's expired reservation stays provisionally charged until a final
report resolves it or its quota window resets. Release cannot erase expired uncertainty.
Actual overruns are accepted, charged, and returned in `overrun`; hiding them would
make subsequent admission unsafe. Finalized reports cannot be revised.

Retries are idempotent. Reusing an acquire/report ID with different parameters is a
conflict. Denials create no reservation, so the same denied request can be retried.
Report IDs are scoped to a grant; acquire IDs are scoped to the workload.

## Observations and adaptation

`probe codex` uses the existing signed-in app-server quota API. `probe claude` uses
CodexBar's configured quota strategy by default. Neither sends a model request. The service
refreshes its configured bindings automatically. `probe PROVIDER --account KEY` is
a one-shot diagnostic or an integration point for externally supplied observations.
Do not schedule it alongside the service's automatic meter for the same binding.
Provider credentials are not stored in Backfill; account identity is fingerprinted.

For another provider, `observe ACCOUNT --json snapshot.json` accepts:

```json
{
  "observed_at": "2026-09-07T09:00:00Z",
  "covered_through": "2026-09-07T08:58:00Z",
  "source": "my-meter",
  "source_account": "stable-account-fingerprint",
  "measurement": "measured",
  "windows": [{
    "name": "weekly", "unit": "tokens", "limit": 1000000, "used": 250000,
    "resets_at": "2026-09-14T09:00:00Z", "duration_seconds": 604800
  }]
}
```

`covered_through` means the cumulative reading includes spending reported **before**
that instant. Later debits are subtracted separately to avoid double spending while
telemetry catches up. Collectors cannot establish that coverage exactly for subscription
percentages. They apply a configurable settlement delay, default 120 seconds, and mark
the entire observation `estimated`. This is conservative accounting, not a guaranteed
hard cap on the upstream subscription.

Every window is considered independently. Missing, invalid, stale, or incomplete
readings deny admission. A changed reset timestamp before the old reset is confirmed
carries existing spending and reservations forward; it cannot create free quota.
Resetting one window does not replenish another. Changed account identity, omitted
known windows, or changed units/capacity require investigation and a new account key
where appropriate. Do not run duplicate keys against the same underlying quota pool.

Default policy:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `cold_start_user_percent` | 20 | Reserve before sufficient history |
| `minimum_user_percent` | 5 | Reserve floor, even on quiet days |
| `safety_percent` | 5 | Allowance for measurement and forecast error |
| `priority_percent` | 15 | Capacity progressively accessible to higher priorities |
| `forecast_multiplier` | 1.25 | Extra margin on predicted interactive demand |
| `history_min_samples` | 12 | Minimum usable intervals, across at least three days |
| `history_days` | 28 | History retention and forecast horizon for samples |
| `snapshot_ttl_seconds` | 180 | Maximum observation age; coverage can lag up to twice this |
| `max_grant_seconds` | 120 | Maximum reservation lifetime, also capped at the earliest reset |
| `timezone` | UTC | Local hour and weekday/weekend grouping |
| `paused` | false | Deny new spending for the account |

History measures observed usage growth minus reported window-unit workload usage.
Intervals containing guarded native runs are excluded because tokens cannot be
subtracted from subscription percentages. The remainder
is **unattributed demand**, which may include other unmanaged processes, not just you.
Intervals spanning a reset or more than six hours are excluded. With sufficient history,
an upper-quartile rate, grouped by local hour and weekday/weekend when enough samples
exist, estimates demand until reset. The reserve uses that forecast plus the multiplier
and minimum floor. Remaining capacity also reacts immediately to each new usage reading.
Forecasts cannot anticipate plans you have not communicated; the owner can change the
policy or pause workloads. No extra calendar or task scheduler is built in.

## HTTP contract

All endpoints require `Authorization: Bearer TOKEN` over the private socket.

| Method and path | Credential |
| --- | --- |
| `GET /v1/status` | Owner |
| `PUT /v1/accounts/{key}` | Owner |
| `POST /v1/accounts/{key}/observations` | Owner |
| `PUT /v1/workloads/{key}` | Owner |
| `POST /v1/workloads/{key}/rotate-token` | Owner |
| `GET /v1/workloads/{key}` | Owner or that workload |
| `POST /v1/workloads/{key}/acquire` | Owner or that workload |
| `GET /v1/workloads/{key}/grants/{id}` | Owner or that workload |
| `POST /v1/workloads/{key}/grants/{id}/report` | Owner or that workload |
| `POST /v1/workloads/{key}/grants/{id}/release` | Owner or that workload |

Status exposes grant state, current permission, and each reserve/buffer component.
It shows the latest 50 grants for a workload. HTTP admission denials return 200 with
`decision: "wait"`; malformed bodies return 422, authentication 401/403, and conflicts 409.

## Upgrade and validation

Version 0.2 replaces the former task runner. The dashboard manages quota and native budgets. It uses a new `quota.db`;
legacy `backfill.db`, repositories, worktrees, and run artifacts are untouched. Old task
API clients are not compatible. Stop the old service before adopting the new service
unit. There is no automatic task migration or live deployment.

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests include a real local socket server and CLI subprocesses, concurrent reservation
attempts, restart persistence, worker authorization, moving/resetting quota windows,
late reports, idempotency, and provider subprocess timeout cleanup.

See [architecture](ARCHITECTURE.md) and [devbox setup](docs/DEVBOX_SETUP.md).
