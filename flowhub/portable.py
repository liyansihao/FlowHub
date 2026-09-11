"""Container-only compatibility bootstrap. Does not import stores or private credentials."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path

from .db import DATA, Database


def prepare(seed, target):
    """Update code without overwriting runtime state, store credentials or the queue."""
    for src in seed.rglob("*"):
        if not src.is_file() or "node_modules" in src.parts:
            continue
        rel = src.relative_to(seed)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "state" in rel.parts and dest.exists():
            continue
        shutil.copy2(src, dest)
    modules = target / "node_modules"
    if not modules.exists():
        modules.symlink_to(seed / "node_modules", target_is_directory=True)
    with Database().connect() as c:
        for kind, name in [
            ("candidates", "FlowB 持续发现"),
            ("matcher", "compareBot 1688 同款"),
            ("profit", "毛子邮政利润"),
        ]:
            c.execute(
                "INSERT OR IGNORE INTO modules VALUES(?,?,?,?,?,?)",
                ("flowb-" + kind, kind, name, "comparebot" if kind == "matcher" else "flowb", "", 1),
            )


def run():
    db = Database()
    lock = (DATA / "discovery-supervisor.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    root = Path(os.environ["FLOWHUB_LEGACY_ROOT"])
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    child = None
    current_token = None
    try:
        while not stop.is_set():
            with db.connect() as c:
                row = c.execute(
                    "SELECT w.* FROM workflows w JOIN users u ON u.id=w.owner WHERE w.enabled=1 AND u.role='admin' AND u.active=1"
                ).fetchone()
            token = None
            if row:
                module_id = json.loads(row["modules"]).get("candidates")
                if module_id == "flowb-candidates":
                    token = db.open(row["secrets"]).get(module_id)
            if child and (not token or token != current_token):
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                child = None
            if token and (child is None or child.poll() is not None):
                # No ERP token in command line, logs, image or installation package.
                env = os.environ | {
                    "MAOZI_ACCESS_TOKEN": token,
                    "FLOW_EF_CONFIG_FILE": str(root / "flow_b_ef/state/config.json"),
                    "FLOW_EF_STATE_DIR": str(root / "flow_b_ef/state/flow-ef"),
                    "MAOZI_HTTP_BACKEND": "native",
                }
                child = subprocess.Popen(
                    ["node", str(root / "flow_b_ef/discover.mjs"), "watch"],
                    env=env,
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                current_token = token
            stop.wait(15)
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    run()
