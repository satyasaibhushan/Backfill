# Guarded execution

Backfill ships two execution adapters. They attach quota control to native runtimes;
callers still own prompts, scheduling, working directories, permissions, and context.
A workload key identifies a budget. It is not a provider API credential.

## Account meters

The service refreshes both providers every 45 seconds without making model requests.
Codex uses `account/read` and `account/rateLimits/read` over its installed app-server.
Claude uses the installed CodexBar configured reader, honoring its existing sign-in.
CodexBar handles browser-session or CLI access; Backfill does not copy credentials.
The SDK's in-flight usage events do not replace a complete, idle account-limits reading.
Leave `BACKFILL_CLAUDE_QUOTA_SOURCE` unset to honor CodexBar preferences; `auto`,
`oauth`, `cli`, and `web` explicitly select a strategy. If the CLI reading omits
identity, Backfill verifies the native subscription identity with `claude auth status`.
The reader timeout is 45 seconds to allow the CLI usage screen to load.

The host must already have the corresponding subscription login. Guards reject API
credential and provider-routing overrides so an account meter does not govern spending
on a different billing route. Failed, incomplete, or stale
readings block new runs and revoke current permission. The dashboard keeps the last
reading visible with an unavailable label. It never substitutes zero usage.

A configured settlement delay is a conservative heuristic, not provider-confirmed
coverage. Native token counts and subscription quota percentages are separate ledgers.
Backfill does not claim that a fixed number of tokens equals one percentage point.
Intervals containing native background work are excluded from personal-demand training.
Unattributed usage is not proof of human usage.

## Configure a project and task

Create these through the dashboard, or pipe JSON to the CLI:

```sh
backfill project history --json project.json
backfill workload github-history --json task.json
backfill budget github-history --json budget.json
```

`project.json`:

```json
{"token_limit": 2000000}
```

`task.json`:

```json
{"account": "claude", "priority": 20}
```

`budget.json`:

```json
{
  "project": "history",
  "token_limit": 500000,
  "run_token_limit": 100000,
  "max_run_seconds": 600,
  "run_cost_usd": 1
}
```

Total token limits are lifetime ceilings across runs, until the owner increases them.
There is no automatic spending reset on a process restart. Projects share a ceiling
across their tasks and providers. Native counts include cached input; they are not
price-normalized. The cost limit is an additional native estimated-dollar cap for
either provider when reported, not authoritative billing or a subscription-percentage conversion.
An optional `reference_cost_limit` caps cumulative reported cost for one workload across
resumes. It is a lifetime amount, not a weekly allowance. The automatic subscription
calibration and model-price reporting are not yet connected to this enforcement path.

`max_run_seconds` is optional. Dashboard and application tasks have no automatic time
cutoff. Budget waits retain their context and do not become failures after three attempts.

The caller can use the local owner credential or a scoped task credential file.
Workers cannot raise their own budgets or change priority. The credentials generated
by `workload` are stored privately. Dashboard-created tasks can be run with the local
owner credential; use `rotate-token` when issuing a dedicated worker credential.

## Claude

```sh
backfill guard claude github-history < request.txt > events.jsonl
```

Optional native flags follow `--`, for example `-- --permission-mode default`.
Claude session continuation can use its native `--resume` flag; each SDK query
reports the new call's usage and the task ledger retains prior calls.
The guard supplies print mode, verbose streaming JSON, partial usage events, and
`--max-budget-usd`. Native stdout passes through unchanged. Guard decisions go to
stderr. Backfill stores counters and session IDs, not prompts or response text.

Message IDs deduplicate repeated usage. Partial output updates are counted during
execution. The final `modelUsage` result reconciles the full subagent tree. An
interrupted stream without complete final accounting retains the unused allocation
as uncertain. Resets and detached/remote execution are outside this adapter's contract.

## Codex

Configure the calling app-server client to launch:

```sh
backfill guard codex implementation-task
```

The guard launches the installed app-server and proxies its JSONL protocol. The
client sends initialize, thread/start, and turn/start as usual. It retains authority
over permissions, models, and prompts. Each turn/start is checked before forwarding.
Cumulative thread counters avoid charging duplicate notifications. A later guard can
resume threads previously metered under the same task key, using persisted baselines.
Importing an existing unmetered thread is refused because its baseline is unknown.

Native delegation features are disabled for guarded processes, and attempts to
re-enable them are refused. App-server descendants
do not have a verified complete usage subscription here. Caller-managed parallel
agents can register separate tasks under one shared project budget. Native compaction and goal commands pass through. Counter resets retain previously
counted tokens. Realtime sessions, forks, and rollback remain outside this adapter's
accounting contract.

## Enforcement contract

- Acquiring a run atomically reserves the smaller of task, project, and per-run
  remaining tokens. A repeated acquisition ID never authorizes another process.
- One native run per account limits simultaneous unreported spending. Priority controls
  access to the account's priority reserve; Backfill does not implement a task queue.
- The guard checks policy every second and before forwarded native turns. Pause,
  lower budget, stale meter, or exhausted headroom stops its private process group.
- A lost connection stops the guard after the local request timeout. No new allowance
  is granted based on a failed refresh. Explicitly configured process time limits also stop silent runtimes.
- Token and cost stops happen at observable usage boundaries. An in-flight request
  can exceed a ceiling before its usage arrives. This is **bounded execution with
  observed-usage interruption**, not a provider-side exact token or percentage cap.
- Missing final usage retains the full allocation. A late complete final report may
  reconcile it. Never clear uncertain holds just to make the dashboard look available.
- A worker deliberately bypassing the guard is outside enforcement. This is not a
  machine-wide inference firewall. An independent watchdog stops the owned process group if the guard dies or its
  time allowance expires. Provider requests already accepted can still finish remotely.

The dashboard shows active, closed, and uncertain runs. The raw API exposes the
measured overrun and refusal reason. Native executor authentication, detached-child
behavior, and provider updates must be validated on each deployment host.

## API contract

Owner routes: `PUT /v1/projects/{key}`, `PUT /v1/workloads/{key}/budget`,
`PUT /v1/pause`, `POST /v1/meters/refresh`, and existing account/workload setup routes.

Worker routes under `/v1/workloads/{key}`:

- `POST /runs` with `request_id` and `provider` acquires a run.
- `POST /runs/{id}/usage` sends a strictly increasing sequence and cumulative native
  `tokens`, optional estimated `cost_usd`, and session identity. A duplicate sequence
  must contain the identical payload. Final reports specify whether accounting is complete.
- `GET /runs/{id}` returns current permission without extending the heartbeat.

Legacy `/acquire` and `/report` remain available for cooperating integrations with
explicit window-cost reservations. Their supplied units are not inferred from native
model tokens. They do not replace the process guard.
