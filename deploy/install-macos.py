"""Install the current checkout as a per-user background service on macOS."""

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

if sys.platform != "darwin":
    raise SystemExit("This installer requires macOS.")
repo = Path(__file__).resolve().parents[1]
executable = repo / ".venv/bin/backfill"
if not executable.is_file():
    raise SystemExit("Install the locked environment before installing the service.")
root = Path.home() / ".local/share/backfill"
root.mkdir(parents=True, exist_ok=True, mode=0o700)
root.chmod(0o700)
label = "com.backfill.service"
agent = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
agent.parent.mkdir(parents=True, exist_ok=True)
if agent.exists():
    existing = plistlib.loads(agent.read_bytes())
    if existing.get("ProgramArguments", [None])[0] != str(executable):
        raise SystemExit("An installation from another checkout already exists.")
config = {
    "Label": label,
    "ProgramArguments": [str(executable), "serve"],
    "WorkingDirectory": str(repo),
    "EnvironmentVariables": {
        "PATH": (
            f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
            "/usr/bin:/bin:/usr/sbin:/sbin"
        ),
        "BACKFILL_DATA_DIR": str(root),
        "BACKFILL_DASHBOARD_PORT": "8431",
        "BACKFILL_AUTOMATION_ENABLED": "true",
        "BACKFILL_METER_ENABLED": "true",
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "Umask": 0o077,
    "StandardOutPath": str(root / "service.log"),
    "StandardErrorPath": str(root / "service.log"),
}
agent.write_bytes(plistlib.dumps(config))
agent.chmod(0o600)
domain = f"gui/{os.getuid()}"
subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
# The service may remain registered briefly while its old process shuts down.
for attempt in range(10):
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(agent)], capture_output=True, text=True
    )
    if result.returncode == 0:
        break
    if attempt == 9:
        raise SystemExit(result.stderr.strip() or "Service installation failed.")
    time.sleep(1)
print("Background service installed. Open Backfill through its dashboard command.")
