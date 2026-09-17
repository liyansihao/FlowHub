"""Isolated compareBot CLI boundary; ERP remains responsible for product/profit facts."""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from . import compat
from .modules import ModuleError, image_url, number

ROOT = Path(__file__).resolve().parents[1]

def product_size(candidate):
    def positive(value):
        try:
            return number(value, 0.000001)
        except (TypeError, ValueError, ModuleError):
            return None
    weight = positive(candidate.get("weight_g"))
    price = positive(candidate.get("sell_price_cny"))
    if (weight is not None and weight >= 500) or (price is not None and price >= 135):
        return "large"
    if weight is not None and price is not None:
        return "small"
    return "unknown"


def manifest(candidate):
    origin = candidate.get("origin", {})
    return {
        "product_id": str(candidate["source_key"]),
        "title": candidate["title"],
        "image_url": image_url(candidate["image"]),
        "additional_image_urls": origin.get("additional_image_urls", []),
        "specifications": origin.get("specifications", {}),
        # Unknown size must be reviewed by Qwen.
        "size": product_size(candidate),
    }


async def screen(candidate, api_key="", *, ranking=None):
    from .cluster_compute import remote
    remote_allowed=ranking is None or os.environ.get('DASHSCOPE_BASE_URL','https://dashscope.aliyuncs.com/compatible-mode/v1')=='https://dashscope.aliyuncs.com/compatible-mode/v1'
    remote_output=await remote('screen' if ranking is not None else 'rank',{
        'manifest':manifest(candidate),'ranking':ranking,'api_key':api_key,
        'qwen_model':os.environ.get('QWEN_VL_MODEL','qwen3-vl-plus')}) if remote_allowed else None
    if remote_output is not None:
        query=(remote_output.get('search_and_rank',remote_output).get('query') or {})
        if str(query.get('product_id'))!=str(candidate['source_key']):
            raise ModuleError('Windows compute product binding mismatch')
        if ranking is not None:
            # The worker may review, but cannot replace the ranked supplier set.
            received=remote_output.get('search_and_rank',{}).get('candidates')
            if received!=ranking.get('candidates'):
                raise ModuleError('Windows compute supplier binding mismatch')
            return remote_output
        if remote_output.get('no_candidates'):
            return {'search_and_rank':{'query':manifest(candidate),'candidates':[]},
                    'decision':{'outcome':'manual_review','reason':'no_supplier_candidates','selected_offer_id':None}}
        rows=remote_output['candidates'];top=rows[0] if rows else None
        low=top is None or number(top['dinov2_similarity'],-1,1)<.63
        return {'search_and_rank':remote_output,'decision':{
            'outcome':'rejected' if low else 'manual_review',
            'reason':'dinov2_low_similarity' if low else 'awaiting_erp_facts',
            'selected_offer_id':top['candidate']['offer_id'] if top else None}}
    module_root = ROOT / "vendor/compareBot"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(module_root / "src")
    # Never inherit another workspace key.
    env.pop("DASHSCOPE_API_KEY", None)
    if api_key:
        env["DASHSCOPE_API_KEY"] = api_key
    env["PYTHON_DOTENV_DISABLED"] = "1"
    with tempfile.TemporaryDirectory(prefix="flowhub-comparebot-") as directory:
        folder = Path(directory)
        inputs, output = folder / "input.json", folder / "output.json"
        inputs.write_text(json.dumps([manifest(candidate)], ensure_ascii=False), encoding="utf-8")
        args = [
            os.environ.get("FLOWHUB_COMPAREBOT_PYTHON") or (
                str(ROOT.parent / "FlowHub-comparebot/.venv/bin/python")
                if (ROOT.parent / "FlowHub-comparebot/.venv/bin/python").is_file()
                else sys.executable
            ),
            "-m",
            "comparebot.interfaces.screen_cli" if ranking is not None else "comparebot.interfaces.cli",
            "--manifest",
            str(inputs),
            "--product-id",
            str(candidate["source_key"]),
            "--output",
            str(output),
        ]
        if ranking is not None:
            cached = folder / "ranking.json"
            cached.write_text(json.dumps(ranking, ensure_ascii=False), encoding="utf-8")
            args += ["--ranking-input", str(cached)]
        if os.environ.get("FLOWHUB_COMPAREBOT_DEVICE"):
            args += ["--device", os.environ["FLOWHUB_COMPAREBOT_DEVICE"]]
        process = None
        try:
            if os.environ.get('FLOWHUB_COMPAREBOT_WARM') == '1':
                from .comparebot_process import ScreeningFailure, screen as warm_screen
                options = dict(manifest=str(inputs), output=str(output), product_id=str(candidate['source_key']),
                               top_k=10, device=os.environ.get('FLOWHUB_COMPAREBOT_DEVICE'))
                if ranking is not None:
                    options.update(ranking_input=str(cached), size=None, high_threshold=.86, medium_threshold=.63,
                                   qwen_match_min_similarity=.82, qwen_mismatch_max_similarity=.64,
                                   qwen_model=os.environ.get('QWEN_VL_MODEL','qwen3-vl-plus'),
                                   qwen_base_url=os.environ.get('DASHSCOPE_BASE_URL','https://dashscope.aliyuncs.com/compatible-mode/v1'))
                try:
                    await warm_screen('screen' if ranking is not None else 'rank', options, api_key)
                except ScreeningFailure as error:
                    if ranking is None and error.diagnostic.get('code') == 'no_candidates':
                        return {'search_and_rank': {'query': manifest(candidate), 'candidates': []},
                                'decision': {'outcome': 'manual_review', 'reason': 'no_supplier_candidates',
                                             'selected_offer_id': None}}
                    raise
            else:
                with (folder/'screen-error.log').open('wb') as error_log:
                    process = await asyncio.create_subprocess_exec(
                        *args, cwd=folder, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=error_log)
                    await asyncio.wait_for(process.wait(), 70 if ranking is not None else 90)
            if (process and process.returncode) or not output.is_file():
                diagnostic=folder/'screen-error.log'
                if ranking is None and diagnostic.is_file() and 'RuntimeError: 1688 image search returned no candidates' in diagnostic.read_text(errors='replace'):
                    return {'search_and_rank':{'query':manifest(candidate),'candidates':[]},
                            'decision':{'outcome':'manual_review','reason':'no_supplier_candidates','selected_offer_id':None}}
                raise ModuleError(
                    "compareBot failed: check its Python environment, model and image-search connection"
                )
            if output.stat().st_size > 2_000_000:
                raise ModuleError("oversized compareBot response")
            result = json.loads(output.read_text(encoding="utf-8"))
            if ranking is not None:
                return result
            rows = result["candidates"]
            top = rows[0] if rows else None
            low = top is None or number(top["dinov2_similarity"], -1, 1) < 0.63
            return {"search_and_rank": result, "decision": {
                "outcome": "rejected" if low else "manual_review",
                "reason": "dinov2_low_similarity" if low else "awaiting_erp_facts",
                "selected_offer_id": top["candidate"]["offer_id"] if top else None,
            }}
        except BaseException:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise


def source_from_result(result, candidate, *, evaluation_only=False):
    ranking = result["search_and_rank"]
    if str(ranking["query"]["product_id"]) != str(candidate["source_key"]):
        raise ModuleError("compareBot product binding mismatch")
    decision = result["decision"]
    outcome = decision["outcome"]
    if outcome not in ("approved", "manual_review", "rejected"):
        raise ModuleError("unknown compareBot decision")
    if outcome != "approved" and not (evaluation_only and outcome == "manual_review"):
        return {
            "manual_review": outcome == "manual_review",
            "rejected": outcome == "rejected",
            "reason": decision.get("reason", outcome),
            "evidence": {"comparebot": result},
        }
    selected = [
        r
        for r in ranking["candidates"]
        if str(r["candidate"]["offer_id"]) == str(decision["selected_offer_id"])
    ]
    if len(selected) != 1:
        raise ModuleError("compareBot selected offer missing or duplicated")
    row = selected[0]
    score = number(row["dinov2_similarity"], -1, 1)
    review = decision.get("qwen_review") or {}
    if not evaluation_only and (review.get("brand_or_model_conflict") or not (
        (score >= 0.86 and ranking["query"].get("size") == "small" and product_size(candidate) == "small")
        or (score >= 0.82 and review.get("verdict") == "match")
    )):
        raise ModuleError("compareBot approval does not satisfy screening policy")
    offer = row["candidate"]
    offer_id = str(offer["offer_id"])
    if (
        not re.fullmatch(r"[0-9]+", offer_id)
        or offer["offer_url"] != f"https://detail.1688.com/offer/{offer_id}.html"
    ):
        raise ModuleError("compareBot supplier URL mismatch")
    try:
        purchase = number(offer["price_cny"], 0.01)
    except (TypeError, ValueError, KeyError, ModuleError):
        return {"manual_review": True, "reason": "supplier_price_missing", "evidence": {"comparebot": result}}
    return dict(
        selected_offer_id=offer_id,
        selected_offer_url=offer["offer_url"],
        selected_title=offer["title"],
        selected_cost_cny=purchase,
        selected_image_url=image_url(offer["image_url"]),
        selected_offer_image={"available": True, "score": number(row["dinov2_similarity"], 0, 1)},
        comparebot=result,
        evaluation_only=evaluation_only,
    )


async def invoke(operation, context, secret=""):
    if operation != "match":
        raise ModuleError("unsupported compareBot operation")
    # Preserve plain ERP token and encrypted per-workspace Qwen credentials.
    credentials = json.loads(secret) if secret.lstrip().startswith("{") else {"erp_token": secret}
    if not credentials.get("erp_token"):
        raise ModuleError("workspace ERP token required")
    candidate = context["candidate"]
    began=time.perf_counter()
    ranked = await screen(candidate)
    timing={"dino_search_seconds":time.perf_counter()-began}
    source = source_from_result(ranked, candidate, evaluation_only=True)
    if source.get("manual_review") or source.get("rejected"):
        return source
    began=time.perf_counter()
    evaluated = await compat.invoke("match", context, credentials["erp_token"], source=source)
    timing["profit_seconds"]=time.perf_counter()-began
    if evaluated.get("rejected") or evaluated.get("manual_review"):
        return evaluated
    evidence = evaluated["evidence"]
    profit = evidence["profit"]
    if number(profit["assessment"]["erp_profit_rate_pct"], -100, 100000) < 25:
        return {"rejected": True, "reason": "profit_below_25", "evidence": evidence}
    enriched = candidate | {
        "weight_g": profit["input"]["package_weight"],
        "sell_price_cny": profit["sell_price_cny"],
    }
    began=time.perf_counter()
    result = await screen(enriched, credentials.get("dashscope_api_key", ""), ranking=ranked["search_and_rank"])
    timing["qwen_screen_seconds"]=time.perf_counter()-began
    evidence["module_timings"]=timing
    approved = source_from_result(result, enriched)
    evidence["source"]["comparebot"] = result
    if approved.get("manual_review") or approved.get("rejected"):
        return approved | {"evidence": evidence}
    evidence["source"] = approved
    return evaluated
