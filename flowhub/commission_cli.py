"""Private stdin/stdout adapter for the existing production process. No ERP calls."""

import json
import sys
from decimal import Decimal

from .commissions import resolve, resolve_production
from .local_profit import ProfitInput, calculate


def production_profit(c):
    product, category, source = c["product"], c["category_data"], c["source"]
    facts = category["product_info"]
    fx = Decimal(str(c["rub_cny"]))
    if not fx.is_finite() or fx <= 0:
        raise ValueError("invalid exchange rate")
    sell = Decimal(str(c["sell_cny"]))
    dims = [Decimal(str(facts[k])) for k in ("depth", "width", "height")]
    # Existing postal warehouse's size limits, retained from verified production carrier rules.
    if sum(dims) > 90 or max(dims) > 60:
        return {"rejected": "no_logistics_route"}
    commission = resolve_production(
        sell_rub=sell / fx, type_id=str(category["cate"][2]), brand=product.get("brand", ""), mode="realFBS"
    )
    inputs = ProfitInput(
        provider="ChinaPost",
        mode="realFBS",
        route_id="postal-extra-small",
        sell_cny=sell,
        rub_per_cny=1 / fx,
        purchase_cny=source["selected_cost_cny"],
        weight_g=facts["weight"],
        dimensions_cm=dims,
        type_id="" if commission["estimated"] else str(category["cate"][2]),
        commission_pct=commission["rate_pct"] if commission["estimated"] else None,
        commission_source=commission["source_url"],
        category=commission["name"],
        brand=product.get("brand", ""),
        domestic_cny=0,
        packing_cny=0,
        other_cny=0,
        ads_pct=0,
        reserve_pct=1,
        profit_min=30,
        eligibility_confirmed=True,
    )
    result = calculate(inputs)
    if commission["estimated"]:
        result["commission_status"] = "estimated"
        result["commission_match"] = commission
    if not result["quotes"]:
        return {"rejected": "no_logistics_route"}
    q = result["quotes"][0]
    fees = q["fees_cny"]
    cate = category["cate"][:2] + [f"{commission['tier']},{commission['rate_pct']:.2f}"]
    legacy_input = dict(
        sku=product["sku"],
        sell_price=float(sell),
        purchase_price=float(inputs.purchase_cny),
        package_weight=float(inputs.weight_g),
        package_length=float(dims[0]),
        package_width=float(dims[1]),
        package_height=float(dims[2]),
        china_fee=0,
        ad_rate=0,
        other_rate=1,
        logistics="ChinaPost",
        profit_value=30,
        profit_type="percentage",
        cate=cate,
        cate_rate=commission["rate_pct"],
    )
    price_list = dict(
        purchase_price=fees["purchase"],
        china_fee=fees["domestic"],
        logi_fee=fees["freight"],
        cate_fee=fees["commission"],
        ad_fee=fees["ads"],
        other_fee=fees["reserve"],
        wc_fee=fees["last_mile"],
        acquiring_fee=fees["acquiring"],
        withdrawal_fee=fees["withdrawal"],
        total_cost=q["total_cost"],
        profit=q["profit_cny"],
        profit_rate=q["cost_return"],
        cate_rate=commission["rate_pct"],
    )
    logistics = dict(provider="ChinaPost", speed="economy", type="Economy", delivery_day="20-25")
    return dict(
        input=legacy_input,
        selected_row=dict(
            name="ChinaPost",
            title="邮政陆运 Extra Small",
            type="Economy",
            speed="economy",
            delivery_day="20-25",
            package_weight=float(inputs.weight_g),
            price_list=price_list,
        ),
        assessment=dict(
            erp_profit_rate_pct=q["cost_return"],
            net_sales_margin_pct=q["net_margin"],
            profit_cny=q["profit_cny"],
            total_cost_cny=q["total_cost"],
            cost_components=price_list,
            missing_fields=[],
            profit_policy="chinapost-cost-return-v1",
            profit_rate_basis="profit / total_cost",
            logistics=logistics,
            ok=q["profit_pass"],
        ),
        category={"mapped": cate, "labels": [commission["name"]]},
        sell_price_cny=float(sell),
        cnyrub_rate=float(1 / fx),
        config_version=result["version"],
        calculation_source="flowhub-local-postal-estimated-commission" if commission["estimated"] else "flowhub-local-postal-official-commission",
        commission=commission,
        calculation=result,
    )


def main():
    try:
        data = json.load(sys.stdin)
        result = production_profit(data) if data.get("operation") == "production_profit" else resolve(**data)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except (ValueError, KeyError, TypeError):
        # Never default a rate or make an ERP fallback when exact mapping is unavailable.
        print(json.dumps({"ok": False, "code": "local_profit_inputs_unverified"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
