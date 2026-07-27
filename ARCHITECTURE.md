# Architecture

## Product boundary

Backfill is a capacity-aware batch scheduler for coding-agent jobs. It does not
replace Codex, Claude Code, CodexBar, GitHub, or an issue tracker. It coordinates
them and keeps a durable record of why a job did or did not run.

## Components

### Identity and control plane

FastAPI serves the private dashboard and typed JSON API on loopback. Tailscale
Serve provides tailnet-only HTTPS, removes caller-supplied identity headers,
and adds `Tailscale-User-Login` for the authenticated user. Backfill requires
that login to equal one configured identity.

All state-changing browser requests must also carry the dashboard marker and
the configured HTTPS origin. The backend never listens on the LAN or tailnet
interface, so remote callers cannot bypass the identity-injecting proxy.

### Durable state

SQLite stores tasks, runs, event lines, provider observations, and scheduler
control state. The scheduler marks an in-flight run blocked after a daemon
restart rather than pretending that it continued.

### Capacity plane

Provider adapters produce a common snapshot:

- readiness and freshness;
- account and plan identity;
- every quota window the provider actually exposes, normalized by duration;
- reset timestamps;
- source and error provenance.

Codex uses `account/rateLimits/read` through `codex app-server`. Claude uses
CodexBar's structured CLI output. Every collector has a hard timeout. The last
good result may be shown as stale for ten minutes, but stale data is never
eligible for scheduling.

### Scheduler

Jobs are inspected in descending priority, then creation order. A provider fits
when:

```text
min(reported window remaining - that window's protected reserve)
    >= task estimated quota cost
```

Task-specific minimums can raise either reserve. Missing windows are not
invented. Among fitting providers, Backfill chooses the one with the most
headroom remaining after the estimate.

### Execution plane

The runner rejects dirty repositories, fetches the configured canonical primary
branch, and creates one branch/worktree per task. Codex runs with
`workspace-write` sandboxing and no approval prompts. Claude execution remains
off until its allowed-tool policy is reviewed on the actual Devbox.

An exit status of zero means `review`, not `done`: tests and changes are visible,
but a human still owns committing, pushing, and merging.

## State machine

```text
queued ──dispatch──> running ──exit 0──> review
  │                    │  └──exit != 0──> failed
  │                    ├──safety check──> blocked
  │                    ├──owner hold────> paused
  │                    └──owner cancel──> cancelled
  └<────────────── retry / requeue ───────┘
```

## Deliberate first-version limits

- One concurrent run by default.
- No automatic commits, pushes, PRs, merges, or worktree deletion.
- No mid-turn provider switching. Provider choice occurs at a task boundary.
- Quota-cost estimates are supplied per task; historical calibration is a
  later scheduling refinement.
- No issue-tracker ingestion yet. Jobs are created in the dashboard or API.
