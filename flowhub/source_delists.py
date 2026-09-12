"""Reuse the workspace's full explicit-delist reader, without changing the table."""

import asyncio
import json
import os
import sys
from pathlib import Path


async def read_delists():
    root = Path(os.environ.get("FLOWHUB_LEGACY_ROOT", Path(__file__).resolve().parents[2]))
    code = """
import asyncio,json,os,tomllib
from pathlib import Path
import httpx
from flowef.adapters.feishu.delist_feedback import FeishuDelistFeedback
async def main():
 if os.environ.get("FLOWHUB_FEISHU_CONFIG"):
  config=json.loads(Path(os.environ["FLOWHUB_FEISHU_CONFIG"]).read_text())
  token,app=config["token"],config["app_token"]
 else:
  args=tomllib.loads((Path.home()/".codex/config.toml").read_text())["mcp_servers"]["feishu_base"]["args"]
  token,app=args[args.index("-p")+1],args[args.index("-a")+1]
 async with httpx.AsyncClient(base_url="https://base-api.feishu.cn/open-apis/bitable/v1/",headers={"Authorization":"Bearer "+token},timeout=20,follow_redirects=False) as client:
  result=await FeishuDelistFeedback(client,app).read()
  print(json.dumps({"skus":list(result.source_skus),"offers":list(result.store_offers)}))
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ | {"PYTHONPATH": str(root / "FlowEF-production/src")},
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 60)
        if process.returncode:
            raise RuntimeError("explicit_delists_unavailable")
        return json.loads(out)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
