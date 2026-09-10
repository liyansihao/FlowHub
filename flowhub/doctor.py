"""Offline dependency inspection. Never connects to shops or prints credentials."""

import importlib
import json
import os
import subprocess
from pathlib import Path


def main():
    results = {}
    for name in ["fastapi", "cryptography", "requests", "PIL", "search1688api"]:
        try:
            importlib.import_module(name)
            results[name] = "ok"
        except ImportError:
            results[name] = "missing"
    root = Path(os.environ.get("FLOWHUB_LEGACY_ROOT", "/data/legacy"))
    checks = {
        "node": ["node", "--version"],
        "ocr": ["tesseract", "--list-langs"],
        "legacy_import": [
            "node",
            "--input-type=module",
            "-e",
            "await import(process.argv[1]); console.log('ok')",
            str(root / "maozi_direct_new_method/maozi_new_method_direct.mjs"),
        ],
    }
    for name, cmd in checks.items():
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ | {"FLOW_EF_CONFIG_FILE": str(root / "flow_b_ef/state/config.json")},
            )
            valid = r.returncode == 0
            if name == "ocr":
                valid = valid and all(x in r.stdout.splitlines() for x in ["eng", "rus", "chi_sim"])
            results[name] = "ok" if valid else "failed"
        except (OSError, subprocess.TimeoutExpired):
            results[name] = "failed"
    results["delist_credentials"] = (
        "configured"
        if Path(os.environ.get("FLOWHUB_FEISHU_CONFIG", "/data/private/feishu.json")).is_file()
        else "not configured: live writes will be blocked"
    )
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(v == "ok" for k, v in results.items() if k != "delist_credentials") else 1


if __name__ == "__main__":
    raise SystemExit(main())
