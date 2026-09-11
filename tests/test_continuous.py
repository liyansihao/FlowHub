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


async def test_sync_failure_does_not_end_reconciliation(tmp_path):
    host = ContinuousHost(Database(tmp_path))
    port = AsyncMock()
    port.find_product.return_value = None
    port.import_status.return_value = "pending"
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
        assert await host.invoke({"driver": "maozi"}, "reconcile", context) == {"found": False}
    port.sync_products.assert_awaited_once()
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
