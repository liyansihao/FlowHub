"""Official China cross-border commission snapshot; exact names/IDs, no fuzzy rate assignment."""

import json
from decimal import Decimal
from pathlib import Path

DATA = json.loads((Path(__file__).parent / "tariffs/commissions.json").read_text())
ROWS = {r["id"]: r for r in DATA["rows"]}


def norm(text):
    return " ".join(str(text or "").casefold().split())


BY_NAME = {}
for row in DATA["rows"]:
    for name in set(map(norm, row["type_names"])):
        if name:
            BY_NAME.setdefault(name, []).append(row)


def search(query, limit=60):
    q = norm(query)
    if not q:
        return []
    found = []
    for r in DATA["rows"]:
        names = r["type_names"] + r["category_names"] + [r["brand"]]
        if any(q in norm(n) for n in names):
            found.append(
                dict(id=r["id"], name=r["type_names"][1], category=r["category_names"][1], brand=r["brand"])
            )
            if len(found) >= limit:
                break
    return found


def metadata():
    return {
        k: DATA[k]
        for k in (
            "version",
            "effective_from",
            "verified_at",
            "region",
            "currency",
            "source_url",
            "download_url",
            "sha256",
        )
    } | {"count": len(ROWS)}


def resolve(*, sell_rub, mode="realFBS", row_id="", type_id="", type_name="", category_name="", brand=""):
    price = Decimal(str(sell_rub))
    if not price.is_finite() or price <= 0 or mode not in ("realFBS", "FBP"):
        raise ValueError("佣金查询需要正数卢布价格和正确配送模式")
    if row_id:
        r = ROWS.get(row_id)
        if not r:
            raise ValueError("官方佣金类目记录不存在")
        # Keep same product type/category, resolve special brand before generic All.
        candidates = [
            x for x in BY_NAME[norm(r["type_names"][0])] if x["category_names"][0] == r["category_names"][0]
        ]
        if not brand and r["brand"] != "All":
            brand = r["brand"]
    elif type_id:
        item = DATA["taxonomy"].get(str(type_id))
        if not item or item["disabled"]:
            raise ValueError("商品类型不存在或已停用，请更新官方类型表")
        candidates = BY_NAME.get(norm(item["name"]), [])
        # A type name can occur in more than one category.
        scoped = [x for x in candidates if norm(x["category_names"][0]) == norm(item["category_name"])]
        candidates = scoped or candidates
    else:
        candidates = BY_NAME.get(norm(type_name), [])
        if category_name:
            candidates = [x for x in candidates if norm(category_name) in map(norm, x["category_names"])]
    if not candidates:
        raise ValueError("未匹配到官方商品类型，不按相似名称或大类猜佣金")
    if len({x["category_names"][0] for x in candidates}) != 1:
        raise ValueError("同名类型对应多个类目，请选择明确的官方记录")
    special = [x for x in candidates if x["brand"] != "All"]
    if special and not norm(brand):
        raise ValueError("该商品类型存在品牌专属佣金，请填写实际品牌（无品牌填无品牌）")
    chosen = [x for x in special if norm(x["brand"]) == norm(brand)]
    if not chosen:
        chosen = [x for x in candidates if x["brand"] == "All"]
    tier = 0 if price <= 1500 else 1 if price <= 5000 else 2
    if not chosen or len({x[mode][tier] for x in chosen}) != 1:
        raise ValueError("官方佣金记录不唯一或缺少当前品牌费率")
    r = chosen[0]
    return dict(
        rate_pct=r[mode][tier],
        row_id=r["id"],
        name=r["type_names"][1],
        type_name_ru=r["type_names"][0],
        brand=r["brand"],
        mode=mode,
        tier=tier + 1,
        tier_label=["≤1500 ₽", "1500–5000 ₽", ">5000 ₽"][tier],
        source_row=r["source_row"],
        source_url=DATA["source_url"],
        version=DATA["version"],
        effective_from=DATA["effective_from"],
        verified_at=DATA["verified_at"],
    )


def resolve_production(*, sell_rub, type_id, brand="", mode="realFBS"):
    """User-authorized price fallback for production; never invent a product type."""
    price = Decimal(str(sell_rub))
    if not price.is_finite() or price <= 0 or mode != "realFBS":
        raise ValueError("价格兜底需要正数卢布售价和 realFBS 模式")
    try:
        return resolve(sell_rub=price, type_id=type_id, brand=brand, mode=mode) | {"estimated": False}
    except ValueError as error:
        return dict(
            rate_pct=12.0 if price <= 1500 else 24.0,
            estimated=True,
            fallback_reason=str(error),
            name="价格档佣金估算（类目未匹配）",
            type_name_ru="",
            type_id=str(type_id),
            mode=mode,
            tier=1 if price <= 1500 else 2 if price <= 5000 else 3,
            tier_label="≤1500 ₽" if price <= 1500 else ">1500 ₽",
            source_url="user-authorized-price-fallback-20260911",
            version="price-fallback-12-24-v1",
        )
