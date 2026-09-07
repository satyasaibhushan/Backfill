"""Stop the guard's private process group if its parent dies or time expires."""

import os
import signal
import sys
import time
from contextlib import suppress


def watch(parent: int, group: int, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while os.getppid() == parent and time.monotonic() < deadline:
        # The group leader is an unreaped child of our living parent. Its PID cannot
        # be reused while this ownership relationship holds.
        try:
            os.kill(group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    with suppress(ProcessLookupError):
        os.killpg(group, signal.SIGTERM)
    time.sleep(1)
    with suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)


if __name__ == "__main__":
    watch(int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]))
