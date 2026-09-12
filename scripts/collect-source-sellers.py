"""Bounded ERP ranking/exact-seller acceptance; never claims whole-shop coverage."""

import argparse
import asyncio
import fcntl
import json
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.source_acquisition import (
    AcquisitionError,
    SourceAcquirer,
    archived_state,
    erp_request,
    page_data,
)
from flowhub.source_delists import read_delists
from flowhub.source_history import import_history
from flowhub.source_library import SourceFilters, SourceLibrary, assess

ROOT = Path(__file__).resolve().parents[2]
OWNER = "seller-acceptance"
FILTERS = SourceFilters(
    price_min=100, weight_max_g=2000, sales_min=1, require_follow_allowed=True, same_seller_only=True
)


def save(path, body):
    temporary = path.with_suffix(path.suffix + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2))
    temporary.replace(path)


def manifest(evidence):
    seeds = {}
    for stage in ("trial", "supplement", "supplement-pages2", "supplement-pages3", "supplement-pages4"):
        path = evidence / stage / "seeds.json"
        if not path.exists():
            continue
        for row in json.loads(path.read_text()):
            seeds[(row["shop"], row["offer"], row["source_sku"])] = row | {"evidence_file": str(path)}
    sellers = {}
    for seed in seeds.values():
        if seed.get("seller_id"):
            sellers.setdefault(str(seed["seller_id"]), []).append(seed)
    return sellers


def snapshot(library, out):
    with library.db.connect() as c:
        tasks = [
            dict(r) | {"body": json.loads(r["body"])}
            for r in c.execute(
                "SELECT id,kind,body,page,state,error,successes,failures,due FROM sourcing_tasks WHERE owner=? AND kind='seller_scan' ORDER BY id",
                (OWNER,),
            )
        ]
        attempts = [
            dict(r) | {"body": json.loads(r["body"])}
            for r in c.execute("SELECT * FROM sourcing_attempts WHERE owner=? ORDER BY id", (OWNER,))
        ]
        known = {r[0] for r in c.execute("SELECT sku FROM sourcing_seeds WHERE owner=?", (OWNER,))}
        blocked = {r[0] for r in c.execute("SELECT source_key FROM blocks WHERE owner=?", (OWNER,))}
        products = [
            json.loads(r[0])
            for r in c.execute("SELECT body FROM sourcing_products WHERE owner=? ORDER BY id", (OWNER,))
        ]
    candidates = {}
    for p in products:
        if p["sku"] not in known | blocked and assess(p, FILTERS)["state"] == "qualified":
            candidates.setdefault(p["sku"], p)
    decisions = [
        {
            "sku": p["sku"],
            "seller_id": p["seller_id"],
            "observed_at": p["collected_at"],
            "historical": p["sku"] in known,
            "explicit_delist": p["sku"] in blocked,
            "assessment": assess(p, FILTERS),
        }
        for p in products
    ]
    baseline_path = out / "previous-candidate-skus.json"
    previous = set(json.loads(baseline_path.read_text())) if baseline_path.exists() else set()
    novel = {sku: p for sku, p in candidates.items() if sku not in previous}
    save(out / "new-candidates.json", list(novel.values()))
    summary = {
        "observed_at": time.time(),
        "source_sellers": len({t["body"]["seller_id"] for t in tasks}),
        "attempts": len(attempts),
        "failed_attempts": sum(a["state"] != "committed" for a in attempts),
        "observed_unique_skus": len({p["sku"] for p in products}),
        "candidate_count": len(candidates),
        "previous_candidate_overlap": len(set(candidates) & previous),
        "new_vs_previous_candidates": len(novel),
        "candidate_duplicate_skus": len(candidates) - len({p["sku"] for p in candidates.values()}),
        "coverage": "ranking-exact-seller",
        "whole_shop_verified": False,
        "tasks": tasks,
    }
    save(out / "status.json", summary)
    save(out / "attempts.json", attempts)
    save(out / "candidates.json", list(candidates.values()))
    save(out / "decisions.json", decisions)
    print(json.dumps({k: v for k, v in summary.items() if k != "tasks"}), flush=True)
    return summary


async def main(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    library = SourceLibrary(Database(out / "data"))
    sellers = manifest(args.evidence.resolve())
    selected = args.sellers.split(",")
    if any(s not in sellers for s in selected):
        raise ValueError("seller not in evidence manifest")

    def task_ids():
        with library.db.connect() as c:
            return [
                r["id"]
                for r in c.execute(
                    "SELECT id,body FROM sourcing_tasks WHERE owner=? AND kind='seller_scan'", (OWNER,)
                )
                if json.loads(r["body"])["seller_id"] in selected
            ]

    if args.action in ("pause", "resume", "retry"):
        for task in task_ids():
            library.control_task(OWNER, task, args.action)
        snapshot(library, out)
        return
    if args.action == "status":
        snapshot(library, out)
        return
    token = subprocess.check_output(
        [
            "node",
            "--input-type=module",
            "-e",
            "import {resolveConfiguredMaoziToken} from './ozon-runtime/lib/maozi-credentials.mjs';process.stdout.write(await resolveConfiguredMaoziToken());",
        ],
        cwd=ROOT,
        text=True,
    )

    async def request(path, query, token):
        began = time.time()
        record = {"path": path, "query": query, "started_at": began}
        try:
            data = await erp_request(path, query, token)
            # Online responses may contain unrelated account fields: retain identity evidence only.
            record["data"] = (
                data
                if path != "/api.product.online/lists"
                else dict(data)
                | {
                    "data": [
                        {
                            k: r.get(k)
                            for k in (
                                "sku",
                                "shop_id",
                                "offer_id",
                                "online_status",
                                "archived",
                                "is_archived",
                            )
                        }
                        for r in data.get("data", [])
                    ]
                }
            )
            return data
        except AcquisitionError as error:
            record.update(error=str(error), diagnostic=error.diagnostic)
            raise
        finally:
            record["finished_at"] = time.time()
            with (out / "requests.jsonl").open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    if args.action == "probe-native":
        for seller in selected:
            try:
                await request("native_shop", {"seller_id": seller, "page": 1}, token)
            except AcquisitionError as error:
                print(
                    json.dumps({"seller": seller, "reason": str(error), "diagnostic": error.diagnostic}),
                    flush=True,
                )
        return
    # A new run refreshes the explicit exclusion list. Failure here must stop collection.
    excluded = await read_delists()
    save(out / "delists.json", excluded | {"observed_at": time.time()})
    with library.db.connect() as c:
        for sku in excluded["skus"]:
            c.execute("INSERT OR IGNORE INTO blocks VALUES(?,?,?)", (OWNER, sku, "飞书明确下架清单"))
    if args.action == "prepare":
        save(
            out / "manifest.json",
            {"observed_at": time.time(), "evidence": str(args.evidence.resolve()), "sellers": sellers},
        )
        baseline = args.evidence / "同店候选.json"
        if baseline.exists():
            save(out / "previous-candidate-skus.json", sorted(json.loads(baseline.read_text())["candidates"]))
        history = import_history(library, OWNER, ROOT)
        save(out / "history-import.json", history)
        with library.db.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO sourcing_settings VALUES(?,?,?,?)",
                (OWNER, 1, json.dumps(vars(FILTERS)), library.db.seal({})),
            )
            c.execute("UPDATE sourcing_tasks SET state='paused' WHERE owner=?", (OWNER,))
        for seller, roots in sellers.items():
            for category in sorted({s["category_id"] for s in roots if s.get("category_id")}):
                for mode in ("hot", "china"):
                    task = library.enqueue(
                        OWNER,
                        "seller_scan",
                        {"seller_id": seller, "category2": category, "mainType": mode},
                        35,
                    )
                    library.control_task(OWNER, task, "pause")
        # Check source binding, current own SKU, archive state and source seller afresh.
        for seller in selected:
            seed = sellers[seller][0]
            try:
                data = await request(
                    "/api.product.online/lists",
                    {
                        "shop_id": seed["shop"],
                        "offer_id": seed["offer"],
                        "archived_type": "all",
                        "page": 1,
                        "page_size": 100,
                    },
                    token,
                )
                rows, _ = page_data(data, 1, 100)
                row = next(
                    (
                        r
                        for r in rows
                        if str(r.get("sku")) == seed["own_sku"]
                        and str(r.get("shop_id")) == seed["shop"]
                        and r.get("offer_id") == seed["offer"]
                    ),
                    None,
                )
                if not row or archived_state(row) is not False:
                    raise AcquisitionError("seed_not_current")
                if (
                    seed["source_sku"] in excluded["skus"]
                    or seed["own_sku"] in excluded["skus"]
                    or [seed["shop"], seed["offer"]] in excluded["offers"]
                    or ["*", seed["offer"]] in excluded["offers"]
                ):
                    raise AcquisitionError("explicit_delist")
                data = await request(
                    "/api.selection.top/lists",
                    {"sku": seed["source_sku"], "mainType": seed["source_mode"], "page": 1, "page_size": 20},
                    token,
                )
                rows, _ = page_data(data, 1, 20)
                if not rows or any(
                    str(r.get("sku")) != seed["source_sku"] or str(r.get("seller_id")) != seller for r in rows
                ):
                    raise AcquisitionError("seed_identity_mismatch")
                with library.db.connect() as c:
                    binding = c.execute(
                        "SELECT sku FROM sourcing_seeds WHERE owner=? AND shop=? AND offer=?",
                        (OWNER, seed["shop"], seed["offer"]),
                    ).fetchone()
                    if not binding or binding["sku"] != seed["source_sku"]:
                        raise AcquisitionError("history_binding_mismatch")
                    c.execute(
                        "UPDATE sourcing_seeds SET body=?,archived=0,checked=? WHERE owner=? AND shop=? AND offer=?",
                        (
                            json.dumps(
                                seed
                                | {"online_evidence": {"sku": seed["own_sku"]}, "refreshed_at": time.time()}
                            ),
                            time.time(),
                            OWNER,
                            seed["shop"],
                            seed["offer"],
                        ),
                    )
            except AcquisitionError as error:
                with (out / "seed-failures.jsonl").open("a") as f:
                    f.write(
                        json.dumps({"seller": seller, "seed": seed, "reason": str(error), "at": time.time()})
                        + "\n"
                    )
        snapshot(library, out)
        return
    with library.db.connect() as c:
        ready = [
            json.loads(r[0])["seller_id"]
            for r in c.execute(
                "SELECT body FROM sourcing_tasks WHERE owner=? AND kind='seller_scan' AND state='ready'",
                (OWNER,),
            )
        ]
    if set(ready) - set(selected):
        raise ValueError("other sellers are ready in this output directory; pause them before this batch")
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, stop)
    # Do not implicitly resume paused/blocked tasks; resume is explicit and preserves cursors.
    acquirer = SourceAcquirer(library, request=request)
    acquirer.delist_snapshot, acquirer.delist_checked = excluded, time.time()
    started = time.monotonic()
    try:
        for _ in range(args.pages):
            if stopped or time.monotonic() - started >= args.seconds:
                break
            result = await acquirer.cycle(OWNER, token, kinds=("seller_scan",))
            print(json.dumps(result), flush=True)
            if result.get("state") in ("idle", "backpressure"):
                break
    finally:
        # Pause every selected stream on budget exhaustion, termination or exception.
        for task in task_ids():
            with library.db.connect() as c:
                state = c.execute("SELECT state FROM sourcing_tasks WHERE id=?", (task,)).fetchone()[0]
            if state == "ready":
                library.control_task(OWNER, task, "pause")
        snapshot(library, out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "probe-native", "run", "pause", "resume", "retry", "status"]
    )
    parser.add_argument(
        "--evidence", type=Path, default=ROOT / "FlowHub/reports/seller-expansion-hour-20260911"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sellers", default="1168944,2549888,4740958")
    parser.add_argument("--pages", type=int, default=6)
    parser.add_argument("--seconds", type=int, default=120)
    options = parser.parse_args()
    if not 1 <= options.pages <= 100 or not 1 <= options.seconds <= 3600:
        parser.error("pages must be 1..100; seconds must be 1..3600")
    options.output.mkdir(parents=True, exist_ok=True)
    with (options.output / "runner.lock").open("a") as lock:
        if options.action in ("prepare", "run"):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("another collector is using this output directory")
        asyncio.run(main(options))
