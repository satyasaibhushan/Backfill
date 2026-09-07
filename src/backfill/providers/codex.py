import asyncio
import json
import shutil
from contextlib import suppress
from typing import Any

from backfill.config import Settings
from backfill.providers.models import ProviderSnapshot, fingerprint, parse_window
from backfill.providers.process import stop_probe


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
                    "on-request",
                    "app-server",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
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
                                "version": "0.2.0",
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
        except (
            TimeoutError,
            OSError,
            CodexRpcError,
            ValueError,
            TypeError,
            OverflowError,
            AttributeError,
        ):
            return ProviderSnapshot(
                provider_id=self.provider_id,
                ready=False,
                source="codex-app-server",
                error="provider probe failed or returned incomplete quota data",
            )
        finally:
            if process:
                await stop_probe(process)
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
        identity = fingerprint(self.provider_id, account.get("email"))
        buckets = limit_result.get("rateLimitsByLimitId")
        if buckets is None:
            buckets = {"default": limit_result.get("rateLimits")}
        if not isinstance(buckets, dict) or not buckets:
            raise ValueError("missing quota buckets")
        windows = []
        for bucket, limits in buckets.items():
            if not isinstance(limits, dict):
                raise ValueError("invalid quota bucket")
            found = []
            for slot in ("primary", "secondary"):
                window = parse_window(f"{bucket}.{slot}", limits.get(slot))
                if window:
                    found.append(window)
            if not found:
                raise ValueError("quota bucket has no readable windows")
            windows.extend(found)
        return ProviderSnapshot(
            provider_id=self.provider_id,
            ready=True,
            source="codex-app-server",
            account=identity,
            windows=windows,
        )
