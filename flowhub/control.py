"""Local process supervision; use Docker restart policies for server deployment."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading

from .db import DATA, ROOT, Database


def serve():
    Database()
    lock = (DATA / "supervisor.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (DATA / "supervisor.json").write_text(json.dumps({"pid": os.getpid()}))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    commands = {
        "web": [
            sys.executable,
            "-m",
            "uvicorn",
            "flowhub.api:app",
            "--host",
            os.environ.get("FLOWHUB_BIND_HOST", "127.0.0.1"),
            "--port",
            os.environ.get("FLOWHUB_PORT", "38427"),
            "--no-access-log",
            "--log-level",
            "error",
        ],
        "worker": [sys.executable, "-m", "flowhub.worker"],
        "acceptance": [sys.executable, "-m", "flowhub.acceptance"],
    }
    if os.environ.get("FLOWHUB_PORTABLE") == "1":
        commands["discovery"] = [sys.executable, "-m", "flowhub.portable"]
    children = {}
    spawned = {}
    restart_at = {}
    log = (DATA / "supervisor.log").open("ab")
    try:
        while not stop.is_set():
            import time

            for name, command in commands.items():
                child = children.get(name)
                if name == "worker" and child and child.poll() is None:
                    try:
                        with Database().connect() as db:
                            beat = db.execute("SELECT heartbeat FROM health WHERE name='worker'").fetchone()
                        baseline = max(spawned.get(name, 0), beat[0] if beat else 0)
                        if time.time() - baseline > 420:
                            os.killpg(child.pid, signal.SIGKILL)
                    except Exception:
                        pass
                if child and child.poll() is not None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    children.pop(name)
                    restart_at[name] = time.time() + 5
                if name not in children and time.time() >= restart_at.get(name, 0):
                    children[name] = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                    spawned[name] = time.time()
                    (DATA / f"{name}.json").write_text(json.dumps({"pid": children[name].pid}))
            stop.wait(1)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=150)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
        log.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status", "_supervise"])
    args = parser.parse_args()
    if args.action == "_supervise":
        serve()
        return
    Database()
    if args.action == "start":
        with (DATA / "supervisor.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (DATA / "supervisor.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "flowhub.control", "_supervise"],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        if sys.platform == "darwin":
            subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-s", "-w", str(child.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        print(json.dumps({"supervisor_pid": child.pid, "url": "http://127.0.0.1:38427/"}))
    elif args.action == "stop":
        pid = json.loads((DATA / "supervisor.json").read_text())["pid"]
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
        ).stdout
        if "flowhub.control _supervise" in command:
            os.kill(pid, signal.SIGTERM)
    else:
        with Database().connect() as db:
            print(json.dumps([dict(r) for r in db.execute("SELECT * FROM health")]))


if __name__ == "__main__":
    main()
