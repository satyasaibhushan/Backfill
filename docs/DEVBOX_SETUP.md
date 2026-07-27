# Devbox setup

Backfill is private to the tailnet. It does not use Cloudflare, a public DNS
record, or a Google OAuth application.

## Identity

Devbox's current Tailscale identity is:

```text
satyasaibhushan@github
```

Tailscale Serve injects that identity into proxied requests. Backfill accepts
only the exact value configured in `BACKFILL_ALLOWED_LOGIN`.

## 1. Prepare Backfill

Devbox requires Python 3.12 or newer, Git, Codex, Claude, and CodexBar.

```bash
git clone git@github.com:satyasaibhushan/Backfill.git ~/Backfill
cd ~/Backfill
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install .
```

Install CodexBar's Linux CLI from its official release tarball or Homebrew
formula, then verify:

```bash
codexbar usage --provider claude --source oauth --format json
```

Codex and Claude must be signed in as the accounts whose capacity Backfill may
consume.

## 2. Configure Backfill

```bash
mkdir -p ~/.config/backfill
cp ~/Backfill/deploy/backfill.env.example ~/.config/backfill/backfill.env
chmod 600 ~/.config/backfill/backfill.env
```

Keep these safety switches during initial validation:

```dotenv
BACKFILL_SCHEDULER_ENABLED=false
BACKFILL_ENABLE_CLAUDE_EXECUTION=false
```

## 3. Install the user service

```bash
mkdir -p ~/.config/systemd/user
cp ~/Backfill/deploy/backfill.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now backfill.service
```

Verify the loopback origin:

```bash
curl -s http://127.0.0.1:8430/api/health
systemctl --user status backfill.service
```

API routes other than health intentionally reject direct loopback requests
because they do not carry a Tailscale identity.

## 4. Publish privately with Tailscale Serve

```bash
tailscale serve --bg 8430
tailscale serve status
```

The dashboard becomes available only inside the tailnet at:

```text
https://devbox-mark-one.tail6a992e.ts.net
```

Tailscale access rules still apply. Funnel must remain disabled.

## 5. Controlled first dispatch

1. Run `.venv/bin/backfill probe`.
2. Create one small task against a clean, disposable repository with `upstream`
   configured.
3. Keep Claude execution disabled.
4. Enable the scheduler, restart the service, and use **Dispatch now**.
5. Inspect the worktree, branch, transcript, and quota movement.
6. Pause the scheduler again before enabling unattended operation.
