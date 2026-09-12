"""Safari page-source intake. Durable cursor output drives the next browser read."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.storefront import StorefrontCollector


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--owner", default="seller-acceptance")
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--evidence", type=Path, required=True)
    imp = sub.add_parser("import")
    imp.add_argument("seller")
    imp.add_argument("html", type=Path)
    imp.add_argument("--url", help="Exact requested checkpoint URL; defaults to durable checkpoint")
    for name in ("pause", "resume", "retry"):
        sub.add_parser(name).add_argument("seller")
    sub.add_parser("status")
    sub.add_parser("export")
    a = p.parse_args()
    collector = StorefrontCollector(SourceLibrary(Database(a.data)))
    if a.command == "prepare":
        spec = importlib.util.spec_from_file_location(
            "seller_cli", Path(__file__).with_name("collect-source-sellers.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = collector.prepare(a.owner, mod.manifest(a.evidence))
    elif a.command == "import":
        task = next(t for t in collector.tasks(a.owner) if t["seller"] == a.seller)
        result = collector.ingest(
            a.owner, a.seller, a.html.read_text(), a.url or task["next_url"], str(a.html.resolve())
        )
    elif a.command == "status":
        result = collector.tasks(a.owner)
    elif a.command == "export":
        with collector.db.connect() as c:
            known = {r[0] for r in c.execute("SELECT sku FROM sourcing_seeds WHERE owner=?", (a.owner,))}
            known |= {r[0] for r in c.execute("SELECT source_key FROM blocks WHERE owner=?", (a.owner,))}
            products = {}
            for r in c.execute("SELECT body FROM sourcing_products WHERE owner=? ORDER BY id", (a.owner,)):
                product = json.loads(r[0])
                if product["sku"] not in known and product.get("coverage") == "storefront-page":
                    products.setdefault(product["sku"], product)
            attempts = [
                dict(r) | {"body": json.loads(r["body"])}
                for r in c.execute("SELECT * FROM storefront_attempts WHERE owner=? ORDER BY id", (a.owner,))
            ]
        result = dict(candidates=list(products.values()), attempts=attempts, tasks=collector.tasks(a.owner))
    else:
        result = collector.control(a.owner, a.seller, a.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
