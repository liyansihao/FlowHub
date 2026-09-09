import json

import httpx
import pytest

from flowhub.maozi import MaoziPublisher
from flowhub.modules import ModuleError


@pytest.fixture
def port():
    return MaoziPublisher(
        {
            "idempotency_key": "stable-offer",
            "candidate": {
                "source_key": "123",
                "price": 39.2,
                "image": "https://example.com/p.jpg",
                "title": "商品",
            },
            "prepared": {"favorite_id": "101"},
            "rules": {"stock": 99},
            "store": {
                "id": "internal-shop",
                "config": {"shop_id": "7", "warehouse_id": "8", "watermark_id": "9"},
                "credentials": {"erp_token": "test-token", "client_id": "10", "api_key": "test-key"},
            },
        }
    )


@pytest.mark.asyncio
async def test_native_headers_payload_and_precise_stock_identity(port, monkeypatch):
    requests = []

    def response(request):
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, body))
        if request.url.host == "api.maozierp.com":
            assert request.headers["Client"] == "pc"
            assert request.headers["Authorization"] == "Bearer test-token"
            if request.url.path == "/api.shop/lists":
                data = [{"id": 7, "status": 1, "api_client_id": "10", "watermark_id": 9}]
            else:
                data = {"accepted": True}
            return httpx.Response(200, json={"code": 1, "data": data})
        assert request.headers["Client-Id"] == "10"
        assert request.headers["Api-Key"] == "test-key"
        outputs = {
            "/v2/warehouse/list": {"warehouses": [{"warehouse_id": 8, "status": "active"}]},
            "/v4/product/info/limit": {
                "daily_create": {"limit": 100, "usage": 0, "reset_at": "2099-01-01T00:00:00Z"}
            },
            "/v3/product/info/list": {
                "items": [
                    {
                        "id": 99,
                        "sku": 100,
                        "offer_id": "stable-offer",
                        "statuses": {"status_name": "Продаётся"},
                    }
                ]
            },
            "/v2/products/stocks": {"result": [{"updated": True, "errors": []}]},
            "/v2/product/info/stocks-by-warehouse/fbs": {
                "products": [
                    {"warehouse_id": 888, "product_id": 99, "offer_id": "stable-offer", "present": 999},
                    {"warehouse_id": 8, "product_id": 99, "offer_id": "stable-offer", "present": 99},
                ]
            },
        }
        return httpx.Response(200, json=outputs[request.url.path])

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(response))
    )
    assert (await port.invoke("publish"))["accepted"]
    payload = next(body for path, body in requests if path == "/api.selection.follow/import")
    assert payload["shop_ids"] == [7]
    assert payload["watermark_id"] == 9
    assert payload["rows"][0]["offer_id"] == "stable-offer"
    assert payload["rows"][0]["source_currency"] == "CNY"
    await port.invoke("stock")
    stock = next(body for path, body in requests if path == "/v2/products/stocks")["stocks"][0]
    assert stock == {"offer_id": "stable-offer", "product_id": 99, "warehouse_id": 8, "stock": 99}
    assert await port.invoke("check_stock") == {
        "selling": True,
        "stock": 99,
        "store_id": "internal-shop",
        "warehouse_id": "8",
    }


@pytest.mark.asyncio
async def test_mismatched_official_offer_cannot_count_as_success(port):
    async def seller(path, body):
        return {"items": [{"id": 99, "sku": 100, "offer_id": "someone-else"}]}

    port.seller = seller
    with pytest.raises(ModuleError):
        await port.invoke("reconcile")
