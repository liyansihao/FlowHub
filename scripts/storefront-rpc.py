"""Local JSON stdin bridge for the independent browser collector; no network."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.storefront import StorefrontCollector, parse_packet


def main():
    args = json.load(sys.stdin)
    action = args["action"]
    if action == "validate":
        parsed = parse_packet(args["html"], args["seller"], args["url"])
        return {k: v for k, v in parsed.items() if k != "products"} | {"rows": len(parsed["products"])}
    lib = SourceLibrary(Database(Path(args["data"])))
    collector = StorefrontCollector(lib)
    owner, seller = args["owner"], args["seller"]
    if action == "prepare":
        collector.prepare(owner, {seller: args["roots"]})
    elif action in ("pause", "resume", "retry"):
        return collector.control(owner, seller, action)
    elif action == "ingest":
        return collector.ingest(owner, seller, args["html"], args["url"], args["artifact"])
    elif action == "failure":
        with lib.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            task = c.execute(
                "SELECT * FROM storefront_tasks WHERE owner=? AND seller=?", (owner, seller)
            ).fetchone()
            if task["state"] == "ready" and task["next_url"] == args["url"]:
                evidence = dict(
                    requested_url=args["url"],
                    reason=args["reason"],
                    channel="independent-playwright",
                    observed_at=time.time(),
                    root_seeds=json.loads(task["roots"]),
                    artifact=args.get("artifact"),
                )
                c.execute(
                    "INSERT INTO storefront_attempts(owner,seller,page,at,state,digest,body) VALUES(?,?,?,?,?,?,?)",
                    (owner, seller, task["page"], time.time(), "failed", "transport", json.dumps(evidence)),
                )
                c.execute(
                    "UPDATE storefront_tasks SET state='blocked',error=?,updated=? WHERE owner=? AND seller=?",
                    (args["reason"], time.time(), owner, seller),
                )
    elif action == "recover":
        with lib.db.connect() as c:
            row = c.execute(
                "SELECT body FROM storefront_attempts WHERE owner=? AND seller=? AND state='committed' AND json_extract(body,'$.requested_url')=? ORDER BY id DESC LIMIT 1",
                (owner, seller, args["url"]),
            ).fetchone()
        if not row:
            return None
        body = json.loads(row[0])
        return dict(state="committed", added=body["added"], rows=len(body["skus"]), page=body["page"])
    elif action == "export":
        with lib.db.connect() as c:
            products = [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT body FROM sourcing_products WHERE owner=? AND seller=? ORDER BY id",
                    (owner, seller),
                )
            ]
            attempts = [
                dict(r) | {"body": json.loads(r["body"])}
                for r in c.execute(
                    "SELECT * FROM storefront_attempts WHERE owner=? AND seller=? ORDER BY id",
                    (owner, seller),
                )
            ]
        return dict(products=products, attempts=attempts, tasks=collector.tasks(owner))
    return next(t for t in collector.tasks(owner) if t["seller"] == seller)


if __name__ == "__main__":
    try:
        print(json.dumps(dict(ok=True, data=main()), ensure_ascii=False))
    except Exception as e:
        print(json.dumps(dict(ok=False, error=str(e)), ensure_ascii=False))
        sys.exit(1)
