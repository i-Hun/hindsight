"""Two cooperative, crash-released slots shared by daemon and maintenance judges."""
import asyncio
import contextlib
import fcntl
import os
from pathlib import Path
import time

ROOT = Path.home() / '.hermes/hindsight/local-slots'


def _take():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    for n in range(2):
        fd = os.open(ROOT / str(n), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
    return None


@contextlib.contextmanager
def local_slot(deadline_at=None):
    fd = None
    try:
        while fd is None:
            if deadline_at is not None and time.monotonic() >= deadline_at:
                raise TimeoutError('local slot deadline')
            fd = _take()
            if fd is None:
                time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            os.close(fd)


@contextlib.asynccontextmanager
async def async_local_slot():
    fd = None
    try:
        while fd is None:
            fd = _take()
            if fd is None:
                await asyncio.sleep(0.05)
        yield
    finally:
        if fd is not None:
            os.close(fd)
