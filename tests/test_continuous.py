import json
from unittest.mock import AsyncMock

import pytest

from flowhub import comparebot
from flowhub.continuous import ContinuousHost, ContinuousWorker
from flowhub.db import Database


@pytest.fixture(autouse=True)
def restore_screen(monkeypatch):
    monkeypatch.setattr(comparebot, "screen", comparebot.screen)


def test_offer_identity_stable_after_restart(tmp_path):
    worker = ContinuousWorker(Database(tmp_path))
    job = {"id": "fh-123", "owner": "owner", "plan": None, "data": "{}"}
    assert worker.context(job)["idempotency_key"] == "flowef-live1-fh-123"
    job["id"] = "flowef-live1-old"
    assert worker.context(job)["idempotency_key"] == "flowef-live1-old"


@pytest.mark.parametrize("status", ["pending", "found", "failed"])
async def test_reconciliation_keeps_store_binding_and_never_republishes(tmp_path, status):
    from types import SimpleNamespace

    host = ContinuousHost(Database(tmp_path))
    port = AsyncMock()
    port.find_product.return_value = (
        SimpleNamespace(product_id="7", issue_codes=[]) if status == "found" else None
    )
    port.import_status.return_value = status
    port.sync_products.side_effect = RuntimeError("sync too frequent")
    client = AsyncMock()
    host.erp = lambda _: (client, port)
    context = {
        "store": {
            "id": "store",
            "credentials": {"erp_token": "test"},
            "config": {"shop_id": "1", "warehouse_id": "2", "watermark_id": "3"},
        },
        "candidate": {
            "source_key": "4",
            "title": "Товар",
            "price": 50,
            "image": "https://example.com/a.jpg",
            "origin": {"category_id": "5"},
        },
        "match": {"purchase": 10, "supplier_id": "6"},
        "idempotency_key": "flowef-live1-test",
    }
    for _ in range(2):
        result = await host.invoke({"driver": "maozi"}, "reconcile", context)
        assert result["store_id"] == context["store"]["id"]
        assert result.get("found", False) == (status == "found")
        assert result.get("issue", False) == (status == "failed")
    assert port.sync_products.await_count == (1 if status == "pending" else 0)
    port.publish_zero.assert_not_called()
    port.set_stocks.assert_not_called()


async def test_model_reused_across_products_and_decimal_serializable(monkeypatch):
    from decimal import Decimal

    from comparebot.adapters.dinov2 import ranker
    from comparebot.application.services import search_and_rank
    from comparebot.domain.models import RankedCandidate, SearchAndRankResult, SearchCandidate

    from flowhub.comparebot_runtime import WarmScreening

    created = []

    class Model:
        def __init__(self):
            created.append(self)

    class Search:
        def __init__(self, search, images, model):
            assert model is created[0]

        async def run(self, query):
            offer = SearchCandidate(
                "123",
                "test",
                "https://example.com/b.jpg",
                "https://detail.1688.com/offer/123.html",
                1,
                price_cny=Decimal("1.25"),
            )
            return SearchAndRankResult(query, 1, 1, 0, "test", "mps", (RankedCandidate(offer, 1, 0.9),), {})

    monkeypatch.setattr(ranker, "DinoV2Ranker", Model)
    monkeypatch.setattr(search_and_rank, "SearchAndRankService", Search)
    runtime = WarmScreening()
    for sku in ("1", "2"):
        result = await runtime.screen(
            {"source_key": sku, "title": "test", "image": "https://example.com/a.jpg"}
        )
        assert result["search_and_rank"]["query"]["product_id"] == sku
        assert result["search_and_rank"]["candidates"][0]["candidate"]["price_cny"] == "1.25"
        json.dumps(result)
    assert len(created) == 1


async def test_shop_cache_reuses_reads_but_write_prechecks_are_fresh(tmp_path, monkeypatch):
    from flowhub.continuous import CachedTargetAdapter, MaoziZeroStockAdapter

    lookup = AsyncMock(side_effect=["first", "fresh", "other-owner"])
    monkeypatch.setattr(MaoziZeroStockAdapter, "target", lookup)
    cache = {}
    adapter = CachedTargetAdapter(None, cache, "owner-a")
    assert await adapter.target("shop") == "first"
    assert await adapter.target("shop") == "first"
    assert await adapter.target("shop", fresh=True) == "fresh"
    other = CachedTargetAdapter(None, cache, "owner-b")
    assert await other.target("shop") == "other-owner"
    assert lookup.await_count == 3


def test_claim_lanes_do_not_block_or_double_claim(tmp_path):
    import time

    db = Database(tmp_path)
    with db.connect() as c:
        c.execute(
            "insert into users(id,username,password,role,active,created) values(?,?,?,?,?,?)",
            ("test", "test", "unused", "admin", 1, time.time()),
        )
        c.execute(
            "insert into workflows(owner,enabled,rules,modules,secrets,updated) values(?,?,?,?,?,?)",
            ("test", 1, "{}", "{}", db.seal({}), time.time()),
        )
        for name, phase in [("screen", "queued"), ("publish", "reconciling")]:
            c.execute(
                "insert into jobs(id,owner,source_key,phase,data,modules,next_at,created,updated) values(?,?,?,?,?,?,?,?,?)",
                (name, "test", name, phase, "{}", "{}", 0, time.time(), time.time()),
            )
    worker = ContinuousWorker(db)
    worker.lane.set("fulfillment")
    assert worker.claim()["id"] == "publish"
    worker.lane.set("screening")
    assert worker.claim()["id"] == "screen"
    assert worker.claim() is None
    worker.lane.set("fulfillment")
    assert worker.claim() is None


async def test_slow_screening_does_not_block_fulfillment(tmp_path):
    import asyncio

    worker = ContinuousWorker(Database(tmp_path))
    fulfilled = asyncio.Event()
    release = asyncio.Event()

    async def step():
        if worker.lane.get() == "screening":
            await release.wait()
        else:
            fulfilled.set()
        return False

    worker.step = step
    worker.replenish = AsyncMock()
    running = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(fulfilled.wait(), 1)
        assert not release.is_set()
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running


def test_missing_stock_backs_off_without_replaying_write(tmp_path, monkeypatch):
    from flowhub.worker import Worker

    worker = ContinuousWorker(Database(tmp_path))
    moves = []
    monkeypatch.setattr(Worker, "move", lambda self, *args: moves.append(args))
    job = {"data": json.dumps({"stock_unconfirmed_checks": 5})}
    worker.move(job, "checking", delay=15)
    assert moves[0][1] == "checking"
    assert moves[0][4] == 300
    worker.move(job, "selling")
    assert moves[1][1] == "selling"
    assert moves[1][4] == 0


@pytest.mark.parametrize("operation", ["prepare", "reconcile", "stock", "check_stock", "publish"])
async def test_official_route_preserves_context_without_erp(tmp_path, monkeypatch, operation):
    import flowhub.continuous as module

    seen = []

    class Official:
        def __init__(self, context, db):
            seen.append(context)

        async def invoke(self, op):
            return {"operation": op}

    monkeypatch.setattr(module, "OzonDirectPublisher", Official)
    host = ContinuousHost(Database(tmp_path))
    host.erp = lambda _: pytest.fail("ERP must not be called")
    context = {
        "store": {"credentials": {"api_key": "test"}},
        "idempotency_key": "old-offer",
        "prepared": {"frozen": {"binding": "original"}},
    }
    assert await host.invoke({"driver": "maozi"}, operation, context) == {"operation": operation}
    assert seen == [context]


async def test_failed_call_has_private_diagnostic_and_public_timing(tmp_path, monkeypatch):
    from flowhub.worker import Worker

    worker = ContinuousWorker(Database(tmp_path))

    async def fail(*args):
        raise RuntimeError("secret-response-marker")

    monkeypatch.setattr(Worker, "call", fail)
    with pytest.raises(RuntimeError):
        await worker.call({"id": "j", "owner": "o"}, "matcher", "match")
    with worker.db.connect() as db:
        event = db.execute("select message from events where code='operation_error'").fetchone()[0]
        body = db.execute("select body from continuous_errors").fetchone()[0]
    assert "secret-response-marker" not in event
    assert "secret-response-marker" not in body
    assert worker.db.open(body)["detail"] == "secret-response-marker"
    assert json.loads(event)["operation"] == "match"
