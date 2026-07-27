import asyncio
import json
import shutil
from datetime import UTC, datetime

from backfill.config import Settings
from backfill.providers.models import ProviderSnapshot, UsageWindow


class ClaudeCodexBarProvider:
    provider_id = "claude"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def probe(self) -> ProviderSnapshot:
        if not shutil.which(self.settings.codexbar_command):
            return ProviderSnapshot(
                provider_id=self.provider_id,
                ready=False,
                source="codexbar",
                error=f"{self.settings.codexbar_command} is not installed",
            )
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    self.settings.codexbar_command,
                    "usage",
                    "--provider",
                    "claude",
                    "--source",
                    "cli",
                    "--format",
                    "json",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                if stdout:
                    payload = json.loads(stdout)
                    if process.returncode == 0:
                        return self._parse(payload)
                    try:
                        return self._parse(payload)
                    except RuntimeError as error:
                        raise RuntimeError(str(error)) from error
                if process.returncode != 0:
                    detail = stderr.decode(errors="replace").strip()[-1_000:]
                    raise RuntimeError(detail or f"codexbar exited with {process.returncode}")
                raise RuntimeError("codexbar returned no output")
        except (TimeoutError, OSError, RuntimeError, json.JSONDecodeError) as error:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                ready=False,
                source="codexbar",
                error=str(error) or "Claude quota probe timed out",
            )

    def _parse(self, payload: object) -> ProviderSnapshot:
        if isinstance(payload, list):
            candidate = next(
                (
                    item
                    for item in payload
                    if isinstance(item, dict) and item.get("provider") == "claude"
                ),
                payload[0] if payload else {},
            )
        else:
            candidate = payload
        if not isinstance(candidate, dict):
            raise RuntimeError("codexbar returned an invalid Claude payload")
        if candidate.get("error"):
            error = candidate["error"]
            if isinstance(error, dict):
                raise RuntimeError(str(error.get("message") or error))
            raise RuntimeError(str(error))

        usage = candidate.get("usage") or {}
        windows: list[UsageWindow] = []
        for name, key in (("session", "primary"), ("weekly", "secondary")):
            value = usage.get(key)
            if parsed := self._parse_window(name, value):
                windows.append(parsed)
        for value in usage.get("extraRateWindows") or []:
            if isinstance(value, dict):
                name = str(value.get("title") or value.get("id") or "extra")
                if parsed := self._parse_window(name, value):
                    windows.append(parsed)

        return ProviderSnapshot(
            provider_id=self.provider_id,
            ready=bool(windows),
            source=str(candidate.get("source") or "codexbar"),
            account=candidate.get("account") or usage.get("accountEmail"),
            plan=candidate.get("plan") or usage.get("loginMethod"),
            windows=windows,
            error=None if windows else "Claude account did not report quota windows",
        )

    @staticmethod
    def _parse_window(name: str, value: object) -> UsageWindow | None:
        if not isinstance(value, dict):
            return None
        if "usedPercent" in value:
            used = float(value["usedPercent"])
        elif "remainingPercent" in value:
            used = 100 - float(value["remainingPercent"])
        else:
            return None
        reset_value = value.get("resetsAt")
        resets_at: datetime | None = None
        if isinstance(reset_value, str):
            resets_at = datetime.fromisoformat(reset_value.replace("Z", "+00:00"))
        elif isinstance(reset_value, int | float):
            resets_at = datetime.fromtimestamp(float(reset_value), tz=UTC)
        duration = value.get("windowMinutes") or value.get("windowDurationMins")
        return UsageWindow(
            name=name,
            used_percent=max(0, min(100, used)),
            remaining_percent=max(0, min(100, 100 - used)),
            window_minutes=int(duration) if isinstance(duration, int | float) else None,
            resets_at=resets_at,
        )
