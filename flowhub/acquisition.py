"""Durable single-operation source acquisition; legacy journals remain authoritative.

One tick performs at most one ERP request. Writes enter their legacy uncertain
state before dispatch. A lost acknowledgement is recovered only by lookup.
"""

import asyncio
import copy
import hashlib
import json
import secrets
import time

from .modules import Pending


class AcquisitionPending(Pending):
    def __init__(self, reason, *, delay=1, category="remote_pending", progress=False, not_sent=False):
        super().__init__(reason)
        self.delay, self.category, self.progress = delay, category, progress
        self.diagnostic = {"not_sent": True, "retry_after_ms": delay * 1000} if not_sent else {}


def enabled(db, sku=None):
    path = db.directory / "acquisition-policy.json"
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cohort = cfg.get("source_keys")
    if cohort is not None and (
        not isinstance(cohort, list) or any(not isinstance(v, str) or not v.isdigit() for v in cohort)
    ):
        raise ValueError("invalid_acquisition_cohort")
    return bool(cfg.get("enabled", False) and (sku is None or cohort is None or str(sku) in cohort))


def routing(db, c):
    """SQL predicate for the new lane; enrolled tasks survive flag rollback."""
    active = enabled(db)  # validate the cohort before using it
    cfg = json.loads((db.directory / "acquisition-policy.json").read_text()) if (db.directory / "acquisition-policy.json").exists() else {}
    cohort = cfg.get("source_keys")
    sql, args = "0", []
    if active:
        if cohort is None:
            sql = "1"
        elif cohort:
            sql, args = "q.sku IN (" + ",".join("?" for _ in cohort) + ")", list(cohort)
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_bindings'").fetchone():
        sql += " OR EXISTS (SELECT 1 FROM acquisition_bindings a WHERE a.owner=q.owner AND a.sku=q.sku)"
    return "(" + sql + ")", args


def routing_active(db):
    if enabled(db):
        return True
    with db.connect() as c:
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_bindings'").fetchone()
                    and c.execute("SELECT 1 FROM acquisition_bindings LIMIT 1").fetchone())


def enrolled(db, key):
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_tasks'").fetchone():
            return False
        return bool(c.execute("SELECT 1 FROM acquisition_tasks WHERE key=?", (key,)).fetchone())


def saved_candidate(db, key):
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_tasks'").fetchone():
            return None
        row = c.execute("SELECT body FROM acquisition_tasks WHERE key=?", (key,)).fetchone()
        return db.open(row[0]).get("candidate") if row else None


def schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS acquisition_tasks(
      key TEXT PRIMARY KEY,account TEXT NOT NULL,body TEXT NOT NULL,
      version INTEGER NOT NULL,token TEXT,lease_until REAL NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS acquisition_attempts(
      id TEXT PRIMARY KEY,task_key TEXT NOT NULL,operation TEXT NOT NULL,
      method TEXT NOT NULL,started REAL NOT NULL,finished REAL,state TEXT NOT NULL,
      error_class TEXT,timing TEXT NOT NULL,result TEXT);
    CREATE INDEX IF NOT EXISTS acquisition_attempts_task ON acquisition_attempts(task_key,started);
    CREATE TABLE IF NOT EXISTS dossier_snapshots(
      hash TEXT PRIMARY KEY,task_key TEXT NOT NULL,body TEXT NOT NULL,observed REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS acquisition_bindings(
      owner TEXT,sku TEXT,task_key TEXT PRIMARY KEY);
    CREATE INDEX IF NOT EXISTS acquisition_binding_source ON acquisition_bindings(owner,sku);
    CREATE TABLE IF NOT EXISTS source_draft_index(
      account TEXT,sku TEXT,draft_id TEXT,observed REAL,PRIMARY KEY(account,sku,draft_id));
    CREATE TABLE IF NOT EXISTS acquisition_events(
      id INTEGER PRIMARY KEY,task_key TEXT,version INTEGER,stage TEXT,at REAL,body TEXT);
    CREATE TABLE IF NOT EXISTS acquisition_circuits(
      account TEXT,path TEXT,failures INTEGER NOT NULL,until REAL NOT NULL,
      PRIMARY KEY(account,path));
    """)


def classify(error, write=False):
    diagnostic = getattr(error, "diagnostic", {})
    code = str(error).lower()
    if write:
        return "write_unknown"
    if diagnostic.get("http_status") in (401, 403):
        return "auth_expired"
    if any(x in code for x in ("pacing", "rate_limit", "pool_busy")):
        return "rate_deferred"
    if isinstance(error, TimeoutError):
        return "operation_timeout"
    if any(
        x in code for x in ("timeout", "timedout", "connect", "fetch failed", "econn", "enotfound", "eai_again", "enetunreach", "ehostunreach", "readerror", "bridge_read_failure")
    ):
        return "network"
    return "contract_invalid"


def initial(collector, state, data):
    # Legacy claimed can represent a crash after a write: never infer no dispatch.
    if state == "ready" and collector.cached_snapshot():
        stage = "ready"
    elif data.get("draft_id"):
        stage = "read_draft"
    elif state == "draft_started":
        stage = "find_draft"
    else:
        stage = "find_favorite"
    return {
        "stage": stage,
        "data": data,
        "candidate": dict(collector.c["candidate"]),
        "unknown_favorite": state in ("favorite_started", "claimed") or bool(data.get("favorite_attempted")),
        "unknown_draft": state == "draft_started",
        "created": time.time(),
        "due": 0,
        "last_progress": time.time(),
        "failures": 0,
        "binding": hashlib.sha256(
            json.dumps(collector.c.get("candidate", {}), sort_keys=True).encode()
        ).hexdigest(),
    }


class Acquisition:
    def __init__(self, collector):
        self.s = collector
        self.db = collector.db
        from .collection_capacity import account

        self.account = account(collector.c)
        self.key = collector.key
        self.token = secrets.token_hex(16)
        with self.db.connect() as c:
            schema(c)

    def claim(self):
        now = time.time()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_module_control'").fetchone():
                if c.execute(
                    "SELECT 1 FROM pipeline_module_control WHERE module='seed' AND paused=1"
                ).fetchone():
                    raise AcquisitionPending("seed_paused", delay=30)
            for other in c.execute(
                "SELECT t.body,t.lease_until FROM acquisition_tasks t JOIN acquisition_bindings b ON b.task_key=t.key "
                "WHERE b.owner=? AND b.sku=? AND t.key!=?",
                (self.s.c["owner"], self.s.sku, self.key),
            ):
                previous = self.db.open(other["body"])
                if previous["stage"] != "ready" or other["lease_until"] > now:
                    raise AcquisitionPending(
                        "credential_binding_changed_with_unfinished_source",
                        delay=3600,
                        category="identity_mismatch",
                    )
            c.execute(
                "INSERT OR IGNORE INTO acquisition_bindings VALUES(?,?,?)",
                (self.s.c["owner"], self.s.sku, self.key),
            )
            old = c.execute("SELECT * FROM acquisition_tasks WHERE key=?", (self.key,)).fetchone()
            if old and old["lease_until"] > now:
                raise AcquisitionPending("acquisition_owned", delay=5)
            legacy = c.execute("SELECT * FROM source_details WHERE key=?", (self.key,)).fetchone()
            state, data = (legacy["state"], self.db.open(legacy["body"])) if legacy else ("new", {})
            # Do not take over a recently active legacy collector.
            if not old and legacy and state not in ("ready", "draft_ready") and now - legacy["updated"] < 120:
                raise AcquisitionPending("legacy_acquisition_draining", delay=120 - (now - legacy["updated"]))
            work = self.db.open(old["body"]) if old else initial(self.s, state, data)
            if work.get("manual"):
                raise AcquisitionPending(work["manual"], delay=3600, category="identity_mismatch")
            if work.get("due", 0) > now:
                raise AcquisitionPending(
                    work.get("reason", "operation_deferred"),
                    delay=work["due"] - now,
                    category=work.get("category", "remote_pending"),
                )
            if old and work.get("candidate"):
                self.s.c = self.s.c | {"candidate": work["candidate"]}
            version = old["version"] + 1 if old else 1
            c.execute(
                "INSERT OR REPLACE INTO acquisition_tasks VALUES(?,?,?,?,?,?,?)",
                (self.key, self.account, self.db.seal(work), version, self.token, now + 120, now),
            )
        self.version, self.work = version, work
        return state, data

    def owned(self, c):
        return c.execute(
            "SELECT 1 FROM acquisition_tasks WHERE key=? AND token=? AND version=? AND lease_until>?",
            (self.key, self.token, self.version, time.time()),
        ).fetchone()

    def save(self, *, legacy=None):
        now = time.time()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self.owned(c):
                raise AcquisitionPending("acquisition_ownership_changed", delay=5)
            if legacy:
                c.execute(
                    "INSERT OR REPLACE INTO source_details VALUES(?,?,?,?)",
                    (self.key, legacy, self.db.seal(self.work["data"]), now),
                )
            c.execute(
                "UPDATE acquisition_tasks SET body=?,updated=? WHERE key=? AND token=? AND version=?",
                (self.db.seal(self.work), now, self.key, self.token, self.version),
            )
            from .pipeline_modules.favorite_shadow import record_business_state
            record_business_state(c, 'acquisition:'+self.key, self.work['stage'],
                                  str(self.version)+':'+str(now),
                                  favorite_id=self.work.get('data',{}).get('favorite_id'))
            c.execute(
                "INSERT INTO acquisition_events VALUES(NULL,?,?,?,?,?)",
                (
                    self.key,
                    self.version,
                    self.work["stage"],
                    now,
                    json.dumps(
                        {
                            "due": self.work.get("due", 0),
                            "category": self.work.get("category"),
                            "unknown_favorite": self.work.get("unknown_favorite"),
                            "unknown_draft": self.work.get("unknown_draft"),
                        }
                    ),
                ),
            )

    def release(self):
        with self.db.connect() as c:
            c.execute(
                "UPDATE acquisition_tasks SET token=NULL,lease_until=0 WHERE key=? AND token=? AND version=?",
                (self.key, self.token, self.version),
            )

    async def renew(self):
        while True:
            await asyncio.sleep(20)
            with self.db.connect() as c:
                c.execute(
                    "UPDATE acquisition_tasks SET lease_until=? WHERE key=? AND token=? AND version=? AND lease_until>?",
                    (time.time() + 120, self.key, self.token, self.version, time.time()),
                )

    async def advance(self, stage, *, legacy=None):
        self.work.update(stage=stage, due=0, failures=0, last_progress=time.time())
        self.work.pop("scan", None)
        await asyncio.to_thread(self.save, legacy=legacy)

    async def request(self, path, method="GET", query=None, body=None):
        now = time.time()
        write = method != "GET"
        with self.db.connect() as c:
            circuit = c.execute(
                "SELECT until FROM acquisition_circuits WHERE account=? AND path=?", (self.account, path)
            ).fetchone()
        if circuit and circuit[0] > now:
            raise AcquisitionPending(
                "endpoint_cooling_down", delay=circuit[0] - now, category="network", not_sent=True
            )
        identity = secrets.token_hex(16)
        # Intent already saved by the state transition before a write reaches here.
        with self.db.connect() as c:
            if not self.owned(c):
                raise AcquisitionPending("acquisition_ownership_changed", delay=5)
            c.execute(
                "INSERT INTO acquisition_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (identity, self.key, path, method, now, None, "dispatching", None, "{}", None),
            )
        try:
            result = await self.s.call(path, method, query=query, body=body)
        except BaseException as error:
            category = (
                "rate_deferred"
                if getattr(error, "diagnostic", {}).get("not_sent")
                else classify(error, write)
            )
            with self.db.connect() as c:
                c.execute(
                    "UPDATE acquisition_attempts SET finished=?,state=?,error_class=?,timing=? WHERE id=?",
                    (
                        time.time(),
                        "not_sent" if category == "rate_deferred" else "unknown" if write else "failed",
                        category,
                        json.dumps(
                            {"total_ms": (time.time() - now) * 1000, **getattr(self.s, "last_timing", {})}
                        ),
                        identity,
                    ),
                )
                if category in ("network", "operation_timeout", "auth_expired"):
                    old = c.execute(
                        "SELECT failures FROM acquisition_circuits WHERE account=? AND path=?",
                        (self.account, path),
                    ).fetchone()
                    failures = (old[0] if old else 0) + 1
                    delay = (
                        3600
                        if category == "auth_expired"
                        else min(300, 30 * 2 ** min(max(0, failures - 3), 3))
                        if failures >= 3
                        else 0
                    )
                    c.execute(
                        "INSERT OR REPLACE INTO acquisition_circuits VALUES(?,?,?,?)",
                        (self.account, path, failures, time.time() + delay),
                    )
            raise
        with self.db.connect() as c:
            # Save acknowledgement even if the old worker lost ownership: recovery
            # may inspect it, but only the fenced owner may advance the task.
            c.execute(
                "UPDATE acquisition_attempts SET finished=?,state=?,timing=?,result=? WHERE id=?",
                (
                    time.time(),
                    "acknowledged",
                    json.dumps(
                        {"total_ms": (time.time() - now) * 1000, **getattr(self.s, "last_timing", {})}
                    ),
                    self.db.seal(result),
                    identity,
                ),
            )
            c.execute("DELETE FROM acquisition_circuits WHERE account=? AND path=?", (self.account, path))
        return result

    def page(self, response, *, favorite):
        scan = self.work.setdefault(
            "scan",
            {"page": 1, "imported": 0, "seen": [], "matches": {}, "total": None, "started": time.time()},
        )
        rows = (
            response
            if isinstance(response, list)
            else next(
                (response[k] for k in ("data", "list", "rows", "items") if isinstance(response.get(k), list)),
                None,
            )
            if isinstance(response, dict)
            else None
        )
        if rows is None or any(not isinstance(r, dict) for r in rows):
            raise ValueError("invalid_listing")
        total = response.get("total") if isinstance(response, dict) else None
        if total is not None and (isinstance(total, bool) or not str(total).isdigit()):
            raise ValueError("invalid_total")
        if total is not None:
            total = int(total)
        if scan["total"] is not None and total != scan["total"]:
            raise ValueError("listing_changed")
        scan["total"] = total
        ids = [str(r.get("id", "")) for r in rows]
        if (
            any(not i.isdigit() or int(i) <= 0 for i in ids)
            or len(set(ids)) != len(ids)
            or set(ids) & set(scan["seen"])
        ):
            raise ValueError("listing_duplicate_or_invalid")
        scan["seen"] += ids
        if not favorite:
            # Positive hints only. Absence is never cached or used to authorize writes.
            with self.db.connect() as c:
                for r in rows:
                    if r.get("collect_from") == "ozon" and str(r.get("goods_id", "")).isdigit():
                        c.execute(
                            "INSERT OR REPLACE INTO source_draft_index VALUES(?,?,?,?)",
                            (self.account, str(r["goods_id"]), str(r["id"]), time.time()),
                        )
                c.execute("DELETE FROM source_draft_index WHERE observed<?", (time.time() - 300,))
        for r in rows:
            match = (
                str(r.get("sku")) == self.s.sku
                if favorite
                else str(r.get("goods_id")) == self.s.sku and r.get("collect_from") == "ozon"
            )
            if match:
                scan["matches"][str(r["id"])] = r
        if total is not None and len(scan["seen"]) > int(total):
            raise ValueError("listing_changed")
        complete = len(scan["seen"]) == int(total) if total is not None else not rows
        if not complete and not rows:
            raise ValueError("listing_incomplete")
        if not complete:
            if scan["page"] >= 100:
                raise ValueError("listing_page_limit")
            scan["page"] += 1
            self.save()
            return None
        if favorite and scan["imported"] == 0:
            scan.update(imported=1, page=1, seen=[], total=None)
            self.save()
            return None
        matches = list(scan["matches"].values())
        if len(matches) > 1:
            raise ValueError("source_identity_ambiguous")
        return matches

    async def capacity(self, next_stage):
        from .collection_capacity import observe, settings
        from .collection_capacity import schema as capacity_schema

        cfg = settings(self.db)
        if cfg["enabled"]:
            with self.db.connect() as c:
                capacity_schema(c)
                row = c.execute(
                    "SELECT * FROM collection_capacity WHERE account=?", (self.account,)
                ).fetchone()
            if not row or not 0 <= time.time() - row["observed"] < cfg["max_age"]:
                began = time.time()
                header = await self.request("/api.product.collect/lists", query={"page": 1, "page_size": 1})
                observe(self.db, self.account, header, began)
                # The next tick checks the fresh observation; no second request.
                await asyncio.to_thread(self.save)
                return
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute(
                    "SELECT * FROM collection_capacity WHERE account=?", (self.account,)
                ).fetchone()
                pending = c.execute(
                    "SELECT count(*) FROM collection_reservations WHERE account=?", (self.account,)
                ).fetchone()[0]
                if row["blocked"] or row["used"] + pending >= row["capacity"] * cfg["stop_ratio"]:
                    raise AcquisitionPending("collection_box_full", delay=60, category="capacity")
                if next_stage == "import_draft":
                    c.execute(
                        "INSERT OR IGNORE INTO collection_reservations VALUES(?,?,?,?)",
                        (self.account, self.key, "unknown", time.time()),
                    )
        await self.advance(next_stage)

    async def step(self):
        w = self.work
        data = w["data"]
        stage = w["stage"]
        if stage in ("find_favorite", "find_draft"):
            favorite = stage == "find_favorite"
            if not favorite and not w.get("scan"):
                with self.db.connect() as c:
                    hints = c.execute(
                        "SELECT draft_id FROM source_draft_index WHERE account=? AND sku=? AND observed>?",
                        (self.account, self.s.sku, time.time() - 30),
                    ).fetchall()
                if len(hints) == 1:
                    data["draft_id"] = int(hints[0][0])
                    w["unknown_draft"] = False
                    await self.advance("read_draft", legacy="draft_ready")
                    return
            scan = w.get("scan", {})
            # Bound a scan's observation window; never reuse old absence evidence.
            if scan and time.time() - scan["started"] > 300:
                w.pop("scan")
                scan = {}
            path = "/api.product.favorite/lists" if favorite else "/api.product.collect/lists"
            query = {"page": scan.get("page", 1), "page_size": 100}
            if favorite:
                query.update(sku=self.s.sku, is_imported=scan.get("imported", 0))
            result = await self.request(path, query=query)
            matches = await asyncio.to_thread(self.page, result, favorite=favorite)
            if matches is None:
                return
            w.pop("scan", None)
            if not matches:
                if favorite and not w["unknown_favorite"] and not w["unknown_draft"]:
                    if self.s.c.get("existing_favorite_only") or not self.s.c["candidate"].get("price"):
                        raise AcquisitionPending("real_price_required", delay=300, category="missing_fields")
                    w["absence_at"] = time.time()
                    await self.advance("favorite_capacity")
                    return
                raise AcquisitionPending("write_result_not_visible", delay=60, category="remote_pending")
            found = matches[0]
            if favorite:
                data["favorite_id"] = int(found["id"])
                w["unknown_favorite"] = False
                await self.advance(
                    "find_draft"
                    if str(found.get("is_imported")) == "1" or w["unknown_draft"]
                    else "draft_capacity"
                )
            else:
                data["draft_id"] = int(found["id"])
                w["unknown_draft"] = False
                await self.advance("read_draft", legacy="draft_ready")
            return
        if stage in ("favorite_capacity", "draft_capacity"):
            await self.capacity("create_favorite" if stage == "favorite_capacity" else "import_draft")
            return
        if stage == "create_favorite":
            if time.time() - w.get("absence_at", 0) > 60:
                await self.advance("find_favorite")
                return
            p = w.setdefault("favorite_intent", dict(self.s.c["candidate"]))
            data.update(source_key=self.s.sku, favorite_attempted=True)
            w["unknown_favorite"] = True
            await self.advance("find_favorite", legacy="favorite_started")
            await self.request(
                "/api.product.favorite/toggle",
                "POST",
                body={
                    "status": True,
                    "productInfo": {
                        "sku": self.s.sku,
                        "title": p.get("title", ""),
                        "coverImage": p.get("image", ""),
                        "price_info": {"sell_price": p["price"], "currency": "CNY"},
                    },
                },
            )
            return
        if stage == "import_draft":
            w["unknown_draft"] = True
            await self.advance("find_draft", legacy="draft_started")
            result = await self.request(
                "/api.product.favorite/edit_import", "POST", body={"id": data["favorite_id"]}
            )
            identifier = result.get("jump_id") or result.get("id")
            if not str(identifier).isdigit() or int(identifier) <= 0:
                raise ValueError("draft_acknowledgement_missing")
            data["draft_id"] = int(identifier)
            w["unknown_draft"] = False
            await self.advance("read_draft", legacy="draft_ready")
            return
        if stage == "read_draft":
            with self.db.connect() as c:
                receipt = None
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='draft_cleanup_receipts'").fetchone():
                    receipt = c.execute(
                        "SELECT body FROM draft_cleanup_receipts WHERE account=? AND draft_id=? AND sku=? AND state='deleted'",
                        (self.account, str(data["draft_id"]), self.s.sku),
                    ).fetchone()
            if receipt:
                backup = self.db.open(receipt[0])
                if not 0 <= time.time() - backup.get("at", 0) < 21600:
                    raise ValueError("deleted_draft_archive_stale_requires_reacquisition_review")
                detail = backup.get("fresh_detail")
                observed = backup["at"]
            else:
                observed = time.time()
                detail = await self.request(
                    "/api.product.collect/detail", query={"id": data["draft_id"], "is_online": 0}
                )
            if not isinstance(detail, dict) or len(detail.get("skus", [])) != 1:
                raise ValueError("source_variant_ambiguous")
            if detail.get("goods_id") is not None and str(detail["goods_id"]) != self.s.sku:
                raise ValueError("source_identity_mismatch")
            data.update(source_key=self.s.sku, detail=detail, observed_at=observed)
            digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            with self.db.connect() as c:
                if not self.owned(c):
                    raise AcquisitionPending("acquisition_ownership_changed")
                c.execute(
                    "INSERT OR IGNORE INTO dossier_snapshots VALUES(?,?,?,?)",
                    (digest, self.key, self.db.seal(data), data["observed_at"]),
                )
            w["snapshot_hash"] = digest
            await self.advance("ready", legacy="ready")
            from .collection_capacity import finish

            finish(self.s)
            return
        if stage != "ready":
            raise ValueError("invalid_acquisition_stage")

    async def tick(self):
        from .pipeline_modules.favorite_release import source_guard
        with source_guard(self.db.directory,self.s.sku,busy=AcquisitionPending):
            return await self._guarded_tick()

    async def _guarded_tick(self):
        await asyncio.to_thread(self.claim)
        heartbeat = asyncio.create_task(self.renew())
        try:
            before = self.work["stage"]
            previous = copy.deepcopy(self.work)
            if before == "ready":
                cached = self.s.cached_snapshot()
                if cached:
                    return cached
                await self.advance("read_draft")
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if getattr(error, "diagnostic", {}).get("not_sent"):
                    # The gateway proved there was no network dispatch. Restore the
                    # operation, never do this for a lost/uncertain acknowledgement.
                    self.work = previous
                    category = (
                        error.category
                        if isinstance(error, AcquisitionPending)
                        else "rate_deferred"
                        if any(v in str(error).lower() for v in ("pacing", "pool_busy", "rate_limit"))
                        else "contract_invalid"
                    )
                    error = AcquisitionPending(
                        "request_not_sent",
                        delay=max(1, (getattr(error, "diagnostic", {}).get("retry_after_ms") or 1000) / 1000),
                        category=category,
                    )
                if isinstance(error, AcquisitionPending):
                    category = error.category
                    delay = error.delay
                else:
                    category = classify(error)
                    delay = 30
                    if self.work.get("unknown_favorite") or self.work.get("unknown_draft"):
                        category = "remote_pending"
                    if isinstance(error, ValueError):
                        category = (
                            "identity_mismatch"
                            if "identity" in str(error) or "ambiguous" in str(error)
                            else "contract_invalid"
                        )
                        if str(error) in ("listing_changed", "listing_incomplete"):
                            category = "remote_pending"
                            self.work.pop("scan", None)
                self.work["failures"] += 1
                if category in ("identity_mismatch", "contract_invalid"):
                    self.work["manual"] = str(error)
                self.work.update(
                    reason=str(error)[:200],
                    category=category,
                    due=time.time()
                    + (
                        delay
                        if category == "rate_deferred"
                        else max(delay, min(900, 30 * 2 ** min(self.work["failures"] - 1, 5)))
                    ),
                )
                await asyncio.to_thread(self.save)
                raise AcquisitionPending(
                    self.work["reason"], delay=self.work["due"] - time.time(), category=category
                ) from error
            if self.work["stage"] == "ready":
                return self.work["data"]
            raise AcquisitionPending("acquisition:" + self.work["stage"], progress=True)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.to_thread(self.release)


# Bounded operation runners are independent of SKU business ticks. Their fenced
# leases are visible to cleanup/drain; the checkpoint is authoritative on restart.
RUNNERS = {}


async def dispatch(collector):
    loop = asyncio.get_running_loop()
    key = (loop, str(collector.db.path), collector.key)
    # Evict completed runners: results/checkpoints already reside in the journals.
    for old, task in list(RUNNERS.items()):
        if task.done():
            RUNNERS.pop(old, None)
    if key in RUNNERS:
        raise AcquisitionPending("operation_inflight", delay=2)
    active = sum(k[0] is loop and k[1] == str(collector.db.path) and not t.done() for k, t in RUNNERS.items())
    if active >= 2:
        raise AcquisitionPending("operation_pool_busy", delay=2, category="rate_deferred")
    machine = await asyncio.to_thread(Acquisition, collector)
    # No await between the second capacity check and registration.
    if (
        key in RUNNERS
        or sum(k[0] is loop and k[1] == str(collector.db.path) and not t.done() for k, t in RUNNERS.items())
        >= 2
    ):
        raise AcquisitionPending("operation_pool_busy", delay=2, category="rate_deferred")
    task = asyncio.create_task(machine.tick())
    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
    RUNNERS[key] = task
    done, _ = await asyncio.wait({task}, timeout=0.05)
    if done:
        RUNNERS.pop(key, None)
        return task.result()
    raise AcquisitionPending("operation_inflight", delay=2)


async def close_runners():
    loop = asyncio.get_running_loop()
    tasks = [t for k, t in RUNNERS.items() if k[0] is loop]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for key in list(RUNNERS):
        if key[0] is loop:
            RUNNERS.pop(key, None)
