"""An explicit Seller API route must not change ERP routing or injected transports."""

import httpx
import pytest

from flowhub import official_api
from flowhub.db import Database
from flowhub.maozi import MaoziPublisher


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7897"])
async def test_official_default_transport_uses_only_explicit_proxy(tmp_path, monkeypatch, proxy):
    monkeypatch.delenv("FLOWHUB_OZON_SELLER_PROXY", raising=False)
    if proxy:
        monkeypatch.setenv("FLOWHUB_OZON_SELLER_PROXY", proxy)
    seen = []

    def transport_factory(**kwargs):
        seen.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    monkeypatch.setattr(official_api.httpx, "AsyncHTTPTransport", transport_factory)
    async with official_api.client(Database(tmp_path), {"client_id": "test", "api_key": "secret"}) as api:
        response = await api.post("/v2/warehouse/list", json={})
    assert response.status_code == 200
    assert seen == [{"retries": 0, "proxy": proxy}]


@pytest.mark.asyncio
async def test_injected_official_transport_remains_authoritative(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWHUB_OZON_SELLER_PROXY", "http://127.0.0.1:7897")

    def unexpected_transport(**kwargs):
        raise AssertionError("injected transport must not be replaced")

    monkeypatch.setattr(official_api.httpx, "AsyncHTTPTransport", unexpected_transport)
    injected = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with official_api.client(
        Database(tmp_path), {"client_id": "test", "api_key": "secret"}, transport=injected
    ) as api:
        assert (await api.post("/v2/warehouse/list", json={})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7897"])
async def test_maozi_seller_uses_only_explicit_proxy(monkeypatch, proxy):
    monkeypatch.delenv("FLOWHUB_OZON_SELLER_PROXY", raising=False)
    if proxy:
        monkeypatch.setenv("FLOWHUB_OZON_SELLER_PROXY", proxy)
    seen = []
    original = httpx.AsyncClient

    def client_factory(**kwargs):
        seen.append(kwargs)
        return original(
            **(kwargs | {"proxy": None}),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"warehouses": []})),
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    publisher = MaoziPublisher({
        "store": {
            "config": {},
            "credentials": {"client_id": "test", "api_key": "secret"},
        }
    })
    assert await publisher.seller("/v2/warehouse/list", {}) == {"warehouses": []}
    assert seen[0]["proxy"] == proxy
    assert seen[0]["trust_env"] is False
