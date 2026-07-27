import asyncio
import json
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from backfill.config import Settings
from backfill.providers.models import ProviderSnapshot, UsageWindow


class CodexRpcError(RuntimeError):
    pass


class CodexProvider:
    provider_id = "codex"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def probe(self) -> ProviderSnapshot:
        if not shutil.which(self.settings.codex_command):
            return ProviderSnapshot(
                provider_id=self.provider_id,
                ready=False,
                source="codex-app-server",
                error=f"{self.settings.codex_command} is not installed",
            )

        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    self.settings.codex_command,
                    "-s",
                    "read-only",
                    "-a",
                    "untrusted",
                    "app-server",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                if process.stderr:
                    stderr_task = asyncio.create_task(process.stderr.read())
                await self._send(
                    process,
                    {
                        "method": "initialize",
                        "id": 1,
                        "params": {
                            "clientInfo": {
                                "name": "backfill",
                                "title": "Backfill",
                                "version": "0.1.0",
                            }
                        },
                    },
                )
                await self._read_response(process, 1)
                await self._send(process, {"method": "initialized", "params": {}})
                await self._send(
                    process,
                    {"method": "account/read", "id": 2, "params": {"refreshToken": False}},
                )
                account_result = await self._read_response(process, 2)
                await self._send(process, {"method": "account/rateLimits/read", "id": 3})
                limit_result = await self._read_response(process, 3)
                return self._parse(account_result, limit_result)
        except (TimeoutError, OSError, CodexRpcError, json.JSONDecodeError) as error:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                ready=False,
                source="codex-app-server",
                error=str(error),
            )
        finally:
            if process and process.returncode is None:
                process.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if stderr_task:
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task

    async def _send(self, process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        if not process.stdin:
            raise CodexRpcError("Codex app-server stdin is unavailable")
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()

    async def _read_response(
        self, process: asyncio.subprocess.Process, request_id: int
    ) -> dict[str, Any]:
        if not process.stdout:
            raise CodexRpcError("Codex app-server stdout is unavailable")
        while line := await process.stdout.readline():
            payload = json.loads(line)
            if payload.get("id") != request_id:
                continue
            if "error" in payload:
                message = payload["error"].get("message", "Codex app-server request failed")
                raise CodexRpcError(message)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise CodexRpcError("Codex app-server returned an invalid response")
            return result
        raise CodexRpcError("Codex app-server closed before responding")

    def _parse(
        self, account_result: dict[str, Any], limit_result: dict[str, Any]
    ) -> ProviderSnapshot:
        account = account_result.get("account") or {}
        rate_limits = limit_result.get("rateLimits") or {}
        windows: list[UsageWindow] = []
        for fallback_name, key in (("session", "primary"), ("weekly", "secondary")):
            value = rate_limits.get(key)
            name = self._window_name(fallback_name, value)
            if parsed := self._parse_window(name, value):
                windows.append(parsed)
        return ProviderSnapshot(
            provider_id=self.provider_id,
            ready=bool(windows),
            source="codex-app-server",
            account=account.get("email"),
            plan=account.get("planType"),
            windows=windows,
            error=None if windows else "Codex account did not report subscription rate limits",
        )

    @staticmethod
    def _window_name(fallback_name: str, value: object) -> str:
        if not isinstance(value, dict):
            return fallback_name
        duration = value.get("windowDurationMins")
        if isinstance(duration, int | float):
            return "session" if duration <= 1_440 else "weekly"
        return fallback_name

    @staticmethod
    def _parse_window(name: str, value: object) -> UsageWindow | None:
        if not isinstance(value, dict):
            return None
        used = float(value.get("usedPercent", 0))
        reset_value = value.get("resetsAt")
        resets_at = (
            datetime.fromtimestamp(float(reset_value), tz=UTC)
            if isinstance(reset_value, int | float)
            else None
        )
        duration = value.get("windowDurationMins")
        return UsageWindow(
            name=name,
            used_percent=max(0, min(100, used)),
            remaining_percent=max(0, min(100, 100 - used)),
            window_minutes=int(duration) if isinstance(duration, int | float) else None,
            resets_at=resets_at,
        )
