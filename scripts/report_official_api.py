"""Read-only migration/cooldown counters; never opens keys or initializes a database."""

import argparse
import json
import sqlite3
import time
from pathlib import Path


def report(directory):
    path = Path(directory).resolve() / "flowhub.sqlite3"
    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {
            "at": time.time(),
            "counter_scope": "cumulative_since_instrumentation",
            "endpoints": [],
            "cooldowns": [],
            "publications": [],
        }
        if "official_api_metrics" in tables:
            result["endpoints"] = [
                dict(r)
                for r in c.execute("""SELECT path,SUM(calls) AS requests,SUM(cache_hits) AS cache_hits,
                SUM(errors) AS errors,ROUND(SUM(network_ms)/MAX(1,SUM(calls)),1) AS mean_network_ms
                FROM official_api_metrics GROUP BY path""")
            ]
        if "official_api_accounts" in tables:
            result["cooldowns"] = [
                dict(r)
                for r in c.execute(
                    "SELECT substr(account,1,12) AS scope,reason,blocked_until FROM official_api_accounts WHERE blocked_until>?",
                    (time.time(),),
                )
            ]
        if "plugin_publications" in tables:
            result["publications"] = [
                dict(r)
                for r in c.execute(
                    "SELECT COALESCE(json_extract(body,'$.backend'),'maozi') AS backend,json_extract(body,'$.phase') AS phase,COUNT(*) AS count FROM plugin_publications GROUP BY backend,phase"
                )
            ]
            result["official_first_verified_last_15m"] = c.execute(
                "SELECT COUNT(*) FROM plugin_publications WHERE json_extract(body,'$.backend')='official' AND json_extract(body,'$.first_verified_at')>?",
                (time.time() - 900,),
            ).fetchone()[0]
        return result
    finally:
        c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    print(json.dumps(report(parser.parse_args().data), ensure_ascii=False, indent=2))
