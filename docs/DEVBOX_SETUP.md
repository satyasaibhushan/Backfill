# Deployment

Backfill runs on the host whose working folders and native logins it uses. Choose
one active service for a queue; do not run separate copies against the same accounts.
The dashboard is loopback-only, on port 8431. Never expose its listener publicly.

## macOS

Install the locked environment with `uv sync --locked`, then run
`python3 deploy/install-macos.py` from the checkout. The installer creates the
`com.backfill.service` LaunchAgent and uses `~/.local/share/backfill` for persistent
private state. It preserves existing data. Logs are in that directory's `service.log`.
The service starts at login and restarts after a crash. Work stops while the Mac
is asleep or logged out. This is not an always-on remote host.

Run `.venv/bin/backfill dashboard` to open a private session. The one-use link expires
in 60 seconds; the browser session lasts eight hours. Check `/health` and both account
cards after installation. Submit a small read-only task and verify its saved result.

For an update, pause work, wait for the running task to stop, update the checkout,
install its locked environment, and rerun the installer. Interrupted work retains
partial output and requires an explicit retry. Back up the data directory while the
service is stopped before a schema-changing update.

## Linux / devbox

Use a real, writable checkout path owned by the execution user. The included user
unit assumes `~/apps/backfill`; change it to the verified path on your host. Install
with `uv sync --locked --no-dev`, copy the environment example to
`~/.config/backfill/backfill.env`, and set native executable paths for that user.
Copy the unit to `~/.config/systemd/user/backfill.service`, reload the user manager,
and enable/start it. An administrator must enable user lingering for execution after
SSH logout. Verify persistence after disconnecting before calling this deployed.

Both native executors and a working quota reader must be installed and authenticated
as the service user. A signed-in status alone does not prove quota access. Check fresh
meter readings as well as a bounded task. Missing or incomplete telemetry holds work.
Do not copy login secrets into the environment file, substitute zero usage, or run
another reader against the same binding.

Connect through an authenticated SSH tunnel to port 8431. Generate the dashboard link
on the execution host and open it through that tunnel. Folder paths in tasks refer to
the remote host. `BACKFILL_DASHBOARD_PORT=0` disables TCP and retains the private socket.

Meter refresh defaults to 45 seconds. The 120-second settlement buffer estimates
provider reporting lag; it is not a publication guarantee. See
[guarded execution](GUARDED_EXECUTION.md) for accounting and interruption boundaries.
