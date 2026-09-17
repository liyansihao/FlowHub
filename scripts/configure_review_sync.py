#!/usr/bin/env python3
"""Write the private Netlify review sync configuration used by FlowHub supervisor."""
import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    target = DATA / "review-sync.env"
    target.write_text(
        "\n".join([
            f"FLOWHUB_REVIEW_SYNC_URL={args.url}",
            f"FLOWHUB_REVIEW_SYNC_TOKEN={args.token}",
            f"FLOWHUB_REVIEW_OWNER={args.owner}",
            "FLOWHUB_REVIEW_SYNC_INTERVAL=15",
            "",
        ]),
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    print(target)


if __name__ == "__main__":
    main()
