from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from comparebot.calibration.collector import collect_cases
from comparebot.calibration.dataset import build_manifest
from comparebot.calibration.server import serve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and label a compareBot calibration set")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="freeze a balanced Ozon manifest")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--per-category", type=int, default=20)
    build.add_argument("--seed", type=int, default=20260910)

    collect = commands.add_parser("collect", help="run live 1688 search and DINOv2")
    collect.add_argument("--manifest", type=Path, required=True)
    collect.add_argument("--cases-dir", type=Path, required=True)
    collect.add_argument("--top-k", type=int, default=5)
    collect.add_argument("--device", choices=("cpu", "mps", "cuda"))
    collect.add_argument("--limit", type=int)

    panel = commands.add_parser("serve", help="start the blind-label panel")
    panel.add_argument("--cases-dir", type=Path, required=True)
    panel.add_argument("--labels", type=Path, required=True)
    panel.add_argument("--host", default="127.0.0.1")
    panel.add_argument("--port", type=int, default=8770)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "build":
        payload = build_manifest(
            args.source_root,
            per_category=args.per_category,
            seed=args.seed,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"wrote {payload['product_count']} products across "
            f"{payload['category_count']} categories to {args.output}"
        )
        return
    if args.command == "collect":
        summary = asyncio.run(
            collect_cases(
                args.manifest,
                args.cases_dir,
                top_k=args.top_k,
                device=args.device,
                limit=args.limit,
            )
        )
        print(json.dumps(summary, ensure_ascii=False))
        return
    serve(args.cases_dir, args.labels, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
