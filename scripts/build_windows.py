"""Build a credential-free Windows/Docker compatibility distribution from this workspace."""

import hashlib, json, re, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = ROOT.parent
OUT = WS / "deliverables/FlowHub-Windows"
if OUT.exists():
    raise SystemExit("Output exists; choose a new release directory or move the previous release first.")
OUT.mkdir(parents=True)
for f in (ROOT / "packaging/windows").iterdir():
    if f.is_file():
        shutil.copy2(f, OUT / f.name)
for p in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0"):
    if not p or not p.startswith(
        ("flowhub/", "flowhub_plugins/", "web/", "plugins/", "requirements.lock.txt", "pyproject.toml")
    ):
        continue
    d = OUT / "app" / p
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / p, d)
legacy = OUT / "legacy"
folders = [
    "flow_b_ef",
    "flow_ef_category_fbs",
    "maozi_direct_new_method",
    "maozi_playwright_china",
    "ozon-runtime",
    "flow_b_codex_transfer_20260608",
]
for folder in folders:
    for f in (WS / folder).rglob("*"):
        rel = f.relative_to(WS)
        if not f.is_file() or any(
            x in rel.parts
            for x in [
                "state",
                "node_modules",
                ".venv",
                "runs",
                "tests",
                "tests-js",
                "__pycache__",
                "review",
                "cache",
                "bin",
                "backtests",
                "backtest",
                "evidence",
                "listing_review",
                "low-ticket",
                "publish-passed",
                "artifacts",
            ]
        ):
            continue
        if f.suffix not in [".mjs", ".py", ".json"]:
            continue
        if f.suffix == ".json" and str(rel) not in [
            "maozi_direct_new_method/prohibited-leaf-categories.json",
            "maozi_direct_new_method/prohibited-categories.json",
            "maozi_direct_new_method/yellow-priority-categories.json",
            "maozi_direct_new_method/supplier-research-priority.json",
            "flow_b_ef/lib/image-policy.json",
            "flow_b_codex_transfer_20260608/config/flow_b_category_policy.json",
        ]:
            continue
        d = legacy / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, d)
# Resolve additional relative source imports; do not copy neighboring state folders.
while True:
    missing = []
    for f in legacy.rglob("*.mjs"):
        for spec in re.findall(r"(?:from\s*|import\s*\()\s*[\"']([^\"']+)[\"']", f.read_text()):
            if not spec.startswith("."):
                continue
            dest = (f.parent / spec).resolve()
            if dest.exists():
                continue
            rel = dest.relative_to(legacy.resolve())
            src = WS / rel
            if not src.is_file() or src.suffix not in [".mjs", ".py"]:
                raise SystemExit("Missing source dependency: " + str(rel))
            missing.append((src, dest))
    if not missing:
        break
    for src, dest in missing:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
# Include only the read-only publication guard Python package and bridge.
shutil.copytree(
    WS / "FlowEF-production/src",
    legacy / "FlowEF-production/src",
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
)
(legacy / "FlowEF-production/bridges").mkdir(parents=True)
shutil.copy2(WS / "FlowEF-production/bridges/flowb.mjs", legacy / "FlowEF-production/bridges/flowb.mjs")


def put(name, value):
    p = legacy / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2))


cfg = json.loads((WS / "flow_b_ef/state/config.json").read_text())
cfg["stores"] = []
for k in ["parallel_store_ids", "standby_store_ids", "sequential_store_order"]:
    cfg["runtime"][k] = []
cfg["runtime"]["ranking_refresh_leader_store_id"] = 0
cfg["flow_e"]["supplier_search_script"] = (
    "/data/legacy/maozi_direct_new_method/lib/portable-support/flow_b_1688_sync.py"
)
put("flow_b_ef/state/config.json", cfg)
for name in [
    "flow_ef_category_fbs/state/candidate-outcomes.json",
    "flow_b_ef/state/flow-ef/candidate-outcomes.json",
]:
    put(name, {"outcomes": {}, "completed": {}, "submissions": {}})
# Durable exclusion knowledge is included, never listing history or live credentials.
q = json.loads((WS / "flow_b_ef/state/quarantine.json").read_text())
put(
    "flow_b_ef/state/quarantine.json",
    {
        "products": [
            {k: r[k] for k in ["source_sku", "shop_id", "offer_id", "ozon_sku", "quarantined"] if k in r}
            for r in q["products"]
        ]
    },
)
put(
    "maozi_direct_new_method/state/feedback/match-feedback.json",
    json.loads((WS / "maozi_direct_new_method/state/feedback/match-feedback.json").read_text()),
)
for f in (WS / "maozi_direct_new_method/state/global-sku-claims").glob("*.json"):
    put(
        "maozi_direct_new_method/state/global-sku-claims/" + f.name,
        {"state": "migration_protected", "source_sku": f.stem},
    )
r = json.loads((WS / "maozi_direct_new_method/state/category_analytics/multistore-ranking.json").read_text())
put(
    "maozi_direct_new_method/state/category_analytics/multistore-ranking.json",
    {
        "ranking": {
            "products": list(
                {
                    str(p.get("category_id")): {
                        k: p[k]
                        for k in [
                            "sku",
                            "title",
                            "category_id",
                            "category_name",
                            "average_price_rub",
                            "cover_image",
                        ]
                        if k in p
                    }
                    for p in r.get("ranking", {}).get("products", [])
                }.values()
            )
        }
    },
)
put(
    "package.json",
    {"private": True, "type": "module", "dependencies": {"sharp": "^0.35.3", "playwright": "^1.62.1"}},
)
for f in legacy.rglob("*"):
    if f.is_file() and f.suffix in [".mjs", ".py", ".json"]:
        s = f.read_text()
        s = s.replace(str(WS), "/data/legacy")
        f.write_text(s)
# Scan every outgoing file for actual configured account secrets and accidental JWTs.
sys.path.insert(0, str(ROOT))
from flowhub.db import Database

D = Database()
secrets = []
with D.connect() as c:
    for col, table in [("secret", "stores"), ("secrets", "workflows")]:
        for row in c.execute(f"SELECT {col} FROM {table}"):
            secrets.extend(v.encode() for v in D.open(row[0]).values() if isinstance(v, str) and len(v) > 12)
for f in OUT.rglob("*"):
    if not f.is_file():
        continue
    b = f.read_bytes()
    if any(s in b for s in secrets) or re.search(rb"eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}", b):
        raise SystemExit("Credential scan failed: " + str(f.relative_to(OUT)))
manifest = {
    str(f.relative_to(OUT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in OUT.rglob("*") if f.is_file()
}
(OUT / "SHA256SUMS.json").write_text(json.dumps(manifest, indent=2))
zip_path = OUT.with_suffix(".zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for f in OUT.rglob("*"):
        if f.is_file():
            z.write(f, Path(OUT.name) / f.relative_to(OUT))
print(json.dumps({"archive": str(zip_path), "files": len(manifest), "bytes": zip_path.stat().st_size}))
