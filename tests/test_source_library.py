import json
import time

import pytest
from fastapi.testclient import TestClient

from flowhub.api import create_app
from flowhub.db import Database, password_hash
from flowhub.source_acquisition import SourceAcquirer, archived_state, page_data
from flowhub.source_history import history_rows, import_history
from flowhub.source_library import SourceFilters, SourceLibrary, assess, ranking_product


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "data")
    return SourceLibrary(db)


def test_linked_order_history_requires_consistent_explicit_source(tmp_path):
    path = tmp_path / "order-review.json"
    fields = {
        "店铺ID": "106644",
        "跟卖源SKU": "3579750792",
        "本店货号": "opaque-offer",
        "本店Ozon SKU": "5317853180",
        "对应上架记录键": "106644|3579750792|https://www.ozon.ru/product/name-3579750792/",
        "对应上架记录": [{"record_ids": ["record1"]}],
    }
    variants = [
        fields,
        fields | {"对应上架记录": []},
        fields | {"对应上架记录": [{"record_ids": "record1"}]},
        fields | {"对应上架记录键": "106644|3579750792|https://www.ozon.ru/product/999/"},
    ]
    path.write_text(json.dumps([{"fields": item} for item in variants]))
    rows = list(history_rows(path))
    assert len(rows) == 1
    assert rows[0][1]["source_sku"] == "3579750792"
    assert rows[0][1]["linked_record_ids"] == ["record1"]
    path.write_text("[null]")
    assert list(history_rows(path)) == [(1, None)]
    path.write_text("{")
    assert list(history_rows(path)) == [(0, None)]


def product(sku="123456", seller="100", **extra):
    return ranking_product(
        dict(
            sku=sku,
            seller_id=seller,
            name="box",
            photo="https://example.com/product.jpg",
            cate2_id="22",
            sales_schema="FBS",
            avg_price="100",
            weight=50,
            sold_count=1,
        )
        | extra,
        time.time(),
    )


async def no_delists():
    return {"skus": [], "offers": []}


def test_unknown_is_not_zero_and_statistical_is_not_current():
    p = product(weight=None, sold_count=None, avg_price=None)
    decision = assess(p, SourceFilters(weight_max_g=100, sales_min=0, price_min=0))
    assert decision["state"] == "needs_review"
    assert set(decision["missing"]) == {"weight_max_g", "sales_min", "price_min"}
    assert p["current_price_rub"] is None
    assert (
        assess(product(weight=0, sold_count=0), SourceFilters(weight_max_g=100, sales_min=0))["state"]
        == "qualified"
    )
    with pytest.raises(ValueError):
        SourceFilters(price_min=200, price_max=100)


def test_evidence_dedupe_tenant_boundary_and_stale_observation(library):
    p = product()
    assert library.put("a", p, {"seed": "s"})
    assert not library.put("a", p, {"seed": "s"})
    library.put("b", product("999999"), {})
    older = p | {"collected_at": p["collected_at"] - 100, "weight_g": 999}
    assert not library.put("a", older, {})
    assert library.query("a")["items"][0]["weight_g"] == 50
    assert library.status("a")["products"] == 1
    assert library.status("a")["evidence"] == 2


def test_checkpoint_lease_and_repeat_page_rollback(library):
    library.enqueue("a", "category", {"category2": "22"})
    first = library.claim("a", 100)
    assert library.claim("a", 100) is None
    replacement = library.claim("a", 251)
    with pytest.raises(ValueError, match="lease"):
        library.commit_page(first, [{"sku": "123456"}], [product()], 5, 252)
    library.commit_page(replacement, [{"sku": "123456"}], [product()], 5, 252)
    with library.db.connect() as c:
        c.execute("UPDATE sourcing_tasks SET due=99999 WHERE kind='seller'")
    second = library.claim("a", 260)
    assert second["page"] == 2
    with pytest.raises(ValueError, match="repeated_page"):
        library.commit_page(second, [{"sku": "123456"}], [product("222222")], 5, 261)
    assert library.status("a")["products"] == 1


def test_unknown_archive_and_explicit_pagination():
    assert archived_state({"archived_type": "unknown"}) is None
    assert archived_state({"online_status": "selling"}) is False
    assert archived_state({"online_status": "selling", "archived": True}) is True
    assert page_data({"data": [{"sku": 1}], "total": 150, "per_page": 100}, 1, 100)[1] == 2
    assert page_data([{"sku": 1}], 1, 100)[1] is None
    assert page_data({"data": [{"sku": 1}] * 50, "total": 273}, 3, 100)[1] is None


def test_history_no_offer_id_guessing_and_multimonth(library, tmp_path):
    p = tmp_path / "flow_ef_category_fbs/state/flow-f-publications.jsonl"
    p.parent.mkdir(parents=True)
    rows = [
        dict(
            state="submitted",
            import_outcome="imported",
            sku="123456",
            store_id=7,
            offer_id="anything",
            at="2026-07-01",
        ),
        dict(
            state="submitted",
            import_outcome="imported",
            sku="223456",
            store_id=7,
            offer_id="second",
            at="2026-09-01",
        ),
        dict(state="submitted", sku="323456", store_id=7, offer_id="pending"),
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    assert import_history(library, "a", tmp_path)["bindings"] == 2
    assert import_history(library, "a", tmp_path)["bindings"] == 2
    assert library.status("a")["seeds"] == 2
    with library.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sourcing_seeds WHERE archived IS NULL").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_live_page_import_and_archived_exclusion(library):
    with library.db.connect() as c:
        for offer, sku in [("active", "123456"), ("archive", "223456"), ("unknown", "323456")]:
            c.execute(
                "INSERT INTO sourcing_seeds(owner,shop,offer,sku,body) VALUES(?,?,?,?,?)",
                ("a", "7", offer, sku, "{}"),
            )
    library.enqueue("a", "own_shop", {"shop_id": "7"}, 100)

    async def request(path, query, token):
        return {
            "total": 3,
            "data": [
                dict(id=1, shop_id=7, offer_id="active", sku=999999, online_status="selling"),
                dict(id=2, shop_id=7, offer_id="archive", sku=999998, online_status="archived"),
                dict(id=3, shop_id=7, offer_id="unknown", sku=999997),
            ],
        }

    await SourceAcquirer(library, request, no_delists).cycle("a", "token")
    with library.db.connect() as c:
        tasks = [json.loads(r[0]) for r in c.execute("SELECT body FROM sourcing_tasks WHERE kind='seed'")]
    assert len(tasks) == 4
    assert {r["sku"] for r in tasks} == {"123456"}


@pytest.mark.asyncio
async def test_own_sales_dedupes_and_cancellation_retracts(library):
    library.enqueue("a", "own_orders", {"shop_id": "7"})
    status = "delivered"

    async def request(path, query, token):
        return {
            "total": 1,
            "last_page": 1,
            "data": [
                dict(
                    posting_number="p",
                    shop_id=7,
                    status=status,
                    products=[dict(offer_id="offer", quantity=2)],
                )
            ],
        }

    acquirer = SourceAcquirer(library, request, no_delists)
    await acquirer.cycle("a", "token", 1000)
    with library.db.connect() as c:
        assert c.execute("SELECT SUM(quantity) FROM sourcing_orders").fetchone()[0] == 2
    await acquirer.cycle("a", "token", 23000)
    with library.db.connect() as c:
        assert c.execute("SELECT SUM(quantity) FROM sourcing_orders").fetchone()[0] == 2
    status = "cancelled"
    await acquirer.cycle("a", "token", 45000)
    with library.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sourcing_orders").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_explicit_delist_blocks_own_sku_even_with_sales(library):
    with library.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_seeds(owner,shop,offer,sku,body) VALUES(?,?,?,?,?)",
            ("a", "7", "offer", "123456", "{}"),
        )
    library.enqueue("a", "own_shop", {"shop_id": "7"})

    async def request(*args):
        return {
            "data": [
                dict(id=1, shop_id=7, offer_id="offer", sku=999999, online_status="selling", sold_count=30)
            ],
            "total": 1,
        }

    async def blocked():
        return {"skus": ["999999"], "offers": []}

    await SourceAcquirer(library, request, blocked).cycle("a", "token")
    with library.db.connect() as c:
        assert not c.execute("SELECT 1 FROM sourcing_tasks WHERE kind='seed'").fetchone()
        assert c.execute("SELECT 1 FROM blocks WHERE source_key=?", ("123456",)).fetchone()


def test_source_api_auth_and_no_secret_exposure(library):
    db = library.db
    with db.connect() as c:
        c.execute("UPDATE users SET password=?,must_change=0", (password_hash("test-password-12345"),))
    client = TestClient(create_app(db))
    assert client.get("/api/sources").status_code == 401
    assert (
        client.post("/api/login", json={"username": "admin", "password": "test-password-12345"}).status_code
        == 200
    )
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    result = client.put(
        "/api/sources/settings",
        json={"enabled": True, "erp_token": "PRIVATE_TOKEN_123", "filters": {"weight_max_g": 100}},
    )
    assert result.status_code == 200
    assert "PRIVATE_TOKEN_123" not in client.get("/api/sources").text
    assert b"PRIVATE_TOKEN_123" not in db.path.read_bytes()
    assert (
        client.put(
            "/api/sources/settings", json={"filters": {"price_min": 200, "price_max": 100}}
        ).status_code
        == 400
    )


@pytest.mark.asyncio
async def test_seller_evaluation_is_local_and_store_queue_is_unique(library):
    library.enqueue("a", "category", {"category2": "22", "mainType": "china"})
    task = library.claim("a")
    library.commit_page(task, [{"sku": "123456"}, {"sku": "123457"}], [product(), product("123457")], 1)
    with library.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sourcing_tasks WHERE kind='seller'").fetchone()[0] == 1

    async def request(*args):
        raise AssertionError("seller evaluation must reuse the product library")

    result = await SourceAcquirer(library, request, no_delists).cycle("a", "token")
    assert result["assessment"]["sample_products"] == 2
    assert result["assessment"]["sample_assessment"]["qualified"] == 2


@pytest.mark.asyncio
async def test_auth_failure_pauses_task_and_seed_revocation_stops_expansion(library):
    from flowhub.source_acquisition import AcquisitionError

    library.enqueue("a", "own_shop", {"shop_id": "7"})

    async def request(*args):
        raise AcquisitionError("authentication")

    assert (await SourceAcquirer(library, request, no_delists).cycle("a", "token"))["state"] == "blocked"
    assert library.claim("a") is None
    library.enqueue("a", "seed", {"sku": "123456", "mainType": "china"})
    result = await SourceAcquirer(library, request, no_delists).cycle("a", "token")
    assert result["reason"] == "seed_not_current"


def test_latest_live_mode_overrides_statistical_mode(library):
    p = product()
    p["live_check"] = {"observed_at": time.time(), "sales_schema": "FBO,FBS"}
    library.put("a", p, {})
    assert assess(p, SourceFilters())["state"] == "rejected"
    assert library.query("a", state="qualified")["items"] == []


def test_handoff_is_idempotent_and_holds_unknown_price(library, monkeypatch):
    from flowhub import source_delists

    monkeypatch.setattr(source_delists, "read_delists", no_delists)
    db = library.db
    with db.connect() as c:
        c.execute("UPDATE users SET password=?,must_change=0", (password_hash("test-password-12345"),))
        owner = c.execute("SELECT id FROM users").fetchone()[0]
    library.put(owner, product(), {})
    client = TestClient(create_app(db))
    client.post("/api/login", json={"username": "admin", "password": "test-password-12345"})
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    first = client.post("/api/sources/123456/handoff?seller=100").json()
    assert first["phase"] == "attention" and first["created"] is True
    second = client.post("/api/sources/123456/handoff?seller=100").json()
    assert second["created"] is False and second["job_id"] == first["job_id"]
    with db.connect() as c:
        row = c.execute("SELECT data FROM jobs WHERE id=?", (first["job_id"],)).fetchone()
    assert json.loads(row[0])["candidate"]["price"] is None
    assert client.get("/api/sources/export").json()["items"][0]["ready_for_pricing"] is False


@pytest.mark.asyncio
async def test_builtin_source_module_feeds_main_workflow_without_fake_dimensions(library):
    from flowhub.worker import Worker

    with library.db.connect() as c:
        owner = c.execute("SELECT id FROM users").fetchone()[0]
        workflow = c.execute("SELECT modules FROM workflows WHERE owner=?", (owner,)).fetchone()
        modules = json.loads(workflow[0])
        modules["candidates"] = "source-library-candidates"
        c.execute("UPDATE workflows SET enabled=1,modules=? WHERE owner=?", (json.dumps(modules), owner))
    library.put(owner, product(), {})
    await Worker(library.db).replenish()
    with library.db.connect() as c:
        job = c.execute("SELECT * FROM jobs WHERE owner=?", (owner,)).fetchone()
    assert job["phase"] == "attention"
    data = json.loads(job["data"])
    assert data["candidate"]["price"] is None
    assert data["candidate"]["dimensions_cm"] is None
    assert "current_price_rub" in data["dossier_missing"]


def test_follow_rule_and_missing_seller_are_not_default_qualified():
    filters = SourceFilters(require_follow_allowed=True)
    assert assess(product(blocked_by_seller=False), filters)["state"] == "qualified"
    assert "follow_allowed" in assess(product(blocked_by_seller=True), filters)["failed"]
    assert "follow_allowed" in assess(product(), filters)["missing"]
    assert ranking_product({"sku": "123", "seller_id": "0"}, time.time())["seller_id"] is None


def test_ranking_pushdown_and_keyword_normalization():
    from flowhub.source_acquisition import keyword_terms, ranking_query

    query = ranking_query(
        {"seed_sku": "123", "name": "розетка", "mainType": "hot"},
        3,
        SourceFilters(price_min=100, price_max=1000, weight_max_g=2000, sales_min=1),
    )
    assert query["page"] == 3 and query["sales_schema"] == "FBS"
    assert query["avg_price_min"] == 100 and query["weight_max"] == 2000
    assert "seed_sku" not in query
    assert keyword_terms([{"keyword": "#защитный_чехол"}, {"keyword": "#чехол_для_телефона"}]) == ["чехол"]


@pytest.mark.asyncio
async def test_seed_resolves_category_then_filtered_expansion(library):
    with library.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_seeds(owner,shop,offer,sku,body,archived,checked) VALUES('a','7','offer','123456','{}',0,1000)"
        )
        c.execute(
            "INSERT INTO sourcing_settings VALUES('a',1,?, '')",
            (json.dumps({"weight_max_g": 2000, "price_min": 100}),),
        )
    library.enqueue("a", "seed", {"sku": "123456", "mainType": "hot"})
    calls = []

    async def request(path, query, token):
        calls.append(query)
        row = product()["raw"] if query.get("sku") else product("234567")["raw"]
        return {"data": [row], "last_page": 1}

    acquirer = SourceAcquirer(library, request, no_delists)
    await acquirer.cycle("a", "token", 1001)
    with library.db.connect() as c:
        body = json.loads(c.execute("SELECT body FROM sourcing_seeds").fetchone()[0])
        assert body["category_id"] == "22" and body["seller_id"] == "100"
        assert c.execute("SELECT COUNT(*) FROM sourcing_tasks WHERE kind='category'").fetchone()[0] == 3
        c.execute("UPDATE sourcing_tasks SET due=9999 WHERE kind<>'category'")
    await acquirer.cycle("a", "token", 1002)
    assert calls[-1]["weight_max"] == 2000 and calls[-1]["sales_schema"] == "FBS"
    assert "weight_max" not in calls[0]  # Seed identity lookup must not hide an FBO source.
    with library.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sourcing_products").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_keyword_tasks_keep_seed_provenance_and_stop_after_archive(library):
    with library.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_seeds(owner,shop,offer,sku,body,archived,checked) VALUES('a','7','offer','123456','{}',0,1000)"
        )
    library.enqueue("a", "keyword_lookup", {"sku": "123456"})
    calls = []

    async def request(path, query, token):
        calls.append(path)
        return [{"keyword": "#защитный_чехол"}, {"keyword": "#чехол_для_телефона"}]

    acquirer = SourceAcquirer(library, request, no_delists)
    result = await acquirer.cycle("a", "token", 1001)
    assert result["terms"] == ["чехол"]
    with library.db.connect() as c:
        tasks = [json.loads(r[0]) for r in c.execute("SELECT body FROM sourcing_tasks WHERE kind='keyword'")]
        assert len(tasks) == 2 and all(t["seed_sku"] == "123456" for t in tasks)
        c.execute("UPDATE sourcing_seeds SET archived=1")
    result = await acquirer.cycle("a", "token", 1002)
    assert result["reason"] == "seed_not_current"
    assert len(calls) == 1


def test_candidate_query_excludes_history_and_uses_live_weight(library):
    now = time.time()
    library.put("a", product(), {})
    library.put(
        "a",
        product("234567", weight=3000)
        | {"live_check": {"weight_g": 50, "sales_schema": "FBS", "observed_at": now}},
        {},
    )
    library.put(
        "a",
        product("345678") | {"live_check": {"weight_g": 3000, "sales_schema": "FBS", "observed_at": now}},
        {},
    )
    with library.db.connect() as c:
        c.execute("INSERT INTO sourcing_seeds(owner,shop,offer,sku,body) VALUES('a','7','old','123456','{}')")
    page = library.query("a", SourceFilters(weight_max_g=2000), state="qualified")
    assert [p["sku"] for p in page["items"]] == ["234567"]


def test_yield_reward_counts_unique_qualified_not_raw_volume(library):
    now = time.time()
    with library.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_settings VALUES('a',0,?, '')",
            (json.dumps({"require_follow_allowed": True}),),
        )
        c.execute("INSERT INTO sourcing_seeds(owner,shop,offer,sku,body) VALUES('a','7','old','123456','{}')")
    library.enqueue("a", "category", {"category2": "22"}, 20)
    first = library.claim("a", now)
    products = [
        product(blocked_by_seller=False),
        product("234567", blocked_by_seller=True),
        product("345678", blocked_by_seller=False),
    ]
    result = library.commit_page(first, [p["raw"] for p in products], products, 3, now + 1)
    assert result["added"] == 3 and result["qualified_added"] == 1
    # A second method observing the same SKU gets no discovery credit.
    library.enqueue("a", "category", {"category2": "23"}, 20)
    with library.db.connect() as c:
        c.execute("UPDATE sourcing_tasks SET due=? WHERE id=? OR kind='seller'", (now + 999, first["id"]))
    second = library.claim("a", now + 2)
    result = library.commit_page(second, [products[-1]["raw"]], [products[-1]], 1, now + 3)
    assert result["qualified_added"] == 0
    with library.db.connect() as c:
        assert c.execute("SELECT SUM(qualified_added) FROM sourcing_yields").fetchone()[0] == 1
        c.execute("UPDATE sourcing_tasks SET due=0 WHERE kind='category'")
    assert library.claim("a", now + 4)["id"] == first["id"]


@pytest.mark.asyncio
async def test_seller_scan_keeps_only_exact_seller_and_provenance(library):
    with library.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_seeds(owner,shop,offer,sku,body,archived,checked) VALUES('a','7','offer','123456',?,0,1000)",
            (json.dumps({"seller_id": "100", "category_id": "22"}),),
        )
    library.enqueue("a", "seller_scan", {"seller_id": "100", "category2": "22", "mainType": "hot"})

    async def request(path, query, token):
        assert "seller_id" not in query
        return {"data": [product("234567")["raw"], product("345678", seller="200")["raw"]], "last_page": 1}

    result = await SourceAcquirer(library, request, no_delists).cycle("a", "token", 1001)
    assert result["rows"] == 2 and result["added"] == 1
    with library.db.connect() as c:
        p = json.loads(c.execute("SELECT body FROM sourcing_products").fetchone()[0])
    assert p["seller_id"] == "100" and p["provenance"]["coverage"] == "ranking-exact-seller"
    assert p["provenance"]["root_seeds"] == [{"sku": "123456", "shop": "7", "offer": "offer"}]


@pytest.mark.asyncio
async def test_seller_scan_requires_live_unblocked_root(library):
    with library.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_seeds(owner,shop,offer,sku,body,archived,checked) VALUES('a','7','offer','123456',?,0,1000)",
            (json.dumps({"seller_id": "100", "online_evidence": {"sku": "999999"}}),),
        )
    library.enqueue("a", "seller_scan", {"seller_id": "100", "category2": "22", "mainType": "hot"})

    async def delists():
        return {"skus": ["999999"], "offers": []}

    async def request(*args):
        raise AssertionError("A delisted root must never trigger a scan")

    result = await SourceAcquirer(library, request, delists).cycle("a", "token", 1001)
    assert result["reason"] == "seed_not_current"


def test_same_seller_filter_does_not_admit_generic_category_products(library):
    p = product()
    library.put("a", p, {})
    relation = {
        "kind": "same_seller",
        "seller_id": "100",
        "root_seeds": [{"sku": "777", "shop": "7", "offer": "x"}],
    }
    library.put("a", product("234567") | {"source_relation": relation}, {})
    library.put("a", product("345678") | {"source_relation": relation | {"seller_id": "999"}}, {})
    f = SourceFilters(same_seller_only=True)
    assert "same_seller" in assess(p, f)["missing"]
    assert [p["sku"] for p in library.query("a", f, state="qualified")["items"]] == ["234567"]


def test_generic_refresh_preserves_same_seller_evidence(library):
    relation = {
        "kind": "same_seller",
        "seller_id": "100",
        "root_seeds": [{"sku": "777", "shop": "7", "offer": "x"}],
    }
    library.put("a", product() | {"source_relation": relation}, {"channel": "same-seller"})
    old = library.query("a")["items"][0]["source_relation_evidence_hash"]
    library.put("a", product(), {"channel": "category-refresh"})
    library.put("a", product("234567"), {"channel": "generic"})
    rows = library.query("a", SourceFilters(same_seller_only=True))["items"]
    assert len(rows) == 1 and rows[0]["source_relation"] == relation
    assert rows[0]["source_relation_evidence_hash"] == old


@pytest.mark.asyncio
async def test_resolving_seed_joins_existing_seller_inventory_without_refetch(library):
    now = time.time()
    p = product("234567")
    observed = p["collected_at"]
    library.put("a", p, {"channel": "category"})
    library.put("a", product("345678", seller="200"), {"channel": "unrelated"})
    with library.db.connect() as c:
        c.execute(
            "INSERT INTO sourcing_seeds(owner,shop,offer,sku,body,archived,checked) VALUES('a','7','offer','123456','{}',0,?)",
            (now,),
        )
    library.enqueue("a", "seed", {"sku": "123456", "mainType": "hot"})
    calls = []

    async def request(path, query, token):
        calls.append(query)
        return {"data": [product()["raw"]], "last_page": 1}

    result = await SourceAcquirer(library, request, no_delists).cycle("a", "token", now + 1)
    assert len(calls) == 1 and result["cached_seller_links"] == 1
    items = library.query("a", SourceFilters(same_seller_only=True), state="qualified")["items"]
    assert [p["sku"] for p in items] == ["234567"]
    assert items[0]["collected_at"] == observed  # Joining evidence cannot refresh market statistics.
    assert items[0]["source_relation"]["root_seeds"] == [{"sku": "123456", "shop": "7", "offer": "offer"}]


def test_market_refresh_preserves_newer_listing_review_without_refreshing_review_time(library):
    p = product()
    review = {"observed_at": p["collected_at"], "label": "已回查可售，库存 99"}
    library.put("a", p | {"listing_review": review}, {"channel": "listing"})
    refreshed = p | {"collected_at": p["collected_at"] + 1, "average_price_rub": 120}
    library.put("a", refreshed, {"channel": "ranking"})
    row = library.query("a")["items"][0]
    assert row["average_price_rub"] == 120
    assert row["listing_review"] == review
    library.put("a", refreshed | {"listing_review": {"observed_at": 1, "label": "旧核查"}}, {})
    assert library.query("a")["items"][0]["listing_review"] == review
    library.put("b", refreshed, {})
    assert "listing_review" not in library.query("b")["items"][0]


def test_admission_index_keeps_exact_source_binding_rules(tmp_path):
    from flowhub.db import Database
    from flowhub.source_library import SourceLibrary
    db=Database(tmp_path);SourceLibrary(db)
    condition="""owner=? AND json_extract(body,'$.coverage') IN ('storefront-page','maozi-exact-seller-page')
        AND json_extract(body,'$.source_relation.seller_id')=json_extract(body,'$.seller_id')
        AND json_array_length(json_extract(body,'$.source_relation.root_seeds'))>0"""
    import json
    good={'coverage':'storefront-page','seller_id':'2','source_relation':{'seller_id':'2','root_seeds':[{'sku':'9'}]}}
    variants=[good,good|{'coverage':'unknown'},good|{'source_relation':{'seller_id':'3','root_seeds':[{'sku':'9'}]}},good|{'source_relation':{'seller_id':'2','root_seeds':[]}},good|{'source_relation':{}},good|{'coverage':'maozi-exact-seller-page'}]
    with db.connect() as c:
        for i,body in enumerate(variants):
            c.execute('INSERT INTO sourcing_products(owner,sku,seller,body,first_seen,updated) VALUES(?,?,?,?,0,0)',('o',str(i),'2',json.dumps(body)))
        expected=[tuple(r) for r in c.execute('SELECT sku FROM sourcing_products NOT INDEXED WHERE '+condition+' ORDER BY id',('o',))]
        actual=[tuple(r) for r in c.execute('SELECT sku FROM sourcing_products WHERE '+condition+' ORDER BY id',('o',))]
        assert actual==expected==[('0',),('5',)]
        plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM sourcing_products WHERE '+condition,('o',)))
        assert 'sourcing_admission_candidates' in plan
