"""Offline, versioned shipping quotes. No network, ERP credentials, or publication side effects."""

import json
import time
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import commissions

D = Decimal
OFFICIAL_COMMISSION_URL = "https://docs.ozon.ru/global/zh-hans/commissions/ozon-fees/commissions/?country=CN"
TARIFFS = json.loads((Path(__file__).parent / "tariffs/shipping.json").read_text())


class ProfitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    provider: Literal["ChinaPost", "GUOO"]
    mode: Literal["realFBS", "FBP"] = "realFBS"
    route_id: str = Field(default="", max_length=80)
    sell_cny: Decimal = Field(gt=0, le=1000000)
    rub_per_cny: Decimal = Field(gt=0, le=1000)
    purchase_cny: Decimal = Field(ge=0, le=1000000)
    weight_g: Decimal = Field(gt=0, le=1000000)
    dimensions_cm: tuple[Decimal, Decimal, Decimal]
    category: str = Field(default="", max_length=200)
    commission_row_id: str = Field(default="", max_length=100)
    type_id: str = Field(default="", max_length=30)
    brand: str = Field(default="", max_length=150)
    commission_pct: Decimal | None = Field(default=None, ge=0, le=100)
    commission_source: str = Field(default="", max_length=500)
    domestic_cny: Decimal = Field(ge=0, le=1000000)
    packing_cny: Decimal = Field(ge=0, le=1000000)
    other_cny: Decimal = Field(ge=0, le=1000000)
    ads_pct: Decimal = Field(ge=0, le=100)
    reserve_pct: Decimal = Field(ge=0, le=100)
    # GUOO only quotes freight: all settlement fees must be supplied explicitly.
    acquiring_pct: Decimal | None = Field(default=None, ge=0, le=100)
    last_mile_cny: Decimal | None = Field(default=None, ge=0, le=1000000)
    withdrawal_pct: Decimal | None = Field(default=None, ge=0, le=100)
    profit_min: Decimal = Field(default=D("30"), ge=0, le=1000)
    # A numerical freight quote is not evidence of store/commodity shipping eligibility.
    eligibility_confirmed: bool = False


def money(v):
    return float(v.quantize(D(".01"), rounding=ROUND_HALF_UP))


def ceil_gram(kg):
    return (kg * 1000).to_integral_value(rounding=ROUND_CEILING) / 1000


def catalog():
    return dict(
        version=TARIFFS["version"],
        sources=TARIFFS["sources"],
        routes=[
            dict(
                id="postal-extra-small",
                provider="ChinaPost",
                mode="realFBS",
                name="邮政陆运 Extra Small · 1–500g / 1–1500₽",
            )
        ]
        + TARIFFS["routes"],
        commission=dict(
            status="official_ready",
            **commissions.metadata(),
            official_url=commissions.DATA["source_url"],
            message="已导入官方中国卖家 10,795 条类型记录，按 FBS（realFBS）、品牌与卢布价格档自动匹配。",
        ),
    )


def calculate(p: ProfitInput):
    dims = sorted(p.dimensions_cm, reverse=True)
    if any(not x.is_finite() or x <= 0 or x > 1000 for x in dims):
        raise ValueError("三边尺寸须为大于 0、不超过 1000 的厘米数")
    matched = None
    if p.commission_row_id or p.type_id or p.commission_pct is None:
        matched = commissions.resolve(
            sell_rub=p.sell_cny * p.rub_per_cny,
            mode=p.mode,
            row_id=p.commission_row_id,
            type_id=p.type_id,
            type_name=p.category,
            brand=p.brand,
        )
        p = p.model_copy(
            update={
                "commission_pct": D(str(matched["rate_pct"])),
                "commission_source": matched["source_url"],
                "category": matched["name"],
            }
        )
    elif not p.category.strip() or not p.commission_source.strip():
        raise ValueError("手动佣金须填写具体类目和来源")
    if p.provider == "GUOO" and any(x is None for x in (p.acquiring_pct, p.last_mile_cny, p.withdrawal_pct)):
        raise ValueError("GUOO 表仅提供运费，请填写收单费率、另计尾程费及提现费率（已包含可明确填 0）")
    kg = ceil_gram(p.weight_g / 1000)
    rub = p.sell_cny * p.rub_per_cny
    warnings = []
    if p.provider == "ChinaPost":
        warnings = [
            "已修正邮政原表利润漏扣收单业务费的问题。",
            "邮政表仅有 Extra Small 档，未提供尺寸、材积及禁限运条件；报价不代表承运确认。",
        ]
        candidates = [
            dict(
                id="postal-extra-small",
                name="邮政陆运 Extra Small",
                weight_min_g=1,
                weight_max_g=500,
                price_min_rub=1,
                price_max_rub=1500,
                per_kg_cny=26,
                base_cny=1.9,
                source_cell="Sheet1!C7/G7/C9",
                volume_divisor=None,
                mode="realFBS",
            )
        ]
    else:
        candidates = [r for r in TARIFFS["routes"] if r["mode"] == p.mode]
        warnings = [
            "GUOO 采用表中标注的重量/货值区间；大件在货值边界也取实重与材积重较大值，修正原表边界漏算材积。",
            "已向上取整到克；仅核算俄罗斯 Ozon 线路。仓储、包装、退货等费用需在输入中另行计入。",
            "需核实商品禁限运、电池条件及店铺实际开通线路；不会自动切换店铺物流。",
        ]
    if not p.eligibility_confirmed:
        warnings.append("承运条件未确认：可看利润，但不能据此自动放行上架。")
    quotes, unavailable = [], []
    for r in candidates:
        if p.route_id and r["id"] != p.route_id:
            continue
        reasons = []
        if p.mode != r["mode"]:
            reasons.append("配送模式不符")
        if not D(r["weight_min_g"]) <= kg * 1000 <= D(r["weight_max_g"]):
            reasons.append("重量超出该档范围")
        if not D(r["price_min_rub"]) <= rub <= D(r["price_max_rub"]):
            reasons.append("卢布货值超出该档范围")
        if "max_cm" in r and (sum(dims) > r["sum_cm"] or any(a > b for a, b in zip(dims, r["max_cm"]))):
            reasons.append("尺寸超限")
        if reasons:
            unavailable.append(dict(route_id=r["id"], name=r["name"], reasons=reasons))
            continue
        billed = (
            max(kg, ceil_gram(dims[0] * dims[1] * dims[2] / r["volume_divisor"]))
            if r["volume_divisor"]
            else kg
        )
        freight = billed * D(str(r["per_kg_cny"])) + D(str(r["base_cny"]))
        commission = p.sell_cny * p.commission_pct / 100
        acquiring = p.sell_cny * (D("1.9") if p.provider == "ChinaPost" else p.acquiring_pct) / 100
        tail = (
            min(D("18"), max(D("1.3"), p.sell_cny * D(".02")))
            if p.provider == "ChinaPost"
            else p.last_mile_cny
        )
        withdrawal = (
            max(D(0), p.sell_cny - commission - freight - acquiring - tail)
            * (D("1.2") if p.provider == "ChinaPost" else p.withdrawal_pct)
            / 100
        )
        fees = dict(
            purchase=p.purchase_cny,
            domestic=p.domestic_cny,
            packing=p.packing_cny,
            freight=freight,
            commission=commission,
            acquiring=acquiring,
            last_mile=tail,
            withdrawal=withdrawal,
            ads=p.sell_cny * p.ads_pct / 100,
            reserve=p.sell_cny * p.reserve_pct / 100,
            other=p.other_cny,
        )
        total = sum(fees.values())
        profit = p.sell_cny - total
        cost_return = profit / total * 100
        quotes.append(
            dict(
                route_id=r["id"],
                name=r["name"],
                billed_kg=float(billed),
                total_cost=money(total),
                profit_cny=money(profit),
                cost_return=money(cost_return),
                net_margin=money(profit / p.sell_cny * 100),
                profit_pass=cost_return > p.profit_min,
                route_available=p.eligibility_confirmed,
                fees_cny={k: money(v) for k, v in fees.items()},
                source_cell=r["source_cell"],
                battery_note=r.get("battery_note", "原表未说明"),
                logistics=p.provider,
                observed_at=time.time(),
            )
        )
    if p.route_id and not any(r["id"] == p.route_id for r in candidates):
        raise ValueError("所选线路不属于当前物流商及配送模式")
    quotes.sort(key=lambda q: q["total_cost"])
    return dict(
        version=TARIFFS["version"],
        sell_rub=float(rub),
        sell_cny=float(p.sell_cny),
        category=p.category,
        commission_pct=float(p.commission_pct),
        commission_source=p.commission_source,
        commission_status="official" if matched else "user_supplied",
        commission_match=matched,
        quotes=quotes,
        unavailable=unavailable,
        warnings=warnings,
    )


async def invoke(provider, operation, context):
    """Candidate modules supply per-item confirmed inputs; never inherit a global commission."""
    from .modules import ModuleError

    if operation != "profit":
        raise ModuleError("unsupported operation")
    try:
        c = context["candidate"]
        inputs = dict(c["origin"]["profit_inputs"])
        if not inputs.get("route_id") or inputs.get("eligibility_confirmed") is not True:
            raise ValueError("须确认具体线路及承运条件")
        # Canonical pipeline facts take precedence over provider-supplied copied fields.
        inputs.update(
            provider=provider,
            sell_cny=c["price"],
            purchase_cny=context["match"]["purchase"],
            weight_g=c["weight_g"],
            dimensions_cm=c["dimensions_cm"],
            profit_min=context["rules"]["profit_min"],
        )
        result = calculate(ProfitInput(**inputs))
        if not result["quotes"]:
            raise ValueError("无适用运费档")
        return result["quotes"][0] | {"calculation": result, "sell_price": float(inputs["sell_cny"])}
    except (KeyError, TypeError, ValueError):
        raise ModuleError(
            "本地利润计算缺少已确认的商品费率、费用或适用线路，请补齐候选 profit_inputs"
        ) from None
