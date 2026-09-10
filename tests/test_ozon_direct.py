import copy

import pytest

from flowhub.db import Database
from flowhub.modules import ModuleError, ModuleHost
from flowhub.ozon_direct import OzonDirectPublisher


@pytest.fixture
def port(tmp_path):
    item = dict(
        source_key="123",
        name="Массажная подушка",
        description_category_id=30960284,
        type_id=97894,
        currency_code="CNY",
        vat="0",
        depth=100,
        width=100,
        height=100,
        dimension_unit="mm",
        weight=400,
        weight_unit="g",
        images=["https://example.com/a.jpg"],
        attributes=[{"id": 85, "values": [{"dictionary_value_id": 5, "value": "Нет бренда"}]}],
    )
    item["provenance"] = {k: "test supplier specification" for k in item}
    c = dict(
        owner="owner",
        idempotency_key="test-offer",
        candidate={"source_key": "123", "price": 100, "origin": {"ozon_dossier": item}},
        rules={"stock": 99},
        store={
            "id": "one",
            "config": {"warehouse_id": "88"},
            "credentials": {"client_id": "12", "api_key": "test"},
        },
    )
    p = OzonDirectPublisher(c, Database(tmp_path))
    p.calls = []
    p.product_exists = False
    p.timeout = False

    async def seller(path, body):
        p.calls.append((path, body))
        if path == "/v3/product/import":
            if p.timeout:
                raise TimeoutError("response lost")
            return {"result": {"task_id": 77}}
        return {
            "/v2/warehouse/list": {"warehouses": [{"warehouse_id": 88, "status": "active"}]},
            "/v3/product/info/list": {
                "items": [{"id": 44, "sku": 55, "offer_id": "test-offer"}] if p.product_exists else []
            },
            "/v1/description-category/attribute": {
                "result": [{"id": 85, "name": "Бренд", "is_required": True, "dictionary_id": 1}]
            },
            "/v1/description-category/attribute/values/search": {
                "result": [{"id": 5, "value": "Нет бренда"}]
            },
            "/v4/product/info/limit": {
                "daily_create": {"limit": 100, "usage": 0, "reset_at": "2099-01-01T00:00:00Z"}
            },
            "/v1/product/import/info": {
                "result": {"items": [{"offer_id": "test-offer", "status": "imported"}]}
            },
        }[path]

    p.seller = seller
    return p


async def test_local_missing_fields_do_not_touch_external_services(port):
    port.c["candidate"]["origin"] = {}
    r = await port.invoke("prepare")
    assert r["needs_input"] and not r["ready"]
    assert any("vat" in x for x in r["missing"])
    assert port.calls == []


async def test_official_only_create_task_reconcile_and_frozen_payload(port):
    port.c["prepared"] = await port.invoke("prepare")
    assert port.c["prepared"]["ready"]
    assert await port.invoke("publish") == {"accepted": True, "task_id": "77"}
    port.product_exists = True
    assert (await port.invoke("reconcile"))["found"]
    assert any(path == "/v1/product/import/info" for path, _ in port.calls)
    payload = next(b for path, b in port.calls if path == "/v3/product/import")["items"][0]
    assert payload["offer_id"] == "test-offer" and "stock" not in payload
    assert payload["attributes"][0]["values"][0]["dictionary_value_id"] == 5


async def test_lost_response_and_restart_never_replay(port):
    port.c["prepared"] = await port.invoke("prepare")
    port.timeout = True
    with pytest.raises(TimeoutError):
        await port.invoke("publish")
    restarted = OzonDirectPublisher(copy.deepcopy(port.c), port.db)
    restarted.seller = port.seller
    assert (await restarted.invoke("publish"))["unknown"]
    assert not (await restarted.invoke("reconcile"))["found"]
    assert sum(path == "/v3/product/import" for path, _ in port.calls) == 1


async def test_changed_store_or_price_blocks_before_write(port):
    port.c["prepared"] = await port.invoke("prepare")
    port.c["candidate"]["price"] = 101
    with pytest.raises(ModuleError, match="changed"):
        await port.invoke("publish")
    port.c["candidate"]["price"] = 100
    port.c["store"]["id"] = "other"
    with pytest.raises(ModuleError, match="identity"):
        await port.invoke("publish")
    assert not any(path == "/v3/product/import" for path, _ in port.calls)


async def test_unknown_dictionary_and_missing_required_attributes(port):
    raw = port.c["candidate"]["origin"]["ozon_dossier"]
    raw["attributes"][0]["values"][0]["dictionary_value_id"] = 999
    assert (await port.invoke("prepare"))["needs_input"]
    raw["attributes"] = [{"id": 404, "values": [{"value": "x"}]}]
    result = await port.invoke("prepare")
    assert result["needs_input"] and any("85" in x for x in result["missing"])


async def test_existing_offer_is_never_overwritten(port):
    port.product_exists = True
    with pytest.raises(ModuleError, match="overwrite"):
        await port.invoke("prepare")


async def test_registered_module_missing_dossier(port):
    port.c["candidate"]["origin"] = {}
    result = await ModuleHost(port.db).invoke({"driver": "ozon-direct"}, "prepare", port.c)
    assert result["needs_input"]
    with port.db.connect() as db:
        assert (
            db.execute("SELECT driver FROM modules WHERE id='ozon-direct-publisher'").fetchone()[0]
            == "ozon-direct"
        )


def test_assembler_reuses_evidence_without_inventing_identity(port):
    from flowhub.ozon_direct import assemble

    port.c["candidate"]["origin"] = {}
    port.c["candidate"]["title"] = "known title"
    port.c["candidate"]["image"] = "https://example.com/known.jpg"
    port.c["match"] = {
        "evidence": {
            "profit": {
                "input": {
                    "sell_price": 100,
                    "package_length": 12.3,
                    "package_width": 5,
                    "package_height": 2,
                    "package_weight": 80,
                }
            }
        }
    }
    result = assemble(port.c)
    assert result["depth"] == 123 and result["weight"] == 80
    assert result["currency_code"] == "CNY"
    assert "vat" not in result and "attributes" not in result
    assert result["provenance"]["depth"].endswith("package_length")


async def test_collected_dossier_survives_worker_phase_boundary(port, monkeypatch):
    import flowhub.source_detail as source
    from flowhub.source_detail import SourceCollector

    raw = port.c["candidate"]["origin"]["ozon_dossier"]
    port.c["candidate"]["origin"] = {}
    port.keys["erp_token"] = "test-source-token"
    original_context = copy.deepcopy(port.c)

    async def collect(self):
        return {"source_key": "123"}

    async def map_detail(snapshot, context, seller):
        return {"dossier": raw, "issues": [], "required_missing": [], "mapping_version": 2}

    monkeypatch.setattr(SourceCollector, "collect", collect)
    monkeypatch.setattr(source, "map_detail", map_detail)
    prepared = await port.invoke("prepare")
    assert prepared["ready"] and prepared["source_dossier"]["attributes"]
    restarted = OzonDirectPublisher(original_context | {"prepared": prepared}, port.db)
    restarted.seller = port.seller
    assert (await restarted.invoke("publish"))["accepted"]


async def test_account_vat_overrides_source_and_freezes_with_payload(port):
    import json

    path = port.db.directory / "store-vat.json"
    path.write_text(json.dumps({"12": {"client_id": "12", "vat": "0", "source": "own ERP products"}}))
    port.c["candidate"]["origin"]["ozon_dossier"]["vat"] = "0.2"
    direct = OzonDirectPublisher(port.c, port.db)
    direct.seller = port.seller
    prepared = await direct.prepare()
    assert prepared["ready"] and prepared["frozen"]["item"]["vat"] == "0"
    path.write_text(
        json.dumps({"12": {"client_id": "12", "vat": "0.1", "source": "changed own configuration"}})
    )
    changed = OzonDirectPublisher(port.c | {"prepared": prepared}, port.db)
    changed.seller = port.seller
    with pytest.raises(ModuleError, match="changed"):
        await changed.invoke("publish")


def test_vat_is_not_shared_with_other_accounts(port):
    import json

    from flowhub.store_vat import configured_vat

    path = port.db.directory / "store-vat.json"
    path.write_text(json.dumps({"12": {"client_id": "12", "vat": "0", "source": "own ERP products"}}))
    assert configured_vat(port.db, "other") is None
    path.write_text(json.dumps({"12": {"client_id": "wrong", "vat": "0", "source": "own ERP products"}}))
    with pytest.raises(ModuleError):
        configured_vat(port.db, "12")


async def test_english_collector_label_is_blocked_before_publication(port):
    port.c["candidate"]["origin"]["ozon_dossier"]["name"] = "Folder A3"
    result = await port.prepare()
    assert not result["ready"] and any("俄文" in x for x in result["missing"])
    assert all(path != "/v3/product/import" for path, _ in port.calls)


@pytest.mark.parametrize("timeout", [False, True])
async def test_stock_receipt_preserves_rejection_vs_unknown_without_retry(port, monkeypatch, timeout):
    from flowhub.maozi import MaoziPublisher

    calls = 0

    async def seller(self, path, body):
        nonlocal calls
        calls += 1
        if timeout:
            raise TimeoutError()
        return {"result": [{"updated": False, "errors": [{"code": "SKU_NOT_READY"}]}]}

    monkeypatch.setattr(MaoziPublisher, "seller", seller)
    if timeout:
        with pytest.raises(TimeoutError):
            await OzonDirectPublisher.seller(port, "/v2/products/stocks", {"stocks": []})
    else:
        await OzonDirectPublisher.seller(port, "/v2/products/stocks", {"stocks": []})
    with port.db.connect() as db:
        row = db.execute("SELECT state,body FROM direct_stock_receipts").fetchone()
    assert row["state"] == ("unknown" if timeout else "responded")
    record = port.db.open(row["body"])
    assert ("error_type" in record) == timeout and calls == 1
    if not timeout:
        assert record["response"]["result"][0]["updated"] is False


async def title_rejected_port(port, monkeypatch):
    import flowhub.compat
    from flowhub.ozon_direct import digest

    port.c["prepared"] = await port.prepare()
    frozen = port.c["prepared"]["frozen"]
    frozen["item"]["name"] = "Folder A3"
    frozen["item"]["attributes"].append({"id": 4180, "values": [{"value": "Папка А3"}]})
    port.c["prepared"]["digest"] = digest(frozen)
    with port.db.connect() as db:
        db.execute(
            "INSERT INTO ozon_direct_writes VALUES(?,?,?,?,?,?)",
            ("test-offer", "owner", digest(port.binding()), digest(frozen), "10", 1),
        )
    error = {
        "code": "DESCRIPTION_DECLINE",
        "attribute_id": 4180,
        "description": "Название товара не может быть на латинице",
    }
    port.recovery_errors = [error]
    port.guard_blocked = False
    port.created = False
    original = port.seller

    async def guard(context):
        if port.guard_blocked:
            raise ModuleError("delist blocked")

    monkeypatch.setattr(flowhub.compat, "guard", guard)

    async def seller(path, body):
        if path == "/v1/product/import/info":
            return {"result": {"items": [{"offer_id": "test-offer", "errors": port.recovery_errors}]}}
        if path == "/v3/product/info/list":
            return {
                "items": [
                    {
                        "id": 44,
                        "offer_id": "test-offer",
                        "sku": 55 if port.created else 0,
                        "statuses": {"is_created": port.created, "moderate_status": "declined"},
                        "errors": port.recovery_errors,
                    }
                ]
            }
        return await original(path, body)

    port.seller = seller


async def test_title_decline_repairs_exact_offer_once_and_persists_task(port, monkeypatch):
    await title_rejected_port(port, monkeypatch)
    result = await port.invoke("reconcile")
    assert result["repair"] == "submitted"
    payload = [b for p, b in port.calls if p == "/v3/product/import"][0]["items"][0]
    old = port.c["prepared"]["frozen"]["item"]
    assert payload == old | {"name": "Папка А3"}
    with port.db.connect() as db:
        assert db.execute("SELECT task_id FROM ozon_direct_writes").fetchone()[0] == "77"
    assert (await port.invoke("reconcile"))["issue"]  # repeated rejection never loops
    assert sum(p == "/v3/product/import" for p, _ in port.calls) == 1
    port.recovery_errors = []
    port.created = True
    assert (await port.invoke("reconcile"))["found"]


async def test_title_repair_unknown_write_is_never_replayed(port, monkeypatch):
    await title_rejected_port(port, monkeypatch)
    port.timeout = True
    with pytest.raises(TimeoutError):
        await port.invoke("reconcile")
    restarted = OzonDirectPublisher(copy.deepcopy(port.c), port.db)
    restarted.seller = port.seller
    assert (await restarted.invoke("reconcile"))["repair"] == "started"
    assert sum(p == "/v3/product/import" for p, _ in port.calls) == 1


@pytest.mark.parametrize("reason", ["delist", "created", "unrelated", "binding", "no_russian"])
async def test_title_repair_never_overwrites_blocked_or_unrelated_product(port, monkeypatch, reason):
    from flowhub.ozon_direct import digest

    await title_rejected_port(port, monkeypatch)
    if reason == "delist":
        port.guard_blocked = True
    elif reason == "created":
        port.created = True
    elif reason == "unrelated":
        port.recovery_errors.append({"code": "INVALID_PRICE"})
    elif reason == "binding":
        port.c["store"]["id"] = "other"
    else:
        frozen = port.c["prepared"]["frozen"]
        frozen["item"]["attributes"] = []
        port.c["prepared"]["digest"] = digest(frozen)
        with port.db.connect() as db:
            db.execute("UPDATE ozon_direct_writes SET payload_hash=?", (digest(frozen),))
    if reason in ("delist", "binding"):
        with pytest.raises(ModuleError):
            await port.invoke("reconcile")
    else:
        assert (await port.invoke("reconcile"))["issue"]
    assert all(p != "/v3/product/import" for p, _ in port.calls)
