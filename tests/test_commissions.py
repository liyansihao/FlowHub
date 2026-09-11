import pytest

from flowhub.commission_cli import production_profit
from flowhub.commissions import DATA, resolve, search
from flowhub.local_profit import ProfitInput, calculate


def test_official_scope_and_exact_price_tiers():
    assert DATA["region"] == "CHN" and len(DATA["rows"]) == 10795
    base = dict(type_id="97894", brand="无品牌")
    assert resolve(sell_rub=1500, **base)["rate_pct"] == 12
    assert resolve(sell_rub=1500.01, **base)["rate_pct"] == 14
    assert resolve(sell_rub=5000, **base)["rate_pct"] == 14
    assert resolve(sell_rub=5000.01, **base)["rate_pct"] == 16
    assert resolve(sell_rub=1500, mode="FBP", **base)["rate_pct"] == 11


def test_brand_specific_rate_wins_over_all():
    special = next(r for r in DATA["rows"] if r["brand"] == "Apple")
    matching = [
        r
        for r in DATA["rows"]
        if r["brand"] == "All"
        and r["type_names"][0] == special["type_names"][0]
        and r["category_names"][0] == special["category_names"][0]
    ]
    assert matching
    actual = resolve(sell_rub=6000, row_id=matching[0]["id"], brand="Apple")
    assert actual["row_id"] == special["id"]
    assert actual["rate_pct"] == special["realFBS"][2]
    with pytest.raises(ValueError):
        resolve(sell_rub=6000, row_id=matching[0]["id"])


def test_unknown_ids_names_and_bad_inputs_never_fallback():
    for args in ({"type_id": "not-known"}, {"type_name": "相似但未知商品"}, {"row_id": "missing"}):
        with pytest.raises(ValueError):
            resolve(sell_rub=100, **args)
    with pytest.raises(ValueError):
        resolve(sell_rub=0, type_id="97894")
    assert search("按摩枕")


def test_automatic_calculator_uses_official_and_ignores_supplied_stale_rate():
    p = ProfitInput(
        provider="ChinaPost",
        sell_cny=100,
        rub_per_cny=10,
        purchase_cny=10,
        weight_g=100,
        dimensions_cm=[20, 15, 5],
        type_id="97894",
        brand="无品牌",
        commission_pct=99,
        domestic_cny=0,
        packing_cny=0,
        other_cny=0,
        ads_pct=0,
        reserve_pct=0,
    )
    r = calculate(p)
    assert r["commission_status"] == "official"
    assert r["commission_pct"] == 12
    assert r["quotes"][0]["fees_cny"]["commission"] == 12


def test_production_profit_no_network_and_fbs_mode(monkeypatch):
    import socket

    def blocked(*a, **kw):
        raise AssertionError("network prohibited")

    monkeypatch.setattr(socket, "create_connection", blocked)
    c = dict(
        product={"sku": "test", "brand": "无品牌"},
        category_data={
            "cate": [17027489, 30960284, 97894],
            "product_info": {"weight": 100, "depth": 20, "width": 15, "height": 5},
        },
        source={"selected_cost_cny": 10},
        rub_cny=0.1,
        sell_cny=100,
    )
    r = production_profit(c)
    assert r["commission"]["mode"] == "realFBS"
    assert r["calculation_source"] == "flowhub-local-postal-official-commission"
    assert r["input"]["cate_rate"] == 12
    assert r["assessment"]["cost_components"]["acquiring_fee"] == 1.9
    assert r["assessment"]["cost_components"]["other_fee"] == 1
    c["category_data"]["product_info"]["weight"] = 501
    assert production_profit(c)["rejected"] == "no_logistics_route"

@pytest.mark.parametrize("price,rate", [(1500,12),(1500.01,24),(5001,24)])
def test_price_fallback_preserves_cost_math(price,rate):
    c = dict(product={"sku":"test","brand":"无品牌"},
        category_data={"cate":[1,2,91303],"product_info":{"weight":100,"depth":20,"width":15,"height":5}},
        source={"selected_cost_cny":10},rub_cny=0.01,sell_cny=price/100)
    from flowhub.commissions import resolve_production
    assert resolve_production(sell_rub=price,type_id='91303')['rate_pct'] == rate
    r = production_profit(c)
    if price > 1500:
        # Existing postal route only accepts <=1500 RUB; fallback cannot bypass it.
        assert r == {'rejected':'no_logistics_route'}
        return
    assert r['commission']['estimated'] is True
    assert r['input']['cate_rate'] == rate
    assert r['assessment']['cost_components']['cate_fee'] == pytest.approx(round(price/100*rate/100,2), abs=0.01)
    assert r['calculation']['commission_status'] == 'estimated'
    assert r['commission']['type_name_ru'] == ''
    c['category_data']['product_info']['weight'] = 0
    with pytest.raises(ValueError):
        production_profit(c)


def test_price_fallback_keeps_exact_rate_and_rejects_invalid_price():
    from flowhub.commissions import resolve_production
    r = resolve_production(sell_rub=1600,type_id='97894',brand='无品牌')
    assert r['estimated'] is False
    assert r['rate_pct'] == 14
    for p in (0,-1,'NaN'):
        with pytest.raises(ValueError):
            resolve_production(sell_rub=p,type_id='91303')
