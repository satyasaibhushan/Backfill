import asyncio
import os
import signal
from contextlib import suppress


async def stop_probe(process: asyncio.subprocess.Process) -> None:
    def send(sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            # Native sandbox helpers can outlive an already-reaped parent on macOS.
            # Their group permissions must not turn a completed turn into a failure.
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.send_signal(sig)

    send(signal.SIGTERM)
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2)
    send(signal.SIGKILL)
    await process.wait()
