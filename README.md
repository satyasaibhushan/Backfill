# Backfill

Backfill fits high-value engineering jobs into spare Claude and Codex capacity
without consuming the headroom reserved for interactive work.

It runs as a loopback-only service on Devbox:

```text
Tailscale user → private HTTPS Serve proxy → Backfill on 127.0.0.1
                                           ├─ quota collectors
                                           ├─ priority scheduler
                                           ├─ isolated worktrees
                                           └─ Codex / Claude CLIs
```

Tailscale Serve strips spoofed identity headers and injects the authenticated
tailnet login. Backfill then compares that login against one exact allowlisted
identity. There is no public route and no Cloudflare dependency.

Backfill never commits, pushes, opens a pull request, or merges. A successful
run stops in `review` with its changes left in an isolated Git worktree.

## Current scope

- Private dashboard with a single Tailscale-login allowlist.
- Loopback-only origin with exact-origin checks for state-changing requests.
- Persistent SQLite task queue and run/event history.
- Codex rate limits through the official `codex app-server` RPC.
- Claude rate limits through the CodexBar Linux CLI.
- Conservative provider selection using every quota window a plan exposes.
- Clean-repository check and branches cut from the configured upstream primary branch.
- Scheduler pause/resume, manual dispatch, task cancellation, and restart recovery.
- Codex execution inside its `workspace-write` sandbox.
- Claude telemetry is enabled; Claude execution is opt-in pending an explicit
  Devbox permission policy.

## Local development

```bash
uv sync --group dev
```

Create `.env`:

```dotenv
BACKFILL_AUTH_MODE=dev
BACKFILL_ALLOWED_LOGIN=owner@example.com
BACKFILL_SCHEDULER_ENABLED=false
```

Then run:

```bash
uv run fastapi dev
```

Open `http://127.0.0.1:8000`. Development authentication is accepted only on
loopback.

Probe provider capacity without starting the dashboard:

```bash
uv run backfill probe
```

## Devbox configuration

Backfill fails closed when its Tailscale identity configuration is incomplete.
The production environment file belongs at
`~/.config/backfill/backfill.env` with mode `0600`:

```dotenv
BACKFILL_AUTH_MODE=tailscale
BACKFILL_ALLOWED_LOGIN=satyasaibhushan@github
BACKFILL_PUBLIC_ORIGIN=https://devbox-mark-one.tail6a992e.ts.net

BACKFILL_SCHEDULER_ENABLED=false
BACKFILL_SESSION_RESERVE_PERCENT=20
BACKFILL_WEEKLY_RESERVE_PERCENT=15
BACKFILL_ENABLE_CODEX_EXECUTION=true
BACKFILL_ENABLE_CLAUDE_EXECUTION=false
```

Keep the scheduler disabled through initial provider and repository validation.
Enable it only after a manual probe and one intentionally queued small job both
look correct.

See [Devbox setup](docs/DEVBOX_SETUP.md) for deployment, Tailscale Serve, and
the controlled enablement sequence.

## Task contract

Each task records:

- repository path on Devbox;
- upstream remote and primary branch;
- explicit branch name;
- priority from 0–100;
- provider preference;
- estimated quota cost;
- runtime ceiling;
- instructions and definition of done.

Before execution Backfill:

1. rejects a dirty repository;
2. verifies the canonical remote;
3. fetches its primary branch;
4. rejects prohibited attribution terms in the branch name;
5. creates a dedicated worktree and branch from `upstream/<primary>`;
6. runs one provider without permission prompts;
7. records events and leaves all changes uncommitted for review.

## Commands

```bash
backfill serve   # dashboard + scheduler
backfill probe   # provider snapshots as JSON
backfill --version
```

## Data

By default Backfill stores:

```text
~/.local/share/backfill/
├── backfill.db
└── worktrees/<task-id>/
```

No provider credentials are copied into Backfill. Codex and CodexBar read their
own existing local authentication.

## Development checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
