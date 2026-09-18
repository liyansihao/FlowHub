"""Read-only local FlowHub audit; never imports production modules or calls remote APIs."""
import argparse
import collections
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))


def stamp(value):
    return dt.datetime.fromtimestamp(value, TZ).isoformat(timespec="seconds")


def connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.execute("PRAGMA query_only=ON")
    return db


def audit(data):
    now = time.time()
    out = {"captured_at": stamp(now), "data_path": str(data.resolve()),
           "scope": "Local durable receipts; not a fresh remote sellability audit",
           "alerts": [], "thresholds": "Initial operating thresholds, not validated SLA"}
    db = connect(data / "flowhub.sqlite3")
    try:
        # One short read transaction keeps the core counts on the same snapshot.
        db.execute("BEGIN")
        out["health"] = [dict(name=n, pid=p, heartbeat_age_s=round(now-h, 1))
                         for n, p, h in db.execute("SELECT name,pid,heartbeat FROM health")]
        out["paused"] = dict(db.execute("SELECT module,paused FROM pipeline_module_control"))
        out["queue"] = dict(db.execute("SELECT state,count(*) FROM plugin_pipeline GROUP BY state"))
        counts = {str(m): 0 for m in (15, 30, 60, 180, 1440)}
        daily, hourly = collections.Counter(), collections.Counter()
        last = None
        for raw, in db.execute("SELECT body FROM plugin_publications"):
            body = json.loads(raw)
            times = [e["at"] for e in body.get("events", []) if e.get("to") == "stock_verified"]
            if not times:
                continue
            at = min(times)  # One first completion per publication record.
            if at > now:
                continue
            last = max(last or at, at)
            for minutes in counts:
                counts[minutes] += at >= now - int(minutes)*60
            if at >= now - 7*86400:
                daily[stamp(at)[:10]] += 1
            if at >= now - 86400:
                hourly[stamp(at)[:13]] += 1
        out["first_stock_verified"] = {"rolling_minutes": counts, "daily_last_7d_partial_edges": dict(sorted(daily.items())),
                                       "hours_last_24h_partial_edges": dict(sorted(hourly.items())),
                                       "last_at": stamp(last) if last else None}
        reasons = collections.Counter()
        request_old = wait_old = retry_exhausted = 0
        for raw, in db.execute("SELECT body FROM plugin_pipeline WHERE state='needs_fields'"):
            body = json.loads(raw)
            reasons[str(body.get("repair_reason") or body.get("reason") or "unknown")] += 1
            request_old += bool(body.get("requested_at") and now-body["requested_at"] >= 7200)
            wait_old += bool(body.get("repair_wait_started_at") and now-body["repair_wait_started_at"] >= 7200)
            retry = body.get("repair_retry") or {}
            retry_exhausted += max(retry.get("total_attempts", 0), retry.get("attempts", 0)) >= 3
        out["repair"] = {"stored_reason_counts": dict(reasons.most_common()),
                         "original_request_over_2h": request_old, "current_repair_wait_over_2h": wait_old,
                         "current_retry_count_at_least_3": retry_exhausted,
                         "note": "Stored reasons may be historical; original request age is not current stage age"}
        events = collections.Counter()
        cleanup = collections.Counter()
        capacity = None
        for module, outcome, raw, started in db.execute(
                "SELECT module,outcome,details,started FROM pipeline_module_events WHERE started>? ORDER BY started", (now-21600,)):
            detail = json.loads(raw)
            events[(module, outcome)] += 1
            if outcome in ("draft_cleanup", "favorite_cleanup"):
                cleanup[(outcome, detail.get("state"), detail.get("error_type"))] += 1
            if outcome == "draft_cleanup" and "used_before" in detail:
                capacity = {"observed_at": stamp(started), "age_s": round(now-started),
                            **{k: detail.get(k) for k in ("used_before", "limit", "deleted", "skipped", "state")}}
        out["events_6h_attempt_counts"] = [{"module": m, "outcome": o, "count": n} for (m, o), n in events.most_common()]
        out["cleanup_6h_attempt_counts"] = [{"kind": k, "state": s, "error_type": e, "count": n} for (k, s, e), n in cleanup.most_common()]
        # Admission and explicit maintenance write the actual post-cleanup count.
        # A pre-cleanup event must not keep reporting a full box after it is freed.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_capacity'").fetchone():
            latest=db.execute('SELECT used,capacity,observed,blocked FROM collection_capacity ORDER BY observed DESC LIMIT 1').fetchone()
            if latest and (not capacity or latest[2] >= now-capacity['age_s']):
                used,limit,observed,blocked=latest
                capacity={'observed_at':stamp(observed),'age_s':round(now-observed),
                          'used':used,'used_before':used,'limit':limit,'blocked':bool(blocked),
                          'source':'verified_capacity_observation'}
        out["last_collection_capacity_observation"] = capacity
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='repair_workflows'").fetchone():
            stages=collections.Counter();waiting=0;oldest=0
            for raw,state in db.execute('SELECT w.body,q.state FROM repair_workflows w JOIN plugin_pipeline q USING(owner,sku,seller)'):
                w=json.loads(raw)
                if state!='needs_fields':continue
                stages[(w['kind'],w['stage'],w['state'])]+=1
                age=max(0,now-w['last_progress_at']);oldest=max(oldest,age)
                waiting+=age>=1800
            out['repair_workflow']={'active_stages':[{'kind':k[0],'stage':k[1],'state':k[2],'count':v} for k,v in stages.items()],
                                    'no_progress_30m':waiting,'oldest_progress_age_s':round(oldest)}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_attempts'").fetchone():
            operation_counts=collections.Counter();durations=collections.defaultdict(lambda:collections.Counter())
            for operation,state,error,timing in db.execute('SELECT operation,state,error_class,timing FROM acquisition_attempts WHERE started>?',(now-3600,)):
                operation_counts[(operation,state,error)]+=1
                for name,value in json.loads(timing).items():
                    if name.endswith('_ms') and isinstance(value,(int,float)):durations[operation][name]+=value
            out['acquisition']={'attempts_1h':[{'operation':k[0],'state':k[1],'error_class':k[2],'count':v} for k,v in operation_counts.items()],
                                'duration_totals_ms':dict(durations),'note':'Operation attempts are not listing completions.'}
        db.rollback()
    finally:
        db.close()
    cluster = connect(data / "cluster/cluster.sqlite3")
    try:
        now2 = time.time()
        out["devices"] = [dict(name=n, heartbeat_age_s=round(now2-s, 1)) for n, s in cluster.execute(
            "SELECT name,last_seen FROM devices WHERE enabled=1")]
        out["version_reports"] = [dict(component=c, count=n, age_s=round(now2-s, 1)) for c, n, s in cluster.execute(
            "SELECT component,count(*),max(seen) FROM runtime_reports GROUP BY component")]
        out["compute_1h"] = [dict(kind=k, state=s, count=n) for k, s, n in cluster.execute(
            "SELECT kind,state,count(*) FROM compute_jobs WHERE created>? GROUP BY kind,state", (now2-3600,))]
        out["erp_commands_1h"] = dict(cluster.execute(
            "SELECT state,count(*) FROM erp_commands WHERE created>? GROUP BY state", (now2-3600,)))
    finally:
        cluster.close()
    runtime = data / "runtime-worker.json"
    if runtime.exists():
        ident = json.loads(runtime.read_text())["identity"]
        out["worker_identity"] = {k: ident.get(k) for k in ("pid", "started_at", "source_revision", "source_dirty", "source_sha256")}
    alert = out["alerts"]
    worker = next((r for r in out["health"] if r["name"] == "worker"), None)
    if not worker or worker["heartbeat_age_s"] > 180:
        alert.append({"level": "critical", "code": "worker_heartbeat_missing"})
    if counts["60"] < 10 and out["queue"].get("needs_fields", 0) >= 8:
        alert.append({"level": "critical", "code": "low_output_with_repair_backlog", "count_1h": counts["60"], "count_3h": counts["180"]})
    if capacity and capacity["limit"] and capacity["used_before"] / capacity["limit"] >= 0.95:
        alert.append({"level": "critical" if capacity["age_s"] < 900 else "warning",
                      "code": "collection_capacity_high_last_observation", "observation_age_s": capacity["age_s"]})
    if not out["version_reports"] and out["devices"]:
        alert.append({"level": "warning", "code": "agent_versions_unreported"})
    for device in out["devices"]:
        if device["heartbeat_age_s"] > 90:
            alert.append({"level": "warning", "code": "device_heartbeat_stale", "name": device["name"]})
    out["completed_at"] = stamp(time.time())
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output.resolve()), "captured_at": report["captured_at"],
                      "verified": report["first_stock_verified"]["rolling_minutes"],
                      "alerts": report["alerts"]}, ensure_ascii=False))
