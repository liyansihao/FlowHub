import json
import time

import pytest

from flowhub.db import Database
from flowhub.source_acquisition import AcquisitionError, SourceAcquirer
from flowhub.source_library import SourceLibrary, ranking_product


@pytest.fixture
def lib(tmp_path):
    return SourceLibrary(Database(tmp_path / "data"))


def task(lib, now):
    key = lib.enqueue("a", "seller_scan", {"seller_id": "12", "category2": "22", "mainType": "hot"})
    return key, lib.claim("a", now, kinds=("seller_scan",))


def test_pause_inflight_atomic_commit_restart_no_duplicates(lib):
    now = time.time()
    key, held = task(lib, now)
    row = dict(
        sku="123456",
        seller_id="12",
        cate2_id="22",
        name="box",
        photo="https://example.com/a.jpg",
        weight=10,
        sales_schema="FBS",
        avg_price=100,
        sold_count=1,
    )
    product = ranking_product(row, now)
    lib.control_task("a", key, "pause")
    lib.commit_page(
        held | {"response": {"data": [row]}, "request_query": {"page": 1}}, [row], [product], 3, now
    )
    # Process restart reuses SQLite and cannot claim a paused second page.
    reopened = SourceLibrary(Database(lib.db.directory))
    assert reopened.claim("a", now + 10, kinds=("seller_scan",)) is None
    reopened.control_task("a", key, "resume")
    second = reopened.claim("a", now + 10, kinds=("seller_scan",))
    assert second["page"] == 2
    # Overlapping pages add only new identities. Raw rows and cursor commit atomically.
    next_row = row | {"sku": "234567"}
    result = reopened.commit_page(
        second, [row, next_row], [product, ranking_product(next_row, now)], 3, now + 10
    )
    assert result["added"] == 1
    with lib.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sourcing_products").fetchone()[0] == 2
        assert [r[0] for r in c.execute("SELECT page FROM sourcing_attempts ORDER BY id")] == [1, 2]
        assert c.execute("SELECT page FROM sourcing_tasks WHERE id=?", (key,)).fetchone()[0] == 3


def test_repeated_page_rolls_back_products_and_keeps_failure_reason(lib):
    now = time.time()
    key, held = task(lib, now)
    row = {"sku": "123456"}
    lib.commit_page(held, [row], [], 4, now)
    held = lib.claim("a", now + 10)
    with pytest.raises(ValueError, match="repeated_page"):
        lib.commit_page(held, [row], [], 4, now + 10)
    lib.fail(held | {"response": {"data": [row]}}, "repeated_page", now + 10)
    with lib.db.connect() as c:
        state = c.execute("SELECT page,state FROM sourcing_tasks WHERE id=?", (key,)).fetchone()
        assert tuple(state) == (2, "blocked")
        assert [r[0] for r in c.execute("SELECT state FROM sourcing_attempts ORDER BY id")] == [
            "committed",
            "repeated_page",
        ]


def test_three_failures_stop_same_page_and_tenant_control(lib):
    now = time.time()
    key, held = task(lib, now)
    with pytest.raises(KeyError):
        lib.control_task("b", key, "pause")
    for attempt in range(3):
        lib.fail(held, "network", now, diagnostic={"cause_code": "ETIMEDOUT"})
        now += 4000
        if attempt < 2:
            held = lib.claim("a", now)
            assert held["page"] == 1
    assert lib.claim("a", now) is None
    lib.control_task("a", key, "retry")
    assert lib.claim("a", now)["page"] == 1
    with lib.db.connect() as c:
        assert all(
            json.loads(r[0])["diagnostic"]["cause_code"] == "ETIMEDOUT"
            for r in c.execute("SELECT body FROM sourcing_attempts")
        )


def test_expired_worker_cannot_commit_after_new_lease(lib):
    now = time.time()
    key, old = task(lib, now)
    new = lib.claim("a", now + 151)
    assert new["lease"] != old["lease"]
    with pytest.raises(ValueError, match="lease expired"):
        lib.commit_page(old, [], [], 1, now + 151)
    lib.fail(old, "network", now + 151)
    with lib.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sourcing_attempts").fetchone()[0] == 0


async def test_acquirer_preserves_diagnostic_and_exact_seller(lib):
    now = time.time()
    lib.enqueue("a", "seller_scan", {"seller_id": "12", "category2": "22", "mainType": "hot"})
    with lib.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_seeds VALUES(?,?,?,?,?,?,?,?)",
            ("a", "own", "offer", "123456", json.dumps({"seller_id": "12"}), 0, 1, now),
        )

    async def delists():
        return {"skus": [], "offers": []}

    async def request(path, query, token):
        assert "seller_id" not in query
        raise AcquisitionError("network", {"cause_code": "ECONNRESET"})

    result = await SourceAcquirer(lib, request, delists).cycle("a", "fake", now)
    assert result == {"state": "retry", "reason": "network"}
    with lib.db.connect() as c:
        audit = json.loads(c.execute("SELECT body FROM sourcing_attempts").fetchone()[0])
    assert audit["origin"]["seller_id"] == "12"
    assert audit["diagnostic"]["cause_code"] == "ECONNRESET"
    assert audit["query"]["page"] == 1


def test_resume_preserves_end_of_scan_delay(lib):
    now = time.time()
    key, held = task(lib, now)
    lib.commit_page(held, [], [], 1, now)
    lib.control_task("a", key, "pause")
    lib.control_task("a", key, "resume")
    assert lib.claim("a", now + 10, kinds=("seller_scan",)) is None
    with lib.db.connect() as c:
        assert c.execute("SELECT due FROM sourcing_tasks WHERE id=?", (key,)).fetchone()[0] == now + 86400


def test_nonobject_page_is_schema_failure():
    from flowhub.source_acquisition import page_data

    for invalid in (None, 123, "unexpected HTML"):
        with pytest.raises(AcquisitionError, match="schema"):
            page_data(invalid, 1, 100)


def test_task_api_controls_and_audit_are_scoped(lib):
    from fastapi.testclient import TestClient

    from flowhub.api import create_app
    from flowhub.db import password_hash

    with lib.db.connect() as c:
        c.execute("UPDATE users SET password=?,must_change=0", (password_hash("test-password-12345"),))
        owner = c.execute("SELECT id FROM users").fetchone()[0]
    key = lib.enqueue(owner, "seller_scan", {"seller_id": "12", "category2": "22"})
    foreign = lib.enqueue("other", "seller_scan", {"seller_id": "99"})
    client = TestClient(create_app(lib.db))
    assert client.get("/api/sources/tasks").status_code == 401
    client.post("/api/login", json={"username": "admin", "password": "test-password-12345"})
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    assert [r["id"] for r in client.get("/api/sources/tasks").json()] == [key]
    assert client.post(f"/api/sources/tasks/{foreign}/pause").status_code == 404
    assert client.post(f"/api/sources/tasks/{key}/pause").json()["state"] == "paused"
    assert client.post(f"/api/sources/tasks/{key}/resume").json()["state"] == "ready"
    held = lib.claim(owner)
    lib.fail(held, "network", diagnostic={"cause_code": "ECONNRESET"})
    rows = client.get(f"/api/sources/tasks/{key}/attempts").json()
    assert len(rows) == 1 and rows[0]["state"] == "network"
    assert client.get(f"/api/sources/tasks/{foreign}/attempts").json() == []
