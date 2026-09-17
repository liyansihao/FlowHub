import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from flowhub import official_api
from flowhub.db import Database


async def test_metadata_cache_survives_restart_is_credential_scoped_and_never_caches_stock(tmp_path):
    db = Database(tmp_path)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"result": [{"id": 85}]})

    keys = {"client_id": "1", "api_key": "synthetic"}
    for credentials in (keys, keys, keys | {"api_key": "rotated"}):
        async with official_api.client(db, credentials, transport=httpx.MockTransport(handler)) as api:
            await api.post("/v1/description-category/attribute", json={"type_id": 1})
            await api.post("/v2/product/info/stocks-by-warehouse/fbs", json={"sku": [2]})
    assert calls.count("/v1/description-category/attribute") == 2
    assert calls.count("/v2/product/info/stocks-by-warehouse/fbs") == 3
    assert b"synthetic" not in db.path.read_bytes()


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_cooldown_is_durable_and_other_accounts_continue(tmp_path, status):
    db = Database(tmp_path)
    calls = []

    def handler(request):
        calls.append(request.headers["Client-Id"])
        return httpx.Response(status, headers={"Retry-After": "120"}, json={})

    for number in ("1", "1", "2"):
        async with official_api.client(
            db, {"client_id": number, "api_key": "fake"}, transport=httpx.MockTransport(handler)
        ) as api:
            if number == "1" and calls:
                with pytest.raises(official_api.OfficialDeferred):
                    await api.post("/v2/warehouse/list", json={})
            else:
                await api.post("/v2/warehouse/list", json={})
    assert calls == ["1", "2"]


async def test_unknown_write_is_never_retried_by_transport(tmp_path):
    db = Database(tmp_path)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("synthetic")

    async with official_api.client(
        db, {"client_id": "1", "api_key": "fake"}, transport=httpx.MockTransport(handler)
    ) as api:
        with pytest.raises(httpx.ReadTimeout):
            await api.post("/v3/product/import", json={})
    assert calls == ["/v3/product/import"]


async def test_read_backlog_does_not_reserve_write_slots(tmp_path):
    db = Database(tmp_path)
    transport = official_api.OfficialTransport(
        db,
        {"client_id": "1", "api_key": "fake"},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with db.connect() as c:
        c.execute(
            "INSERT INTO official_api_slots VALUES(?,?,?,0)",
            (transport.account, "/v3/product/info/list", time.time() + 60),
        )
    async with httpx.AsyncClient(base_url="https://api-seller.ozon.ru", transport=transport) as api:
        with pytest.raises(official_api.OfficialDeferred):
            await api.post("/v3/product/info/list", json={})
        assert (await api.post("/v2/products/stocks", json={})).status_code == 200


class Platform:
    def __init__(self):
        self.calls = []
        self.offer = None
        self.stock = 0
        self.visible = True
        self.lost_import = False
        self.lost_stock = False
        self.archived = False

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content)
        self.calls.append((path, body))
        assert request.url.host == "api-seller.ozon.ru"
        if path == "/v2/warehouse/list":
            return httpx.Response(
                200, json={"warehouses": [{"warehouse_id": 88, "status": "active", "name": "嘉兴邮政测试"}]}
            )
        if path == "/v1/description-category/attribute":
            return httpx.Response(200, json={"result": [{"id": 85, "name": "Brand", "is_required": True}]})
        if path == "/v4/product/info/limit":
            return httpx.Response(
                200,
                json={
                    k: {"limit": 100, "usage": 0, "reset_at": "2099-01-01T00:00:00Z"}
                    for k in ("total", "daily_create")
                },
            )
        if path == "/v3/product/import":
            self.offer = body["items"][0]["offer_id"]
            if self.lost_import:
                raise httpx.ReadTimeout("response lost")
            return httpx.Response(200, json={"result": {"task_id": 77}})
        if path == "/v1/product/import/info":
            return httpx.Response(
                200, json={"result": {"items": [{"offer_id": self.offer, "status": "imported"}]}}
            )
        if path == "/v3/product/info/list":
            items = (
                [
                    {
                        "id": 44,
                        "sku": 55,
                        "offer_id": self.offer,
                        "is_archived": self.archived,
                        "statuses": {
                            "status": "processed",
                            "status_name": "selling" if self.stock else "ready_to_sell",
                        },
                        "errors": [],
                    }
                ]
                if self.offer and self.visible
                else []
            )
            return httpx.Response(200, json={"items": items})
        if path == "/v2/product/info/stocks-by-warehouse/fbs":
            return httpx.Response(
                200,
                json={
                    "products": [
                        {
                            "sku": 55,
                            "product_id": 44,
                            "offer_id": self.offer,
                            "warehouse_id": 88,
                            "present": self.stock,
                            "reserved": 0,
                        }
                    ]
                },
            )
        if path == "/v2/products/stocks":
            self.stock = body["stocks"][0]["stock"]
            if self.lost_stock:
                raise httpx.ReadTimeout("lost stock response")
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "product_id": 44,
                            "warehouse_id": 88,
                            "offer_id": self.offer,
                            "updated": True,
                            "errors": [],
                        }
                    ]
                },
            )
        if path == "/v1/product/unarchive":
            self.archived = False
            return httpx.Response(200, json={"result": True})
        if path == "/v1/product/archive":
            self.archived = True
            return httpx.Response(200, json={"result": True})
        raise AssertionError(path)


@pytest.fixture
def flow(tmp_path, monkeypatch):
    from flowhub import plugin_publication as legacy
    from flowhub.manual_reviews import schema
    from flowhub.maozi import MaoziPublisher
    from flowhub.source_detail import SourceCollector
    from flowhub.source_library import SourceLibrary

    db = Database(tmp_path / "data")
    schema(db)
    library = SourceLibrary(db)
    monkeypatch.setattr(legacy, "DATA", db.directory)
    monkeypatch.setattr(legacy, "ROOT", tmp_path)
    monkeypatch.setattr(legacy, "read_delists", AsyncMock(return_value={"skus": [], "offers": []}))
    monkeypatch.setattr(MaoziPublisher, "erp", AsyncMock(side_effect=AssertionError("ERP forbidden")))
    monkeypatch.setattr(
        SourceCollector, "collect", AsyncMock(side_effect=AssertionError("No lazy collection"))
    )
    monkeypatch.setattr("flowhub.official_source_guard.verify_unimported", AsyncMock())
    platform = Platform()
    original_client = official_api.client

    def client(db, keys, **kwargs):
        return original_client(db, keys, transport=httpx.MockTransport(platform))

    monkeypatch.setattr(official_api, "client", client)
    item = dict(
        source_key="123",
        name="Тестовый товар",
        description_category_id=10,
        type_id=20,
        currency_code="CNY",
        vat="0",
        depth=100,
        width=100,
        height=100,
        weight=400,
        dimension_unit="mm",
        weight_unit="g",
        images=["https://example.com/a.jpg"],
        attributes=[{"id": 85, "values": [{"value": "Test"}]}],
    )
    item["provenance"] = {k: "synthetic specification" for k in item}
    candidate = {
        "source_key": "123",
        "price": 100,
        "title": "Тестовый товар",
        "image": "https://example.com/a.jpg",
        "origin": {
            "seller_id": "456",
            "category_id": "10",
            "ozon_dossier": item,
            "plugin_detail": {
                "monthly_sales": {
                    "sales_schema": "FBS",
                    "blocked_by_seller": False,
                    "observed_at": time.time(),
                }
            },
        },
    }
    review = {
        "state": "matched",
        "finished_at": time.time(),
        "candidate": candidate,
        "result": {
            "supplier_id": "1",
            "purchase": 4.1,
            "evidence": {
                "source": {
                    "selected_offer_id": "1",
                    "selected_cost_cny": 4.1,
                    "comparebot": {"decision": {"outcome": "approved"}},
                },
                "profit": {
                    "sell_price_cny": 100,
                    "assessment": {"erp_profit_rate_pct": 47},
                    "input": {
                        "package_length": 10,
                        "package_width": 10,
                        "package_height": 10,
                        "package_weight": 400,
                        "sell_price": 100,
                    },
                },
            },
        },
    }
    with db.connect() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))"
        )
        owner = c.execute("SELECT id FROM users").fetchone()[0]
        c.execute(
            "INSERT INTO stores(id,owner,name,kind,config,secret,enabled,verified) VALUES(?,?,?,'maozi',?,?,1,1)",
            (
                "one",
                owner,
                "test",
                json.dumps(
                    {
                        "shop_id": "11",
                        "warehouse_id": "88",
                        "watermark_id": "9",
                        "publication_backend": "official_only",
                    }
                ),
                db.seal({"client_id": "12", "api_key": "synthetic", "erp_token": "must-not-be-used"}),
            ),
        )
        c.execute(
            "UPDATE workflows SET active_store=?,rules=? WHERE owner=?",
            ("one", json.dumps({"stock": 99, "logistics": "ChinaPost", "profit_min": 30}), owner),
        )
        c.execute(
            "INSERT INTO plugin_reviews VALUES(?,?,?,?,?,?)",
            (owner, "123", "456", "matched", json.dumps(review), time.time()),
        )
        c.execute("INSERT INTO plugin_pipeline VALUES(?,?,?,'publishing','{}',0,0)", (owner, "123", "456"))
    library.put(
        owner,
        {
            "sku": "123",
            "seller_id": "456",
            "collected_at": time.time(),
            "title": candidate["title"],
            "image": candidate["image"],
            "plugin_detail": candidate["origin"]["plugin_detail"],
        },
        {"channel": "synthetic"},
    )
    return db, owner, platform, review


async def step(flow):
    from flowhub.plugin_publication import advance

    db, owner, _, _ = flow
    return await advance(db, owner, "123", "456")


async def test_cleared_dossier_publishes_and_verifies_with_zero_erp_calls(flow):
    assert (await step(flow))["phase"] == "ready"
    assert (await step(flow))["phase"] == "reconciling"
    result = await step(flow)
    assert result["verified"] and result["phase"] == "stock_verified"
    db, _, p, _ = flow
    assert sum(path == "/v3/product/import" for path, _ in p.calls) == 1
    assert sum(path == "/v2/products/stocks" for path, _ in p.calls) == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE phase='selling'").fetchone()[0] == 1
        record = json.loads(c.execute("SELECT body FROM plugin_publications").fetchone()[0])
        assert record["backend"] == "official" and "api_key" not in json.dumps(record)


@pytest.mark.parametrize("kind", ["import", "stock"])
async def test_restart_after_lost_response_reconciles_without_replay(flow, kind):
    from flowef.application.errors import WriteOutcomeUnknown

    await step(flow)
    p = flow[2]
    if kind == "import":
        p.lost_import = True
        with pytest.raises(httpx.ReadTimeout):
            await step(flow)
    else:
        await step(flow)
        p.lost_stock = True
        with pytest.raises(WriteOutcomeUnknown):
            await step(flow)
    result = await step(flow)
    assert result["verified"]
    assert sum(path == "/v3/product/import" for path, _ in p.calls) == 1
    assert sum(path == "/v2/products/stocks" for path, _ in p.calls) == 1


async def test_legacy_record_is_never_adopted(flow, monkeypatch):
    from flowhub import plugin_publication as legacy

    db, o, _, _ = flow
    with db.connect() as c:
        c.execute(
            "INSERT INTO plugin_publications VALUES(?,?,?,?,?)",
            (o, "123", "456", json.dumps({"store_id": "one", "offer_id": "old"}), time.time()),
        )
    old = AsyncMock(return_value={"phase": "reconciling"})
    monkeypatch.setattr(legacy, "_advance", old)
    assert (await step(flow))["phase"] == "reconciling"
    old.assert_awaited_once()
    assert not flow[2].calls


@pytest.mark.parametrize("change", ["delist", "warehouse", "approval", "source", "block", "deadline"])
async def test_changed_guards_prevent_import(flow, monkeypatch, change):
    from flowhub import plugin_publication as legacy

    await step(flow)
    db, o, p, r = flow
    with db.connect() as c:
        if change == "warehouse":
            c.execute("UPDATE stores SET config=json_set(config,'$.warehouse_id','99')")
        if change == "approval":
            r["finished_at"] = 1
            c.execute("UPDATE plugin_reviews SET body=?", (json.dumps(r),))
        if change == "source":
            c.execute("UPDATE sourcing_products SET body=json_set(body,'$.title','changed')")
        if change == "block":
            c.execute("INSERT INTO blocks VALUES(?,?,?)", (o, "123", "test"))
        if change == "deadline":
            c.execute("UPDATE plugin_publications SET body=json_set(body,'$.write_deadline',1)")
    if change == "delist":
        monkeypatch.setattr(legacy, "read_delists", AsyncMock(return_value={"skus": ["123"], "offers": []}))
    with pytest.raises(ValueError):
        await step(flow)
    assert not any(path == "/v3/product/import" for path, _ in p.calls)


async def test_missing_dossier_does_not_query_or_collect(flow):
    db, o, p, r = flow
    r["candidate"]["origin"]["ozon_dossier"].pop("attributes")
    with db.connect() as c:
        c.execute("UPDATE plugin_reviews SET body=?", (json.dumps(r),))
    result = await step(flow)
    assert result["reason"] == "official_dossier_incomplete" and not p.calls


async def test_official_delist_waits_for_zero_then_archived_readback(flow):
    from flowhub.listing_controls import remove, request, schema

    for _ in range(3):
        await step(flow)
    db, o, p, _ = flow
    schema(db)
    with db.connect() as c:
        request(c, o, "123", "456", "unlist", "test", "synthetic")
    state, body = await remove(db, (o, "123", "456"), {})
    assert state == "waiting" and p.stock == 0 and not p.archived
    state, body = await remove(db, (o, "123", "456"), body)
    assert state == "waiting" and p.archived
    state, body = await remove(db, (o, "123", "456"), body)
    assert state == "unlisted" and body["official_target"]["archived_verified_at"]


async def test_concurrent_product_reads_are_batched_and_demultiplexed(tmp_path):
    import asyncio

    db = Database(tmp_path)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200, json={"items": [{"offer_id": o, "id": i + 1} for i, o in enumerate(body["offer_id"])]}
        )

    async with official_api.client(
        db, {"client_id": "1", "api_key": "fake"}, transport=httpx.MockTransport(handler)
    ) as api:
        results = await asyncio.gather(
            *(api.post("/v3/product/info/list", json={"offer_id": [o]}) for o in ("a", "b", "c"))
        )
    assert calls == [{"offer_id": ["a", "b", "c"]}]
    assert [r.json()["items"][0]["offer_id"] for r in results] == ["a", "b", "c"]


async def test_batch_rejects_foreign_product(tmp_path):
    db = Database(tmp_path)
    async with official_api.client(
        db,
        {"client_id": "1", "api_key": "fake"},
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"items": [{"offer_id": "foreign"}]})
        ),
    ) as api:
        with pytest.raises(ValueError, match="identity_mismatch"):
            await api.post("/v3/product/info/list", json={"offer_id": ["own"]})


async def test_remote_duplicate_blocks_official_import(flow, monkeypatch):
    await step(flow)
    monkeypatch.setattr(
        "flowhub.official_source_guard.verify_unimported",
        AsyncMock(side_effect=ValueError("source_already_imported")),
    )
    with pytest.raises(ValueError, match="source_already_imported"):
        await step(flow)
    assert not any(path == "/v3/product/import" for path, _ in flow[2].calls)


async def test_submitted_official_intent_no_longer_needs_erp_dedupe(flow, monkeypatch):
    await step(flow)
    await step(flow)
    blocked = AsyncMock(side_effect=AssertionError("ERP is offline"))
    monkeypatch.setattr("flowhub.official_source_guard.verify_unimported", blocked)
    assert (await step(flow))["verified"]
    blocked.assert_not_awaited()


async def test_source_dedup_short_cache_cannot_hide_a_positive_import(tmp_path, monkeypatch):
    from flowhub.official_source_guard import verify_unimported
    from flowhub.pipeline_modules.favorite_lookup import PublicationSourceAdapter

    db = Database(tmp_path)
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(PublicationSourceAdapter, "source_has_imports", check)
    keys = {"erp_token": "synthetic"}
    await verify_unimported(db, "owner", "123", keys)
    await verify_unimported(db, "owner", "123", keys)
    assert check.await_count == 1
    with db.connect() as c:
        c.execute("UPDATE official_source_checks SET checked=0")
    check.return_value = True
    with pytest.raises(ValueError, match="already_imported"):
        await verify_unimported(db, "owner", "123", keys)
    check.return_value = False
    with pytest.raises(ValueError, match="already_imported"):
        await verify_unimported(db, "owner", "123", keys)
    assert check.await_count == 2


async def test_restore_keeps_original_offer_and_checks_explicit_delists(flow, monkeypatch):
    from flowhub import plugin_publication as legacy
    from flowhub.listing_controls import prepare_listing, request, schema

    for _ in range(3):
        await step(flow)
    db, o, p, _ = flow
    schema(db)
    p.archived = True
    p.stock = 0
    with db.connect() as c:
        request(c, o, "123", "456", "list", "test", "synthetic")
    monkeypatch.setattr(legacy, "read_delists", AsyncMock(return_value={"skus": ["123"], "offers": []}))
    with pytest.raises(ValueError, match="explicit_delist"):
        await prepare_listing(db, (o, "123", "456"), {})
    assert not any(path == "/v1/product/unarchive" for path, _ in p.calls)
    monkeypatch.setattr(legacy, "read_delists", AsyncMock(return_value={"skus": [], "offers": []}))
    state, body = await prepare_listing(db, (o, "123", "456"), {})
    assert state == "waiting" and not p.archived
    state, body = await prepare_listing(db, (o, "123", "456"), body)
    assert state == "waiting" and p.stock == 99
    state, body = await prepare_listing(db, (o, "123", "456"), body)
    assert state == "listed"
    assert sum(path == "/v3/product/import" for path, _ in p.calls) == 1


async def test_pre_dispatch_cooldown_does_not_leave_false_unknown_stock(flow, monkeypatch):
    from flowef.adapters.ozon.seller_inventory import OzonSellerInventoryAdapter

    await step(flow)
    await step(flow)
    original = OzonSellerInventoryAdapter.set_stocks

    async def deferred(*args):
        raise official_api.OfficialDeferred(time.time() + 30, "official_rate_limit")

    monkeypatch.setattr(OzonSellerInventoryAdapter, "set_stocks", deferred)
    with pytest.raises(official_api.OfficialDeferred):
        await step(flow)
    db, _, p, _ = flow
    from flowhub.plugin_publication import TestListingJournal

    assert (
        TestListingJournal(db.directory / "plugin-production.sqlite3").read(p.offer)["phase"] == "reconciling"
    )
    monkeypatch.setattr(OzonSellerInventoryAdapter, "set_stocks", original)
    assert (await step(flow))["verified"]
    assert sum(path == "/v2/products/stocks" for path, _ in p.calls) == 1


async def test_erp_mutations_resolve_record_id_instead_of_using_seller_product_id():
    from types import SimpleNamespace

    from flowef.application.ports.test_listing import ListedProduct

    from flowhub.official_inventory import exact_erp_product

    official = ListedProduct("100", "100", "11", "offer", "55", "selling")
    erp = ListedProduct("999", "100", "11", "offer", "55", "selling")
    base = SimpleNamespace(find_product=AsyncMock(return_value=erp))
    assert (await exact_erp_product(base, official)).record_id == "999"
    base.find_product.return_value = ListedProduct("999", "200", "11", "offer", "55", "selling")
    with pytest.raises(ValueError, match="identity_unconfirmed"):
        await exact_erp_product(base, official)


async def test_pipeline_cooldown_keeps_attempts_and_does_not_report_old_errors(tmp_path, monkeypatch):
    from test_modular_pipeline import setup

    from flowhub import plugin_pipeline

    db, _ = setup(tmp_path)
    until = time.time() + 300
    with db.connect() as c:
        c.execute(
            "UPDATE plugin_pipeline SET state='publishing',attempts=3,body=?",
            (json.dumps({"phase": "ready", "error": "old failure"}),),
        )
    monkeypatch.setattr(
        plugin_pipeline,
        "advance",
        AsyncMock(side_effect=official_api.OfficialDeferred(until, "official_authentication")),
    )
    assert await plugin_pipeline.tick(db, lane="submit")
    with db.connect() as c:
        row = c.execute("SELECT attempts,due FROM plugin_pipeline").fetchone()
        assert row["attempts"] == 3 and row["due"] >= until
        event = c.execute(
            "SELECT outcome,details FROM pipeline_module_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["outcome"] == "waiting_dependency"
        assert json.loads(event["details"])["reason"] == "official_authentication"
