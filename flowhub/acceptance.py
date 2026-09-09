"""Read-only live acceptance observer. No publication or stock writes exist here."""

import asyncio
import fcntl
import json
import time

from .db import Database
from .maozi import MaoziPublisher


async def check(db):
    path = db.directory / "live-acceptance.json"
    if not path.exists():
        return
    state = json.loads(path.read_text())
    with db.connect() as c:
        jobs = [
            dict(r)
            for r in c.execute("SELECT * FROM jobs WHERE owner=? AND store_id IS NOT NULL", (state["owner"],))
        ]
        heartbeat = c.execute("SELECT heartbeat FROM health WHERE name='worker'").fetchone()
        samples = [r[0] for r in c.execute("SELECT at FROM health_samples ORDER BY at")]
    result = []
    for j in jobs:
        context = (
            json.loads(j["data"]) | db.open(j["plan"]) | {"idempotency_key": j["id"], "owner": j["owner"]}
        )
        port = MaoziPublisher(context)
        try:
            product = await port.product()
            if not product or not product.get("sku"):
                result.append({"job_id": j["id"], "pending": True})
                continue
            stock = await port.invoke("check_stock")
            result.append(
                {
                    "job_id": j["id"],
                    "source_sku": j["source_key"],
                    "product_id": str(product["id"]),
                    "ozon_sku": str(product["sku"]),
                    "selling": stock["selling"],
                    "stock": stock["stock"],
                    "warehouse_id": stock["warehouse_id"],
                    "verified": stock["selling"]
                    and stock["stock"] == context["rules"]["stock"]
                    and stock["warehouse_id"] == str(context["store"]["config"]["warehouse_id"]),
                }
            )
        except Exception as e:
            result.append({"job_id": j["id"], "error": type(e).__name__, "verified": False})
    verified = sum(r.get("verified") is True for r in result)
    state.update(
        last_checked=time.time(),
        official_results=result,
        verified=verified,
        worker_alive=bool(heartbeat and time.time() - heartbeat[0] < 20),
        sample_count=len(samples),
    )
    if verified >= state["max_publications"] and len(result) == verified:
        state.setdefault("first_batch_verified_at", time.time())
        state["status"] = "first_batch_verified_24h_observation_pending"
        with db.connect() as c:
            c.execute(
                "UPDATE workflows SET enabled=0,notice=? WHERE owner=?",
                ("首批真实验收已完成；暂停新增，进行持续运行观察", state["owner"]),
            )
    if samples:
        span = samples[-1] - samples[0]
        gap = max([b - a for a, b in zip(samples, samples[1:])] + [0])
        state.update(observed_span_seconds=span, max_heartbeat_gap_seconds=gap)
        if (
            span >= 86400
            and gap <= 120
            and state.get("first_batch_verified_at")
            and verified == state["max_publications"]
        ):
            state["status"] = "24h_verified"
            state["completed_at"] = time.time()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temp.replace(path)


async def run():
    db = Database()
    lock = (db.directory / "acceptance.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    while True:
        try:
            await check(db)
        except Exception:
            pass
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(run())
