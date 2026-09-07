# Hosted Backfill

The website runs on Vercel with a dedicated Postgres database. A Linux worker runs the existing local task service and a separate outbound HTTPS connection. No inbound worker port is exposed.

## First use

1. Open the private setup link and choose a password of at least 12 characters. Setup closes permanently after the account is created.
2. Sign in and choose **Connect machine**.
3. Copy the command and run it on the Linux account that owns your model logins and working folders.
4. The website shows the machine's first heartbeat, then its provider readings.

Pairing codes expire after ten minutes and can pair one machine. Machine credentials are stored only in the worker's private data directory; the website stores their hashes. The website never receives provider login credentials. **Disconnect** revokes the machine credential. Re-pairing requires a new command.

The installer requires Python 3.12 or later, curl, and user systemd. It installs a private virtual environment, the worker, and a checksum-verified Linux quota reader. It enables `backfill.service` and `backfill-connection.service`. The login manager must permit the user's services to stay running after logout. Installation reports a persistence failure rather than claiming that the machine will remain online.

Model CLIs must be installed and signed in under that Linux user. Installing Backfill does not sign into model accounts. Missing quota readings block the affected provider.

## Connection behavior

The connection checks in every ten seconds while idle. The website considers a machine offline after 45 seconds without a heartbeat. Reconnect uses bounded backoff. Local task execution and quota enforcement remain independent of Vercel function lifetimes.

Changes are written to a durable hosted inbox. The worker applies each command with its local receipt in one SQLite transaction. If the response is lost, replay returns the receipt instead of repeating the task change. Cloud acknowledgements are scoped to the paired machine. Pending work is not reassigned automatically to a replacement machine.

A disconnected worker starts no new queued tasks once its 45-second connection lease expires. Active tasks continue under local quota guards. Results and progress sync when the connection returns. Task details are cached on the website for review while the worker is offline.

The first version supports one connected machine and one password account. Existing laptop tasks are not automatically migrated. Current snapshot transport is bounded by the platform's request size; very large histories need incremental artifact storage before use at that scale.

## Deployment

The project is linked through the Vercel CLI. Production needs `DATABASE_URL`, `BACKFILL_PUBLIC_URL`, and `BACKFILL_SETUP_TOKEN`. The setup token is a private random value used only to create the first password account. Password hashes and sessions persist in Postgres across deployments.

Run `python deploy/build-worker.py` before deploying with `vercel deploy --prod`. The build helper uses uv to create the worker wheel. `.vercelignore` includes that generated download and excludes local credentials, caches, and databases. The wheel is not committed.

Validate `/health`, unauthenticated access, the login page, and `/worker.whl` after deployment. A READY build alone does not establish that the function starts successfully.
