"""Historic stores retain their identity while opting into the working ERP route."""

import httpx
import pytest

from flowhub.maozi import MaoziPublisher


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "fallback", "expected"),
    [
        (None, None, None),
        (None, "http://127.0.0.1:7897", "http://127.0.0.1:7897"),
        ("http://127.0.0.1:9001", "http://127.0.0.1:7897", "http://127.0.0.1:9001"),
    ],
)
async def test_erp_route_fallback_preserves_explicit_store_route(
    monkeypatch, configured, fallback, expected
):
    monkeypatch.delenv("FLOWHUB_MAOZI_ERP_PROXY", raising=False)
    if fallback:
        monkeypatch.setenv("FLOWHUB_MAOZI_ERP_PROXY", fallback)
    seen = []
    original = httpx.AsyncClient

    def client_factory(**kwargs):
        seen.append(kwargs)
        return original(
            **(kwargs | {"proxy": None}),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"code": 1, "data": []})
            ),
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    publisher = MaoziPublisher({
        "store": {
            "config": {"erp_proxy": configured} if configured else {},
            "credentials": {"erp_token": "test-token"},
        }
    })
    assert await publisher.erp("GET", "/api.shop/lists") == []
    assert seen[0]["proxy"] == expected
    assert seen[0]["trust_env"] is False


@pytest.mark.asyncio
async def test_acquisition_gateway_receives_same_opt_in_fallback(monkeypatch):
    monkeypatch.setenv("FLOWHUB_MAOZI_ERP_PROXY", "http://127.0.0.1:7897")
    from flowhub import source_gateway

    seen = []

    async def fake_request(keys, path, method, params, body, *, proxy):
        seen.append((path, method, proxy))
        return {"data": []}, None

    monkeypatch.setattr(source_gateway, "request", fake_request)
    publisher = MaoziPublisher({
        "acquisition_gateway": True,
        "store": {"config": {}, "credentials": {"erp_token": "test-token"}},
    })
    await publisher.erp("GET", "/api.shop/lists")
    assert seen == [("/api.shop/lists", "GET", "http://127.0.0.1:7897")]
