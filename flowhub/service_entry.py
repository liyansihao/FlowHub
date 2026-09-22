"""Adopt a manually started supervisor after it exits; never start a second writer."""
import fcntl
import os
import sys
import time
from pathlib import Path


def main():
    data = Path(os.environ['FLOWHUB_DATA'])
    if not (data / 'flowhub.sqlite3').is_file():
        raise SystemExit('Production database missing; refusing to create a new installation')
    # The existing supervisor owns this lock for its full lifetime. Closing this
    # probe before exec is safe: control.serve independently claims the same lock.
    while True:
        with (data / 'supervisor.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(10)
                continue
        os.execv(sys.executable, [sys.executable, '-m', 'flowhub.control', '_supervise'])


if __name__ == '__main__':
    main()
