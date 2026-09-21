import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flowhub.db import Database


def test_write_transaction_serializes_database_instances(tmp_path):
    first = Database(tmp_path)
    second = Database(tmp_path)
    with first.connect() as c:
        c.execute("CREATE TABLE writer_probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

    state = {"active": 0, "maximum": 0}
    state_lock = threading.Lock()

    def write(db, value):
        with db.write_transaction() as c:
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            c.execute("INSERT INTO writer_probe(value) VALUES(?)", (value,))
            time.sleep(0.01)
            with state_lock:
                state["active"] -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: write((first, second)[i % 2], str(i)), range(24)))

    with first.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM writer_probe").fetchone()[0] == 24
    assert state["maximum"] == 1


def test_write_transaction_rolls_back_and_releases_lock(tmp_path):
    db = Database(tmp_path)
    with db.connect() as c:
        c.execute("CREATE TABLE writer_probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

    try:
        with db.write_transaction() as c:
            c.execute("INSERT INTO writer_probe(value) VALUES('aborted')")
            raise RuntimeError("abort probe")
    except RuntimeError:
        pass

    with db.write_transaction() as c:
        c.execute("INSERT INTO writer_probe(value) VALUES('committed')")
    with db.connect() as c:
        assert [row[0] for row in c.execute("SELECT value FROM writer_probe")] == ["committed"]
