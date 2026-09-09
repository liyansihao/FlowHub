import json
import time

import pytest
from fastapi.testclient import TestClient

from flowhub.api import create_app
from flowhub.db import Database, password_hash
from flowhub.worker import Worker


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE users SET password=?,must_change=0", (password_hash("Local-Test-Password-2026"),))
        admin = c.execute("SELECT id FROM users").fetchone()[0]
        user = db.create_user(c, "alice", "Alice-Test-Password-2026")
        c.execute("UPDATE users SET must_change=0 WHERE id=?", (user,))
    app = create_app(db)
    return db, admin, user, app


def logged(app, name="admin", password="Local-Test-Password-2026"):
    client = TestClient(app)
    assert client.post("/api/login", json={"username": name, "password": password}).status_code == 200
    csrf = client.get("/api/me").json()["csrf"]
    client.headers["X-CSRF-Token"] = csrf
    return client


def test_tenant_isolation_admin_scope_csrf_and_secrets(setup):
    db, admin, user, app = setup
    a = logged(app)
    u = logged(app, "alice", "Alice-Test-Password-2026")
    a.post("/api/stores", json={"name": "private", "api_key": "NEVER_EXPOSE_THIS", "kind": "demo"})
    assert u.get("/api/stores").json() == []
    assert u.get("/api/stores?owner=" + admin).status_code == 403
    assert u.get("/api/users").status_code == 403
    assert u.get("/api/events").status_code == 403
    assert "NEVER_EXPOSE_THIS" not in a.get("/api/stores").text
    assert b"NEVER_EXPOSE_THIS" not in db.path.read_bytes()
    assert a.get("/api/stores?owner=" + user).status_code == 200
    a.headers.pop("X-CSRF-Token")
    assert a.post("/api/workflow/pause").status_code == 403
    for path in ["/data/master.key", "/data/INITIAL_ADMIN.txt", "/flowhub/api.py", "/docs", "/openapi.json"]:
        assert a.get(path).status_code == 404


def test_password_rotation_invalidates_old_sessions_and_initial_gate(setup):
    db, admin, user, app = setup
    with db.connect() as c:
        c.execute("UPDATE users SET must_change=1 WHERE id=?", (admin,))
    a = logged(app)
    assert a.get("/api/stores").status_code == 403
    assert (
        a.post(
            "/api/password", json={"current": "Local-Test-Password-2026", "new": "New-Local-Password-2026"}
        ).status_code
        == 200
    )
    assert a.get("/api/me").status_code == 401
    assert logged(app, password="New-Local-Password-2026").get("/api/stores").status_code == 200


def test_login_throttling_and_no_live_demo_mix(setup):
    db, admin, user, app = setup
    client = TestClient(app)
    for _ in range(8):
        assert client.post("/api/login", json={"username": "bad", "password": "bad"}).status_code == 401
    assert client.post("/api/login", json={"username": "bad", "password": "bad"}).status_code == 429
    a = logged(app)
    a.post("/api/stores", json={"name": "simulation"})
    w = a.get("/api/workflow").json()
    w["rules"]["live"] = True
    assert a.put("/api/workflow", json={"rules": w["rules"], "modules": w["modules"]}).status_code == 200
    assert a.post("/api/workflow/start").status_code == 400


def enable_demo(db, admin, app):
    a = logged(app)
    a.post("/api/stores", json={"name": "Demo 1", "kind": "demo"})
    a.post("/api/stores", json={"name": "Demo 2", "kind": "demo", "position": 1})
    assert a.post("/api/workflow/start").status_code == 200
    return a


async def drain(worker, steps=60):
    for _ in range(steps):
        with worker.db.connect() as c:
            c.execute("UPDATE jobs SET next_at=0")
        await worker.step()


@pytest.mark.asyncio
async def test_complete_pipeline_and_duplicate_candidate_cursor(setup):
    db, admin, user, app = setup
    enable_demo(db, admin, app)
    w = Worker(db)
    await w.replenish()
    await drain(w)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE phase='selling'").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM demo_effects").fetchone()[0] == 2
        c.execute("UPDATE workflows SET cursor='0',last_fetch=0")
    await w.replenish()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,write_phase,final_phase",
    [("publish", "publishing", "selling"), ("stock", "stock_pending", "selling")],
)
async def test_accepted_write_lost_response_restarts_without_duplicate(
    setup, operation, write_phase, final_phase
):
    db, admin, user, app = setup
    enable_demo(db, admin, app)
    w = Worker(db)
    await w.replenish()
    original = w.host.invoke
    calls = 0

    async def lose(module, op, context, secret=""):
        nonlocal calls
        result = await original(module, op, context, secret)
        if op == operation:
            calls += 1
            raise TimeoutError("response was lost")
        return result

    w.host.invoke = lose
    for _ in range(25):
        with db.connect() as c:
            c.execute("UPDATE jobs SET next_at=0")
            if c.execute("SELECT 1 FROM jobs WHERE phase=?", (write_phase,)).fetchone():
                break
        await w.step()
    with db.connect() as c:
        affected = c.execute("SELECT id FROM jobs WHERE phase=?", (write_phase,)).fetchone()[0]
    restarted = Worker(db)
    await drain(restarted)
    with db.connect() as c:
        assert c.execute("SELECT phase FROM jobs WHERE id=?", (affected,)).fetchone()[0] == final_phase
    assert calls == 1


@pytest.mark.asyncio
async def test_blocklist_before_stock_and_expired_lease_recovery(setup):
    db, admin, user, app = setup
    enable_demo(db, admin, app)
    w = Worker(db)
    await w.replenish()
    with db.connect() as c:
        row = c.execute("SELECT * FROM jobs WHERE source_key='demo-1'").fetchone()
        c.execute(
            "UPDATE jobs SET lease=?,lease_until=? WHERE id=?", ("dead-worker", time.time() - 1, row["id"])
        )
        c.execute("INSERT INTO blocks VALUES(?,?,?)", (admin, "demo-1", "must not list"))
    await drain(w)
    with db.connect() as c:
        assert c.execute("SELECT phase FROM jobs WHERE id=?", (row["id"],)).fetchone()[0] == "rejected"
        assert not c.execute("SELECT 1 FROM demo_effects WHERE id=?", (row["id"],)).fetchone()


@pytest.mark.asyncio
async def test_quota_rotates_without_moving_existing_job(setup):
    db, admin, user, app = setup
    enable_demo(db, admin, app)
    w = Worker(db)
    await w.replenish()
    original = w.host.invoke
    with db.connect() as c:
        first = c.execute("SELECT id FROM stores ORDER BY position,id").fetchone()[0]

    async def quota(module, op, context, secret=""):
        if op == "quota":
            return {
                "store_id": context["store"]["id"],
                "remaining": 0 if context["store"]["id"] == first else 10,
                "reset_at": time.time() + 200,
            }
        return await original(module, op, context, secret)

    w.host.invoke = quota
    await drain(w)
    with db.connect() as c:
        rows = c.execute("SELECT store_id FROM jobs WHERE phase='selling'").fetchall()
        assert len(rows) == 2 and all(r[0] != first for r in rows)


@pytest.mark.asyncio
async def test_http_contract_and_private_endpoint_rejected(setup, monkeypatch):
    from flowhub.modules import ModuleError, public_endpoint

    with pytest.raises(ModuleError):
        await public_endpoint("https://127.0.0.1/module")
    with pytest.raises(ModuleError):
        await public_endpoint("http://example.com/module")
    db, admin, user, app = setup
    a = logged(app)
    assert (
        a.post(
            "/api/modules",
            json={"id": "bad-module", "kind": "matcher", "name": "bad", "endpoint": "https://127.0.0.1/x"},
        ).status_code
        == 400
    )


def test_disable_account_revokes_session(setup):
    db, admin, user, app = setup
    a = logged(app)
    u = logged(app, "alice", "Alice-Test-Password-2026")
    assert a.post("/api/users/" + user + "/disable").status_code == 200
    assert u.get("/api/me").status_code == 401


@pytest.mark.asyncio
async def test_full_page_is_buffered_without_skipping_unadmitted_candidates(setup):
    db, admin, user, app = setup
    enable_demo(db, admin, app)
    worker = Worker(db)
    original = worker.host.invoke

    async def page(module, op, context, secret=""):
        if op == "candidates":
            base = (await original(module, op, context, secret))["items"][1]
            return {"items": [base | {"source_key": f"large-{i}"} for i in range(50)], "cursor": "50"}
        return await original(module, op, context, secret)

    worker.host.invoke = page
    await worker.replenish()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 30
        assert len(json.loads(c.execute("SELECT body FROM candidate_pages").fetchone()[0])["items"]) == 20
        c.execute("UPDATE jobs SET phase='rejected'")
        c.execute("UPDATE workflows SET last_fetch=0")
    restarted = Worker(db)
    await restarted.replenish()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 50
        assert c.execute("SELECT cursor FROM workflows WHERE owner=?", (admin,)).fetchone()[0] == "50"


@pytest.mark.asyncio
async def test_module_swap_does_not_rewrite_existing_job_definitions(setup):
    db, admin, user, app = setup
    a = enable_demo(db, admin, app)
    w = Worker(db)
    await w.replenish()
    with db.connect() as c:
        before = c.execute("SELECT modules FROM jobs LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO modules(id,kind,name,driver) VALUES('matcher-v2','matcher','v2','demo')")
    a.post("/api/workflow/pause")
    settings = a.get("/api/workflow").json()
    settings["modules"]["matcher"] = "matcher-v2"
    assert (
        a.put("/api/workflow", json={"rules": settings["rules"], "modules": settings["modules"]}).status_code
        == 200
    )
    with db.connect() as c:
        assert c.execute("SELECT modules FROM jobs LIMIT 1").fetchone()[0] == before


@pytest.mark.asyncio
async def test_http_provider_contract_can_replace_matching_module(setup, monkeypatch):
    import httpx

    import flowhub.modules as modules

    db, admin, user, app = setup
    calls = []

    async def approved(url):
        assert url == "https://provider.example/match"

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["Authorization"] == "Bearer per-user-key"
        assert body["version"] == "1" and body["operation"] == "match"
        return httpx.Response(
            200, json={"version": "1", "result": {"supplier_id": "new-provider", "score": 0.91}}
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(modules, "public_endpoint", approved)
    monkeypatch.setattr(
        modules.httpx, "AsyncClient", lambda **kw: client(**kw, transport=httpx.MockTransport(respond))
    )
    result = await modules.ModuleHost(db).invoke(
        {"driver": "http", "endpoint": "https://provider.example/match"},
        "match",
        {"candidate": {"source_key": "sku-1"}, "idempotency_key": "fixed-job"},
        "per-user-key",
    )
    assert result["supplier_id"] == "new-provider" and len(calls) == 1
