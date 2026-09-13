"""Optional local migration adapter; never shipped as a requirement of the generic engine."""

import asyncio
import json
import os
import sys
from pathlib import Path

from .modules import ModuleError


async def invoke(operation, context, token, *, source=None):
    if not token:
        raise ModuleError("workspace ERP token required")
    root = Path(os.environ.get("FLOWHUB_LEGACY_ROOT", "/Users/mac/Desktop/ozon"))
    bridge = root / "FlowEF-production/bridges/flowb.mjs"
    if operation == "profit":
        report = context["match"]["evidence"]
        assessment = report["profit"]["assessment"]
        return dict(
            cost_return=assessment["erp_profit_rate_pct"],
            total_cost=assessment["total_cost_cny"],
            logistics=report["profit"]["input"]["logistics"],
            route_available=True,
            observed_at=__import__("time").time(),
            sell_price=report["profit"]["sell_price_cny"],
        )
    args = (
        {"action": "candidates"}
        if operation == "candidates"
        else {"action": "evaluate", "product": context["candidate"]["origin"]}
    )
    if source is not None:
        bridge = Path(__file__).resolve().parents[1] / "bridges/comparebot.mjs"
        args = {"action": "comparebot_evaluate", "product": context["candidate"]["origin"], "source": source}
    if operation == "feedback":
        args = {
            "action": "feedback",
            "product": context["candidate"]["origin"],
            "source": context["match"]["evidence"]["source"],
        }
    env = os.environ | {
        "MAOZI_ACCESS_TOKEN": token,
        "FLOWEF_LEGACY_ROOT": str(root),
        "FLOW_EF_CONFIG_FILE": str(root / "flow_b_ef/state/config.json"),
        "MAOZI_HTTP_BACKEND": "native",
        "MAOZI_EXPANSION_SOURCE_FILE": str(root / "flow_b_ef/state/source.json"),
        "MAOZI_REQUIRE_FEISHU_DELIST": "1",
    }
    p = await asyncio.create_subprocess_exec(
        "node",
        str(bridge),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(json.dumps(args).encode()), 120)
    except BaseException:
        if p.returncode is None:
            p.kill()
            await p.wait()
        raise
    d = json.loads(out)
    if not d.get("ok"):
        message = str(d.get("error", {}).get("message", ""))
        if operation == "match" and "No eligible logistics route" in message:
            return {"rejected": True}
        if operation == "match" and message == "Local official commission or cost inputs unavailable":
            return {"manual_review": True, "reason": "commission_or_cost_inputs_missing"}
        error = ModuleError("legacy adapter unavailable")
        error.private_detail = message
        raise error
    r = d["result"]
    if operation == "feedback":
        if r.get("blocked") or r.get("rejected"):
            raise ModuleError("current human feedback prohibits listing")
        return r
    if operation == "candidates":
        products = [
            x
            for x in r["products"]
            if not (root / "maozi_direct_new_method/state/global-sku-claims" / f"{x['sku']}.json").exists()
        ]
        # A cursor is an offset in the refreshed source view; exhausted views wrap for future discovery.
        offset = int(context.get("cursor") or 0)
        if offset >= len(products):
            offset = 0
        rows = products[offset : offset + 10]
        return {
            "items": [
                dict(
                    source_key=str(x["sku"]),
                    title=x["title"],
                    image=x["cover_image"],
                    price=max(0.01, float(x.get("average_price_rub") or 1)),
                    weight_g=0,
                    dimensions_cm=[0, 0, 0],
                    pure_fbs=True,
                    origin=x,
                )
                for x in rows
            ],
            "cursor": str(offset + len(rows)),
        }
    evaluation_only = r.get('evaluation_only_verified') is True and context.get('candidate',{}).get('origin',{}).get('profit_evaluation_only') is True
    if r.get("rejected") or (not r.get("fbs", {}).get("verified") and not evaluation_only):
        return {"rejected": True, "reason": str(r.get("rejected") or "source_unverified"), "evidence": r}
    source = r["source"]
    img = source["selected_offer_image"]
    return dict(
        supplier_id=str(source["selected_offer_id"]),
        supplier_url=source["selected_offer_url"],
        image=source.get("selected_image_url") or context["candidate"]["image"],
        purchase=source["selected_cost_cny"],
        score=img["score"],
        dhash=img.get("dhash_score"),
        observed_at=__import__("time").time(),
        evidence=r,
    )


async def guard(context):
    """Current local migration must honor live Feishu delists and historical quarantine."""
    root = Path(os.environ.get("FLOWHUB_LEGACY_ROOT", "/Users/mac/Desktop/ozon"))
    code = """
import asyncio,json,sys,tomllib,os
from pathlib import Path
import httpx
from flowef.adapters.feishu.delist_feedback import FeishuDelistFeedback
from flowef.adapters.persistence.production_blocks import ProductionBlocks
async def main():
 c=json.load(sys.stdin);root=Path(c['root'])
 if os.environ.get('FLOWHUB_FEISHU_CONFIG'):
  f=json.loads(Path(os.environ['FLOWHUB_FEISHU_CONFIG']).read_text());args=['-p',f['token'],'-a',f['app_token']]
 else:
  args=tomllib.loads((Path.home()/'.codex/config.toml').read_text())['mcp_servers']['feishu_base']['args']
 async with httpx.AsyncClient(base_url='https://base-api.feishu.cn/open-apis/bitable/v1/',headers={'Authorization':'Bearer '+args[args.index('-p')+1]},timeout=20) as client:
  blocks=await ProductionBlocks(FeishuDelistFeedback(client,args[args.index('-a')+1]),root/'flow_b_ef/state/quarantine.json').read()
  prohibited=c['sku'] in blocks.source_skus or (c['shop'],c['offer']) in blocks.store_offers
  print(json.dumps({'allowed':not prohibited}))
asyncio.run(main())
"""
    p = await asyncio.create_subprocess_exec(
        sys.executable
        if os.environ.get("FLOWHUB_PORTABLE") == "1"
        else str(root / "FlowEF-production/.venv/bin/python"),
        "-c",
        code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=root / "FlowEF-production",
        env=os.environ | {"PYTHONPATH": str(root / "FlowEF-production/src")},
    )
    try:
        out, _ = await asyncio.wait_for(
            p.communicate(
                json.dumps(
                    {
                        "root": str(root),
                        "sku": context["candidate"]["source_key"],
                        "shop": str(context["store"]["config"]["shop_id"]),
                        "offer": context["idempotency_key"],
                    }
                ).encode()
            ),
            30,
        )
        if p.returncode != 0 or json.loads(out).get("allowed") is not True:
            raise ModuleError("explicit delist guard unavailable or blocked")
    except BaseException:
        if p.returncode is None:
            p.kill()
            await p.wait()
        raise
    await invoke("feedback", context, context["store"]["credentials"]["erp_token"])
    # Durable global claim prevents the previous production system reusing this SKU later.
    path = (
        root
        / "maozi_direct_new_method/state/global-sku-claims"
        / f"{context['candidate']['source_key']}.json"
    )
    claim = dict(
        contract="maozi-global-sku-claim-v1",
        state="flowhub_owned",
        owner="flowhub-local-acceptance",
        sku=context["candidate"]["source_key"],
        store_id=int(context["store"]["config"]["shop_id"]),
        offer_id=context["idempotency_key"],
    )
    try:
        with path.open("x") as f:
            json.dump(claim, f)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        current = json.loads(path.read_text())
        if current.get("offer_id") != claim["offer_id"] or current.get("owner") != claim["owner"]:
            raise ModuleError("SKU already owned by another publisher")
