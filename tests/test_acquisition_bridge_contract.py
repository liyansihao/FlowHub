"""Offline bridge contract checks: fake transport, no real credentials or network."""

import json
import os
import subprocess
from pathlib import Path


def test_bridge_rejects_publication_and_online_reads_but_allows_exact_source(tmp_path):
    lib = tmp_path / "ozon-runtime/lib"
    lib.mkdir(parents=True)
    (lib / "maozi-transport.mjs").write_text("""
export function createGloballyPacedMaoziTransport(options) {
 if(options.apiIntervalMs!==2500||!options.reserveFutureSlot)throw Error('budget_changed');
 return async(path,args)=>({status:200,json:{code:1,data:{path,method:args.method}}});
}
""")
    inputs = [
        {"id": "1", "path": "/api.product.online/batch_update_stock", "method": "POST"},
        {
            "id": "2",
            "path": "/api.product.collect/detail",
            "method": "GET",
            "query": {"id": 1, "is_online": 1},
        },
        {"id": "3", "path": "/api.product.favorite/toggle", "method": "POST", "body": {"status": False}},
        {
            "id": "4",
            "path": "/api.product.collect/detail",
            "method": "GET",
            "query": {"id": 1, "is_online": 0},
        },
        {"id": "5", "path": "/api.exchange_rate/index", "method": "GET"},
    ]
    result = subprocess.run(
        ["node", str(Path(__file__).resolve().parents[1] / "bridges/acquisition-request.mjs")],
        input="".join(json.dumps(v) + "\n" for v in inputs),
        text=True,
        capture_output=True,
        timeout=5,
        env=os.environ | {"FLOWHUB_LEGACY_ROOT": str(tmp_path), "MAOZI_ACCESS_TOKEN": "fake-bridge-test"},
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [v["ok"] for v in rows] == [False, False, False, True, True]
    assert all(v["diagnostic"]["not_sent"] for v in rows[:3])
    assert "fake-bridge-test" not in result.stdout
