"""Read-only acquisition status: business states and request attempts stay separate."""

import collections
import json
import time


def snapshot(db):
    now = time.time()
    states = collections.Counter()
    errors = collections.Counter()
    unknown = 0
    oldest = 0
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_tasks'").fetchone():
            return {}
        for (raw,) in c.execute("SELECT body FROM acquisition_tasks"):
            w = db.open(raw)
            states[(w["stage"], w.get("category", ""), bool(w.get("manual")))] += 1
            if w.get("unknown_favorite") or w.get("unknown_draft"):
                unknown += 1
            if w["stage"] != "ready":
                oldest = max(oldest, now - w["last_progress"])
        attempts = c.execute(
            "SELECT operation,state,error_class,timing FROM acquisition_attempts WHERE started>?",
            (now - 3600,),
        ).fetchall()
        totals = collections.defaultdict(
            lambda: {"attempts": 0, "network_ms": 0, "rate_wait_ms": 0, "total_ms": 0}
        )
        for path, state, error, raw in attempts:
            timing = json.loads(raw)
            row = totals[path]
            row["attempts"] += 1
            for name in ("network_ms", "rate_wait_ms", "total_ms"):
                row[name] += timing.get(name, 0)
            if error:
                errors[error] += 1
        return {
            "states": [
                {"stage": k[0], "error_class": k[1], "manual": k[2], "count": n} for k, n in states.items()
            ],
            "unknown_tasks": unknown,
            "oldest_progress_age_s": round(oldest),
            "errors_1h": dict(errors),
            "operations_1h": dict(totals),
            "note": "Attempts and ready dossiers are not stock_verified completions.",
        }


def retry_read(db, key):
    """Operator recovery clears a local read hold, never a remote write intent."""
    with db.write_transaction() as c:
        row = c.execute("SELECT * FROM acquisition_tasks WHERE key=?", (key,)).fetchone()
        if not row:
            raise ValueError("acquisition_task_missing")
        if row["lease_until"] > time.time():
            raise ValueError("acquisition_task_busy")
        work = db.open(row["body"])
        if work["stage"] not in ("find_favorite", "find_draft", "read_draft"):
            raise ValueError("only_read_operations_can_be_requeued")
        work.pop("manual", None)
        work.pop("scan", None)
        work.update(due=0, failures=0, reason="operator_requested_readback")
        c.execute(
            "UPDATE acquisition_tasks SET body=?,version=version+1,updated=? WHERE key=?",
            (db.seal(work), time.time(), key),
        )
        c.execute(
            "INSERT INTO acquisition_events VALUES(NULL,?,?,?,?,?)",
            (
                key,
                row["version"] + 1,
                work["stage"],
                time.time(),
                json.dumps(
                    {
                        "action": "operator_readback",
                        "unknown_favorite": work.get("unknown_favorite"),
                        "unknown_draft": work.get("unknown_draft"),
                    }
                ),
            ),
        )
    return {"task_key": key, "state": "readback_requeued", "writes_reauthorized": False}


def main():
    import argparse
    from pathlib import Path

    from cryptography.fernet import Fernet

    from .db import Database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument(
        "--retry-read", metavar="TASK_KEY", help="Requeue a stopped read only; never clears an unknown write"
    )
    args = parser.parse_args()
    # Existing files only: no Database constructor/migrations or worker startup.
    db = Database.__new__(Database)
    db.directory = args.data.resolve()
    db.path = db.directory / "flowhub.sqlite3"
    if not db.path.is_file():
        raise ValueError("existing data directory required")
    db.cipher = Fernet((db.directory / "master.key").read_bytes())
    print(
        json.dumps(
            retry_read(db, args.retry_read) if args.retry_read else snapshot(db), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
