from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from comparebot.calibration.analysis import analyze
from comparebot.calibration.collector import collect_cases, summarize_collection
from comparebot.calibration.dataset import build_manifest
from comparebot.calibration.qwen import review_medium_cases
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

    summary = commands.add_parser("summary", help="summarize existing collection results")
    summary.add_argument("--manifest", type=Path, required=True)
    summary.add_argument("--cases-dir", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)

    panel = commands.add_parser("serve", help="start the blind-label panel")
    panel.add_argument("--cases-dir", type=Path, required=True)
    panel.add_argument("--labels", type=Path, required=True)
    panel.add_argument("--host", default="127.0.0.1")
    panel.add_argument("--port", type=int, default=8770)

    report = commands.add_parser("analyze", help="calibrate thresholds from human labels")
    report.add_argument("--cases-dir", type=Path, required=True)
    report.add_argument("--labels", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--qwen-predictions", type=Path)

    qwen = commands.add_parser("qwen", help="review the calibrated medium band")
    qwen.add_argument("--cases-dir", type=Path, required=True)
    qwen.add_argument("--labels", type=Path, required=True)
    qwen.add_argument("--analysis", type=Path, required=True)
    qwen.add_argument("--output", type=Path, required=True)
    qwen.add_argument("--concurrency", type=int, default=2)
    qwen.add_argument("--model", default=os.getenv("QWEN_VL_MODEL", "qwen3-vl-plus"))
    qwen.add_argument(
        "--base-url",
        default=os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
    )
    return parser


def main() -> None:
    load_dotenv()
    args = _parser().parse_args()
    if args.command == "build":
        payload = build_manifest(
            args.source_root,
            per_category=args.per_category,
            seed=args.seed,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
    if args.command == "serve":
        serve(args.cases_dir, args.labels, host=args.host, port=args.port)
        return
    if args.command == "summary":
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        payload = summarize_collection(manifest["products"], args.cases_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return
    if args.command == "qwen":
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            raise SystemExit("DASHSCOPE_API_KEY is not configured")
        summary = asyncio.run(
            review_medium_cases(
                args.cases_dir,
                args.labels,
                args.analysis,
                args.output,
                api_key=api_key,
                model=args.model,
                base_url=args.base_url,
                concurrency=args.concurrency,
            )
        )
        print(json.dumps(summary, ensure_ascii=False))
        return
    payload = analyze(args.cases_dir, args.labels, args.qwen_predictions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["thresholds"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
