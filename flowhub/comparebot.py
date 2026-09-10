"""Isolated compareBot CLI boundary; ERP remains responsible for product/profit facts."""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from . import compat
from .modules import ModuleError, image_url, number

ROOT = Path(__file__).resolve().parents[1]


def manifest(candidate):
    origin = candidate.get("origin", {})
    return {
        "product_id": str(candidate["source_key"]),
        "title": candidate["title"],
        "image_url": image_url(candidate["image"]),
        "additional_image_urls": origin.get("additional_image_urls", []),
        "specifications": origin.get("specifications", {}),
        # No size guess from weight or category: unknown routes to human review.
        "size": origin.get("size", "unknown"),
    }


async def screen(candidate, api_key=""):
    module_root = ROOT / "vendor/compareBot"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(module_root / "src")
    # A user's key never falls back to a different user's process credentials.
    env.pop("DASHSCOPE_API_KEY", None)
    if api_key:
        env["DASHSCOPE_API_KEY"] = api_key
    env["PYTHON_DOTENV_DISABLED"] = "1"
    with tempfile.TemporaryDirectory(prefix="flowhub-comparebot-") as directory:
        folder = Path(directory)
        inputs, output = folder / "input.json", folder / "output.json"
        inputs.write_text(json.dumps([manifest(candidate)], ensure_ascii=False), encoding="utf-8")
        args = [
            os.environ.get("FLOWHUB_COMPAREBOT_PYTHON", sys.executable),
            "-m",
            "comparebot.interfaces.screen_cli",
            "--manifest",
            str(inputs),
            "--product-id",
            str(candidate["source_key"]),
            "--output",
            str(output),
        ]
        if os.environ.get("FLOWHUB_COMPAREBOT_DEVICE"):
            args += ["--device", os.environ["FLOWHUB_COMPAREBOT_DEVICE"]]
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=folder,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), 140)
            if process.returncode or not output.is_file():
                raise ModuleError(
                    "compareBot failed: check its Python environment, model and image-search connection"
                )
            if output.stat().st_size > 2_000_000:
                raise ModuleError("oversized compareBot response")
            return json.loads(output.read_text(encoding="utf-8"))
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise


def source_from_result(result, candidate):
    ranking = result["search_and_rank"]
    if str(ranking["query"]["product_id"]) != str(candidate["source_key"]):
        raise ModuleError("compareBot product binding mismatch")
    decision = result["decision"]
    outcome = decision["outcome"]
    if outcome not in ("approved", "manual_review", "rejected"):
        raise ModuleError("unknown compareBot decision")
    if outcome != "approved":
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
    )


async def invoke(operation, context, secret=""):
    if operation != "match":
        raise ModuleError("unsupported compareBot operation")
    # Keep the previous plain ERP token format. JSON also supports a per-user Qwen key.
    credentials = json.loads(secret) if secret.lstrip().startswith("{") else {"erp_token": secret}
    if not credentials.get("erp_token"):
        raise ModuleError("workspace ERP token required")
    result = await screen(context["candidate"], credentials.get("dashscope_api_key", ""))
    source = source_from_result(result, context["candidate"])
    if source.get("manual_review") or source.get("rejected"):
        return source
    return await compat.invoke("match", context, credentials["erp_token"], source=source)
