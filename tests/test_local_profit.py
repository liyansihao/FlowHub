import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from flowhub.api import create_app
from flowhub.db import Database, password_hash
from flowhub.local_profit import ProfitInput, calculate, invoke
from flowhub.modules import ModuleError


def inputs(**changes):
    return (
        dict(
            provider="ChinaPost",
            sell_cny=100,
            rub_per_cny=10,
            purchase_cny=10,
            weight_g=100,
            dimensions_cm=[20, 15, 5],
            category="测试类目",
            commission_pct=15,
            commission_source="测试费率，非通用费率",
            domestic_cny=0,
            packing_cny=0,
            other_cny=0,
            ads_pct=0,
            reserve_pct=0,
        )
        | changes
    )


def result(**changes):
    return calculate(ProfitInput(**inputs(**changes)))


def guoo(**changes):
    return result(provider="GUOO", acquiring_pct=0, last_mile_cny=0, withdrawal_pct=0, **changes)


def test_postal_sample_corrects_blank_reference_and_keeps_all_fees():
    q = result()["quotes"][0]
    assert q["fees_cny"]["freight"] == 4.5
    assert q["fees_cny"]["acquiring"] == 1.9
    assert q["fees_cny"]["withdrawal"] == 0.92
    assert q["profit_cny"] == 65.68  # Original C17 67.5808 omitted G9=1.9.
    assert q["total_cost"] == 34.32
    assert q["route_available"] is False
    assert q["cost_return"] != q["net_margin"]


@pytest.mark.parametrize(
    "weight,price,expected", [(500, 150, 1), (501, 150, 0), (500, 150.01, 0), (0.1, 1, 1)]
)
def test_postal_band_and_round_up_grams(weight, price, expected):
    assert len(result(weight_g=weight, sell_cny=price)["quotes"]) == expected


def test_tail_fee_bounds_and_no_negative_withdrawal_credit():
    assert result(sell_cny=5)["quotes"][0]["fees_cny"]["last_mile"] == 1.3
    assert result(sell_cny=1000, rub_per_cny=1)["quotes"][0]["fees_cny"]["last_mile"] == 18
    assert result(sell_cny=1, commission_pct=100)["quotes"][0]["fees_cny"]["withdrawal"] == 0


def test_guoo_all_services_and_restricted_price_gap():
    q = guoo()["quotes"]
    assert len(q) == 3
    assert [r["fees_cny"]["freight"] for r in q] == [6.18, 7.3, 8.43]
    assert guoo(sell_cny=150.05)["quotes"] == []
    assert len(guoo(mode="FBP")["quotes"]) == 2


def test_guoo_weight_boundaries_no_overlapping_bands():
    assert len(guoo(weight_g=500)["quotes"]) == 3
    assert len(guoo(weight_g=500.1)["quotes"]) == 2
    assert all("Budget" in q["name"] for q in guoo(weight_g=501)["quotes"])
    assert all("Small" in q["name"] for q in guoo(weight_g=2000, sell_cny=200)["quotes"])
    assert all("Big" in q["name"] for q in guoo(weight_g=2001, sell_cny=200)["quotes"])


@pytest.mark.parametrize("price", [150.1, 700, 700.1, 25000])
def test_big_volume_at_price_endpoints(price):
    q = guoo(weight_g=6000, sell_cny=price, dimensions_cm=[100, 60, 40])["quotes"]
    assert len(q) == 2
    assert all(x["billed_kg"] == 20 for x in q)


def test_dimensions_sort_and_reject_oversize():
    assert guoo(dimensions_cm=[5, 20, 15])["quotes"]
    assert not guoo(dimensions_cm=[61, 10, 10])["quotes"]
    with pytest.raises(ValueError):
        result(dimensions_cm=[0, 10, 10])


def test_missing_commission_or_guoo_fees_never_zero_default():
    p = inputs()
    del p["commission_pct"]
    with pytest.raises(ValueError):
        calculate(ProfitInput(**p))
    with pytest.raises(ValueError):
        result(provider="GUOO")
    with pytest.raises(ValidationError):
        result(commission_pct=float("nan"))
    with pytest.raises(ValueError):
        result(route_id="not-a-route")


def test_minimum_is_strict_and_does_not_use_display_rounding():
    q = result(profit_min=1000)["quotes"][0]
    assert q["profit_pass"] is False


def test_module_uses_current_product_facts_and_requires_specific_route():
    p = inputs(eligibility_confirmed=True, route_id="postal-extra-small")
    ctx = dict(
        candidate=dict(price=100, weight_g=100, dimensions_cm=[20, 15, 5], origin={"profit_inputs": p}),
        match={"purchase": 10},
        rules={"profit_min": 30},
    )
    q = asyncio.run(invoke("ChinaPost", "profit", ctx))
    assert q["profit_cny"] == 65.68
    ctx["candidate"]["price"] = 200  # Changed price now exceeds route; old cached input must not pass.
    with pytest.raises(ModuleError):
        asyncio.run(invoke("ChinaPost", "profit", ctx))
    ctx["candidate"]["price"] = 100
    p["eligibility_confirmed"] = False
    with pytest.raises(ModuleError):
        asyncio.run(invoke("ChinaPost", "profit", ctx))


def test_api_auth_csrf_calculation_and_modules(tmp_path):
    db = Database(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE users SET password=?,must_change=0", (password_hash("test-password-12345"),))
    client = TestClient(create_app(db))
    assert client.get("/api/profit/catalog").status_code == 401
    client.post("/api/login", json={"username": "admin", "password": "test-password-12345"})
    assert client.post("/api/profit/calculate", json=inputs()).status_code == 403
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    assert len(client.get("/api/profit/catalog").json()["routes"]) == 28
    assert client.post("/api/profit/calculate", json=inputs()).json()["quotes"][0]["profit_cny"] == 65.68
    assert client.post("/api/profit/calculate", json=inputs(provider="GUOO")).status_code == 400
    assert len([m for m in client.get("/api/modules").json() if m["driver"] == "local-profit"]) == 2
    assert client.get("/api/workflow").json()["enabled"] == 0
