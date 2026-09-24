"""OS-level advisory file lock for the shared ledger/queue state directory.

This lock is never removed or stolen because it became old. A paused process retains it; process
termination releases the OS lock. It therefore replaces the age-based lockfile/revision-marker
takeover, whose whole failure mode was mistaking a paused holder for a dead one.

REQUIRES one shared, lock-coherent state directory. It does NOT make independently replicated
directories an authoritative ledger.
"""
import contextlib
import errno
import os
from pathlib import Path
import threading
import time

_registry_guard = threading.Lock()
_registry = {}
_local = threading.local()


@contextlib.contextmanager
def exclusive_file(path, timeout=30.0):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(path))

    with _registry_guard:
        gate = _registry.setdefault(key, threading.RLock())

    deadline = time.monotonic() + timeout
    if not gate.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"State lock busy: {path}")

    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()

    fd = None
    locked = False
    nested = key in held

    try:
        if nested:
            yield
            return

        # Persistent lock inode: never unlink this file.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")

        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                retryable = exc.errno in {
                    errno.EACCES, errno.EAGAIN, errno.EDEADLK
                }
                if not retryable:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"State lock busy: {path}") from exc
                time.sleep(0.025)

        held.add(key)
        yield
    finally:
        if not nested:
            held.discard(key)
            try:
                if locked:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                if fd is not None:
                    os.close(fd)
        gate.release()
