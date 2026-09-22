import asyncio
import time

import pytest

from flowhub.acquisition import Acquisition, AcquisitionPending
from flowhub.db import Database
from flowhub.source_detail import SourceAcquisitionFailure, SourceCollector


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path)
    (tmp_path / "acquisition-policy.json").write_text('{"enabled":true}')
    context = {
        "owner": "a",
        "candidate": {"source_key": "123", "price": 10},
        "store": {"credentials": {"erp_token": "test"}},
    }
    return db, context


def work(db):
    with db.connect() as c:
        return db.open(c.execute("SELECT body FROM acquisition_tasks").fetchone()[0])


def due(db):
    with db.connect() as c:
        row = c.execute("SELECT key,body FROM acquisition_tasks").fetchone()
        b = db.open(row["body"])
        b["due"] = 0
        c.execute("UPDATE acquisition_tasks SET body=? WHERE key=?", (db.seal(b), row["key"]))


async def step(db, context, call):
    collector = SourceCollector(db, context)
    collector.call = call
    try:
        return await Acquisition(collector).tick()
    except AcquisitionPending:
        return None


async def test_single_operation_each_tick_and_restart_with_snapshot(setup):
    db, context = setup
    calls = []

    async def call(path, method="GET", query=None, body=None):
        calls.append((path, method))
        if path.endswith("favorite/lists"):
            return {"data": [{"id": 7, "sku": "123", "is_imported": 0}], "total": 1}
        if path.endswith("edit_import"):
            return {"jump_id": 88}
        return {"goods_id": "123", "skus": [{}]}

    for _ in range(8):
        before = len(calls)
        result = await step(db, context, call)
        assert len(calls) - before <= 1
        if result:
            break
    assert result["draft_id"] == 88
    assert [p for p, m in calls if m == "POST"] == ["/api.product.favorite/edit_import"]
    before = len(calls)
    assert await step(db, context, call) == result
    assert len(calls) == before
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM dossier_snapshots").fetchone()[0] == 1


async def test_lost_write_only_reconciles_even_after_feature_disabled(setup):
    db, context = setup
    calls = []

    async def call(path, method="GET", query=None, body=None):
        calls.append((path, method))
        if path.endswith("favorite/lists"):
            return {"data": [{"id": 7, "sku": "123"}], "total": 1}
        if method == "POST":
            raise TimeoutError()
        if path.endswith("collect/lists"):
            return {"data": [{"id": 88, "goods_id": "123", "collect_from": "ozon"}], "total": 1}
        return {"goods_id": "123", "skus": [{}]}

    for _ in range(4):
        await step(db, context, call)
    assert work(db)["unknown_draft"]
    (db.directory / "acquisition-policy.json").write_text('{"enabled":false}')
    due(db)
    for _ in range(4):
        result = await step(db, context, call)
        if result:
            break
    assert result["draft_id"] == 88
    assert sum(m == "POST" for p, m in calls) == 1


async def test_not_sent_can_reschedule_same_intent(setup):
    db, context = setup
    attempts = 0

    async def call(path, method="GET", query=None, body=None):
        nonlocal attempts
        if path.endswith("favorite/lists"):
            return {"data": [{"id": 7, "sku": "123"}], "total": 1}
        if method == "POST":
            attempts += 1
            if attempts == 1:
                raise SourceAcquisitionFailure(
                    "MAOZI_API_PACING_WAIT", {"not_sent": True, "retry_after_ms": 1}
                )
            return {"id": 88}
        return {"skus": [{}]}

    for _ in range(4):
        await step(db, context, call)
    assert work(db)["stage"] == "import_draft"
    due(db)
    await step(db, context, call)
    result = await step(db, context, call)
    assert result["draft_id"] == 88 and attempts == 2


async def test_concurrent_worker_cannot_dispatch(setup):
    db, context = setup
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def call(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()
        return {"data": [], "total": 0}

    first = asyncio.create_task(step(db, context, call))
    await entered.wait()
    await step(db, context, call)
    assert len(calls) == 1
    release.set()
    await first


async def test_legacy_unknown_favorite_never_recreated(setup):
    db, context = setup
    s = SourceCollector(db, context)
    s.save("favorite_started", {"source_key": "123", "favorite_attempted": True})
    with db.connect() as c:
        c.execute("UPDATE source_details SET updated=?", (time.time() - 200,))

    async def call(path, method="GET", **kwargs):
        assert method == "GET"
        return {"data": [], "total": 0}

    await step(db, context, call)
    await step(db, context, call)
    assert work(db)["unknown_favorite"]
    assert work(db)["category"] == "remote_pending"


async def test_bad_pagination_never_authorizes_write(setup):
    db, context = setup

    async def call(path, method="GET", **kwargs):
        assert method == "GET"
        return {"data": [{"id": 7, "sku": "other"}], "total": 2}

    await step(db, context, call)
    await step(db, context, call)
    assert work(db)["manual"] == "listing_duplicate_or_invalid"


async def test_lost_ownership_cannot_save(setup):
    db, context = setup
    a = Acquisition(SourceCollector(db, context))
    a.claim()
    with db.connect() as c:
        c.execute("UPDATE acquisition_tasks SET version=version+1")
    with pytest.raises(AcquisitionPending):
        await a.advance("create_favorite")


async def test_deleted_draft_uses_fresh_backup_without_network(setup):
    db, context = setup
    s = SourceCollector(db, context)
    s.save("draft_ready", {"source_key": "123", "draft_id": 88})
    from flowhub.collection_capacity import account

    with db.connect() as c:
        c.execute(
            "CREATE TABLE draft_cleanup_receipts(account TEXT,draft_id TEXT,owner TEXT,sku TEXT,state TEXT,body TEXT,updated REAL)"
        )
        c.execute(
            "INSERT INTO draft_cleanup_receipts VALUES(?,?,?,?,?,?,?)",
            (
                account(context),
                "88",
                "a",
                "123",
                "deleted",
                db.seal({"fresh_detail": {"goods_id": "123", "skus": [{}]}, "at": time.time()}),
                time.time(),
            ),
        )

    async def forbidden(*args, **kwargs):
        pytest.fail("deleted remote draft must not be read or recreated")

    result = await step(db, context, forbidden)
    assert result["draft_id"] == 88 and result["detail"]["goods_id"] == "123"


async def test_pause_blocks_acquisition_before_dispatch(setup):
    db, context = setup
    from flowhub.pipeline_modules import control

    control.schema(db)
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO pipeline_module_control VALUES('seed',1,?)", (time.time(),))

    async def forbidden(*args, **kwargs):
        pytest.fail("paused")

    assert await step(db, context, forbidden) is None


async def test_cancelled_write_remains_unknown_and_never_replays(setup):
    db, context = setup

    async def call(path, method="GET", **kwargs):
        if method == "POST":
            raise asyncio.CancelledError()
        return {"data": [{"id": 7, "sku": "123"}], "total": 1}

    for _ in range(3):
        await step(db, context, call)
    with pytest.raises(asyncio.CancelledError):
        await step(db, context, call)
    assert work(db)["stage"] == "find_draft" and work(db)["unknown_draft"]
    with db.connect() as c:
        assert (
            c.execute("SELECT state FROM acquisition_attempts WHERE method='POST'").fetchone()[0] == "unknown"
        )


async def test_changed_total_is_not_absence(setup):
    db, context = setup
    calls = 0

    async def call(path, method="GET", **kwargs):
        nonlocal calls
        calls += 1
        return {"data": [{"id": calls, "sku": "other"}], "total": 2 if calls == 1 else 3}

    await step(db, context, call)
    await step(db, context, call)
    assert work(db)["category"] == "remote_pending" and "scan" not in work(db)


async def test_missing_real_price_does_not_create_favorite(setup):
    db, context = setup
    context["candidate"]["price"] = None

    async def call(path, method="GET", **kwargs):
        assert method == "GET"
        return {"data": [], "total": 0}

    await step(db, context, call)
    await step(db, context, call)
    assert work(db)["category"] == "missing_fields"


async def test_circuit_before_write_does_not_turn_unsent_into_unknown(setup):
    db, context = setup

    async def call(path, method="GET", **kwargs):
        assert method == "GET"
        return {"data": [{"id": 7, "sku": "123"}], "total": 1}

    for _ in range(3):
        await step(db, context, call)
    assert work(db)["stage"] == "import_draft"
    from flowhub.collection_capacity import account

    with db.connect() as c:
        c.execute(
            "INSERT INTO acquisition_circuits VALUES(?,?,?,?)",
            (account(context), "/api.product.favorite/edit_import", 3, time.time() + 60),
        )
    await step(db, context, call)
    assert work(db)["stage"] == "import_draft" and not work(db)["unknown_draft"]


async def test_existing_ready_snapshot_does_not_reacquire(setup):
    db, context = setup
    s = SourceCollector(db, context)
    s.save(
        "ready", {"source_key": "123", "draft_id": 88, "observed_at": time.time(), "detail": {"skus": [{}]}}
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("valid source snapshot must be reused")

    assert (await step(db, context, forbidden))["draft_id"] == 88


async def test_dispatch_releases_business_tick_while_preserving_operation_lease(setup):
    from flowhub.acquisition import close_runners, dispatch

    db, context = setup
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    collector = SourceCollector(db, context)

    async def call(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()
        return {"data": [], "total": 0}

    collector.call = call
    try:
        start = time.monotonic()
        with pytest.raises(AcquisitionPending) as waiting:
            await dispatch(collector)
        assert waiting.value.args[0] == "operation_inflight" and time.monotonic() - start < 1
        await entered.wait()
        with db.connect() as c:
            assert c.execute("SELECT lease_until FROM acquisition_tasks").fetchone()[0] > time.time()
        with pytest.raises(AcquisitionPending):
            await dispatch(collector)
        assert len(calls) == 1
        release.set()
        await asyncio.sleep(0.02)
    finally:
        await close_runners()


def test_cohort_only_admits_selected_new_sources(setup):
    from flowhub.acquisition import enabled

    db, _ = setup
    (db.directory / "acquisition-policy.json").write_text('{"enabled":true,"source_keys":["123"]}')
    assert enabled(db) and enabled(db, "123") and not enabled(db, "999")


async def test_runner_limit_does_not_dispatch_unbounded_requests(setup):
    from flowhub.acquisition import close_runners, dispatch

    db, context = setup
    release = asyncio.Event()
    calls = []

    async def call(*args, **kwargs):
        calls.append(args)
        await release.wait()
        return {"data": [], "total": 0}

    try:
        for sku in ["123", "124", "125"]:
            c = SourceCollector(db, context | {"candidate": context["candidate"] | {"source_key": sku}})
            c.call = call
            with pytest.raises(AcquisitionPending):
                await dispatch(c)
        assert len(calls) == 2
    finally:
        release.set()
        await close_runners()


async def test_operator_readback_preserves_unknown_intent(setup):
    from flowhub.acquisition_status import retry_read

    db, context = setup
    s = SourceCollector(db, context)
    s.save("draft_started", {"source_key": "123"})
    with db.connect() as c:
        c.execute("UPDATE source_details SET updated=?", (time.time() - 200,))
    a = Acquisition(s)
    a.claim()
    a.work["manual"] = "lookup failed"
    a.save()
    a.release()
    result = retry_read(db, s.key)
    assert result["writes_reauthorized"] is False
    assert work(db)["unknown_draft"] and work(db)["stage"] == "find_draft" and "manual" not in work(db)
    a = Acquisition(s)
    a.claim()
    await a.advance("import_draft")
    a.release()
    with pytest.raises(ValueError):
        retry_read(db, s.key)


async def test_shared_positive_draft_hint_skips_scan_but_still_reads_detail(setup):
    db, context = setup
    s = SourceCollector(db, context)
    s.save("draft_started", {"source_key": "123"})
    with db.connect() as c:
        c.execute("UPDATE source_details SET updated=?", (time.time() - 200,))
    a = Acquisition(s)
    with db.connect() as c:
        c.execute("INSERT INTO source_draft_index VALUES(?,?,?,?)", (a.account, "123", "88", time.time()))
    calls = []

    async def call(path, method="GET", **kwargs):
        calls.append(path)
        assert path.endswith("/detail")
        return {"goods_id": "123", "skus": [{}]}

    await step(db, context, call)
    assert not calls
    result = await step(db, context, call)
    assert result["draft_id"] == 88 and len(calls) == 1


async def test_credential_rotation_cannot_escape_unknown_source_intent(setup):
    db, context = setup
    s = SourceCollector(db, context)
    s.save("draft_started", {"source_key": "123"})
    with db.connect() as c:
        c.execute("UPDATE source_details SET updated=?", (time.time() - 200,))
    old = Acquisition(s)
    old.claim()
    old.release()
    changed = context | {"store": {"credentials": {"erp_token": "rotated"}}}
    fresh = Acquisition(SourceCollector(db, changed))
    with pytest.raises(AcquisitionPending, match="credential_binding_changed"):
        fresh.claim()
    assert work(db)["unknown_draft"]


@pytest.mark.parametrize('code',['ETIMEDOUT','ECONNRESET','ENOTFOUND','EAI_AGAIN','ENETUNREACH','EHOSTUNREACH'])
async def test_node_network_failures_remain_retryable_reads(setup,code):
    from flowhub.acquisition import classify
    db,context=setup
    error=SourceAcquisitionFailure(code,{'not_sent':False})
    assert classify(error)=='network'
    assert classify(error,write=True)=='write_unknown'
    async def call(*args,**kwargs):raise error
    await step(db,context,call)
    w=work(db)
    assert w['category']=='network' and not w.get('manual')
    assert w['due']>time.time()
    assert not w['unknown_favorite'] and not w['unknown_draft']
