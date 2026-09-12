"""Persistent, tenant-scoped sourcing. Unknown observations remain unknown."""

import hashlib
import json
import math
import secrets
import time
from dataclasses import dataclass, field


def numeric(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def identity(value):
    value = str(value or "")
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("numeric product/seller identity required")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class SourceFilters:
    price_min: float | None = None
    price_max: float | None = None
    weight_max_g: float | None = None
    sales_min: float | None = None
    categories: list[str] = field(default_factory=list)
    pure_fbs: bool = True
    require_follow_allowed: bool = False
    same_seller_only: bool = False
    max_age_hours: float = 168

    def __post_init__(self):
        for name in ("price_min", "price_max", "weight_max_g", "sales_min", "max_age_hours"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or numeric(value) is None):
                raise ValueError(f"invalid {name}")
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("price range reversed")
        if not isinstance(self.categories, list) or any(not isinstance(x, str) for x in self.categories):
            raise ValueError("invalid categories")
        if not isinstance(self.require_follow_allowed, bool) or not isinstance(self.same_seller_only, bool):
            raise ValueError("invalid follow rule")
        if not isinstance(self.pure_fbs, bool) or self.max_age_hours is None or self.max_age_hours <= 0:
            raise ValueError("invalid freshness or shipping rule")


def assess(product, filters, now=None):
    now = time.time() if now is None else now
    passed, failed, missing = [], [], []
    for key in ("sku", "seller_id", "title", "image", "category_id"):
        (passed if product.get(key) else missing).append(key)
    for name, key, bound, compare in (
        ("price_min", "average_price_rub", filters.price_min, lambda a, b: a >= b),
        ("price_max", "average_price_rub", filters.price_max, lambda a, b: a <= b),
        ("weight_max_g", "weight_g", filters.weight_max_g, lambda a, b: a <= b),
        ("sales_min", "sold_count_28d", filters.sales_min, lambda a, b: a >= b),
    ):
        if bound is None:
            continue
        value = numeric(
            (product.get("live_check") or {}).get(key)
            if key == "weight_g" and product.get("live_check")
            else product.get(key)
        )
        (missing if value is None else passed if compare(value, bound) else failed).append(name)
    if filters.categories:
        value = product.get("category_id")
        (missing if not value else passed if value in filters.categories else failed).append("category")
    if filters.require_follow_allowed:
        blocked = (product.get("raw") or {}).get("blocked_by_seller")
        (passed if blocked is False else failed if blocked is True else missing).append("follow_allowed")
    if filters.same_seller_only:
        relation = product.get("source_relation") or {}
        valid = (
            relation.get("kind") == "same_seller"
            and relation.get("seller_id") == product.get("seller_id")
            and bool(relation.get("root_seeds"))
        )
        (passed if valid else missing).append("same_seller")
    if filters.pure_fbs:
        live = product.get("live_check")
        value = live.get("sales_schema") if live else product.get("sales_schema")
        (missing if not value else passed if value == "FBS" else failed).append("pure_fbs")
        if live:
            observed = numeric(live.get("observed_at"))
            (missing if observed is None or observed > now or now - observed > 21600 else passed).append(
                "live_freshness"
            )
    at = numeric(product.get("collected_at"))
    (missing if at is None or at > now or now - at > filters.max_age_hours * 3600 else passed).append(
        "freshness"
    )
    return {
        "state": "rejected" if failed else "needs_review" if missing else "qualified",
        "passed": passed,
        "failed": failed,
        "missing": missing,
    }


def ranking_product(row, observed_at):
    """ERP ranking price is statistical RUB; never a current selling price."""
    return {
        "sku": identity(row.get("sku")),
        "seller_id": identity(row["seller_id"])
        if str(row.get("seller_id") or "0").isdigit() and int(row.get("seller_id") or 0) > 0
        else None,
        "title": str(row.get("name") or ""),
        "image": str(row.get("photo") or ""),
        "url": f"https://www.ozon.ru/product/{identity(row.get('sku'))}/",
        "category_id": str(row.get("cate2_id") or ""),
        "average_price_rub": numeric(row.get("avg_price")),
        "current_price_rub": None,
        "weight_g": numeric(row.get("weight")),
        "dimensions_cm": None,
        "sold_count_28d": numeric(row.get("sold_count")),
        "sales_schema": row.get("sales_schema"),
        "specifications": None,
        "collected_at": observed_at,
        "verified_at": None,
        "provider_updated_at": row.get("update_time"),
        "raw": row,
    }


class SourceLibrary:
    def __init__(self, db):
        self.db = db
        with db.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS sourcing_products(
              id INTEGER PRIMARY KEY,owner TEXT NOT NULL,sku TEXT NOT NULL,seller TEXT NOT NULL,
              body TEXT NOT NULL,first_seen REAL NOT NULL,updated REAL NOT NULL,UNIQUE(owner,sku,seller));
            CREATE INDEX IF NOT EXISTS sourcing_product_lookup ON sourcing_products(owner,updated,id);
            CREATE INDEX IF NOT EXISTS sourcing_filter_price ON sourcing_products(owner,json_extract(body,'$.average_price_rub'),id);
            CREATE INDEX IF NOT EXISTS sourcing_filter_category ON sourcing_products(owner,json_extract(body,'$.category_id'),id);
            CREATE TABLE IF NOT EXISTS sourcing_evidence(
              owner TEXT NOT NULL,hash TEXT NOT NULL,sku TEXT NOT NULL,body TEXT NOT NULL,at REAL NOT NULL,
              PRIMARY KEY(owner,hash));
            CREATE TABLE IF NOT EXISTS sourcing_seeds(
              owner TEXT NOT NULL,shop TEXT NOT NULL,offer TEXT NOT NULL,sku TEXT NOT NULL,
              body TEXT NOT NULL,archived INTEGER,sales REAL,checked REAL,
              PRIMARY KEY(owner,shop,offer));
            CREATE TABLE IF NOT EXISTS sourcing_tasks(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,
              page INTEGER NOT NULL DEFAULT 1,due REAL NOT NULL DEFAULT 0,priority REAL NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'ready',lease TEXT,lease_until REAL NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0,error TEXT,successes INTEGER NOT NULL DEFAULT 0,
              added INTEGER NOT NULL DEFAULT 0,last_at REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS sourcing_due ON sourcing_tasks(owner,state,due,lease_until);
            CREATE TABLE IF NOT EXISTS sourcing_pages(task TEXT NOT NULL,page INTEGER NOT NULL,hash TEXT NOT NULL,
              PRIMARY KEY(task,page));
            CREATE TABLE IF NOT EXISTS sourcing_yields(task TEXT PRIMARY KEY,requests INTEGER NOT NULL DEFAULT 0,
              qualified_added INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sourcing_sellers(owner TEXT NOT NULL,seller TEXT NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(owner,seller));
            CREATE TABLE IF NOT EXISTS sourcing_orders(owner TEXT NOT NULL,shop TEXT NOT NULL,posting TEXT NOT NULL,offer TEXT NOT NULL,quantity REAL NOT NULL,PRIMARY KEY(owner,shop,posting,offer));
            CREATE TABLE IF NOT EXISTS sourcing_attempts(
              id INTEGER PRIMARY KEY,owner TEXT NOT NULL,task TEXT NOT NULL,page INTEGER NOT NULL,
              at REAL NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sourcing_attempt_task ON sourcing_attempts(owner,task,id);
            CREATE TABLE IF NOT EXISTS sourcing_settings(owner TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
              body TEXT NOT NULL DEFAULT '{}',secret TEXT NOT NULL);
            """)

    def enqueue(self, owner, kind, body, priority=0, connection=None):
        key = fingerprint([owner, kind, body])

        def save(c):
            c.execute(
                "INSERT OR IGNORE INTO sourcing_tasks(id,owner,kind,body,priority) VALUES(?,?,?,?,?)",
                (key, owner, kind, json.dumps(body), priority),
            )
            c.execute("UPDATE sourcing_tasks SET priority=MAX(priority,?) WHERE id=?", (priority, key))

        if connection is not None:
            save(connection)
        else:
            with self.db.connect() as c:
                save(c)
        return key

    def put(self, owner, product, provenance, connection=None):
        sku = identity(product["sku"])
        seller = str(product.get("seller_id") or "")
        now = product["collected_at"]
        evidence = {"product": product, "provenance": provenance}
        digest = fingerprint(evidence)

        def save(c):
            c.execute(
                "INSERT OR IGNORE INTO sourcing_evidence VALUES(?,?,?,?,?)",
                (owner, digest, sku, json.dumps(evidence), now),
            )
            prior = c.execute(
                "SELECT body,updated FROM sourcing_products WHERE owner=? AND sku=? AND seller=?",
                (owner, sku, seller),
            ).fetchone()
            if prior and prior["updated"] > now:
                return False
            body = product | {"evidence_hash": digest, "provenance": provenance}
            if prior:
                previous_review = json.loads(prior["body"]).get("listing_review")
                incoming_review = body.get("listing_review") or {}
                if previous_review and previous_review.get("observed_at", 0) > incoming_review.get(
                    "observed_at", 0
                ):
                    body["listing_review"] = previous_review
            if product.get("source_relation"):
                body["source_relation_evidence_hash"] = digest
            elif prior:
                previous = json.loads(prior["body"])
                if previous.get("source_relation"):
                    body["source_relation"] = previous["source_relation"]
                    body["source_relation_evidence_hash"] = previous.get(
                        "source_relation_evidence_hash", previous["evidence_hash"]
                    )
            c.execute(
                """INSERT INTO sourcing_products(owner,sku,seller,body,first_seen,updated) VALUES(?,?,?,?,?,?)
              ON CONFLICT(owner,sku,seller) DO UPDATE SET body=excluded.body,updated=excluded.updated""",
                (owner, sku, seller, json.dumps(body), now, now),
            )
            return prior is None

        if connection is not None:
            return save(connection)
        with self.db.connect() as c:
            return save(c)

    def control_task(self, owner, task_id, action):
        if action not in ("pause", "resume", "retry"):
            raise ValueError("invalid action")
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM sourcing_tasks WHERE owner=? AND id=?", (owner, task_id)
            ).fetchone()
            if not row:
                raise KeyError(task_id)
            if action == "pause":
                # Keep an in-flight lease: its page can commit, but no new page is claimed.
                c.execute("UPDATE sourcing_tasks SET state='paused' WHERE id=?", (task_id,))
            elif row["state"] == "paused" or action == "retry" and row["state"] == "blocked":
                if row["lease_until"] > time.time():
                    raise ValueError("task still in flight")
                if row["state"] == "paused":
                    c.execute("UPDATE sourcing_tasks SET state='ready' WHERE id=?", (task_id,))
                else:
                    c.execute(
                        "UPDATE sourcing_tasks SET state='ready',due=0,failures=0 WHERE id=?", (task_id,)
                    )
            return dict(c.execute("SELECT * FROM sourcing_tasks WHERE id=?", (task_id,)).fetchone())

    def audit_attempt(self, c, task, state, now, diagnostic=None):
        body = {
            "origin": task["body"],
            "query": task.get("request_query"),
            "root_seeds": task.get("root_seeds", []),
            "request_started_at": task.get("request_started_at"),
            "response_observed_at": task.get("response_observed_at"),
            "response": task.get("response"),
            "diagnostic": diagnostic or {},
            "coverage": "ranking-exact-seller" if task["kind"] == "seller_scan" else task["kind"],
        }
        c.execute(
            "INSERT INTO sourcing_attempts(owner,task,page,at,state,body) VALUES(?,?,?,?,?,?)",
            (task["owner"], task["id"], task["page"], now, state, json.dumps(body)),
        )

    def claim(self, owner, now=None, kinds=None):
        now = time.time() if now is None else now
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Reserve a turn for each pipeline stage; thousands of seeds cannot starve discovery.
            completed = c.execute(
                "SELECT COALESCE(SUM(successes),0) FROM sourcing_tasks WHERE owner=?", (owner,)
            ).fetchone()[0]
            stages = (
                "own_shop",
                "own_orders",
                "seed",
                "seller_scan",
                "seller",
                "category",
                "seller_scan",
                "keyword_lookup",
                "keyword",
            )
            preferred = stages[completed % len(stages)]
            order = (
                "CASE WHEN kind=? THEN 0 ELSE 1 END,priority + "
                "COALESCE((SELECT MIN(20,qualified_added*5.0/(requests+1)) FROM sourcing_yields y WHERE y.task=sourcing_tasks.id),0) DESC,last_at"
            )
            kind_clause = " AND kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
            row = c.execute(
                f"SELECT * FROM sourcing_tasks WHERE owner=? AND state='ready' AND due<=? AND lease_until<=?{kind_clause} ORDER BY {order},id LIMIT 1",
                (owner, now, now, *(kinds or ()), preferred),
            ).fetchone()
            if not row:
                return None
            lease = secrets.token_hex(16)
            c.execute(
                "UPDATE sourcing_tasks SET lease=?,lease_until=? WHERE id=?", (lease, now + 150, row["id"])
            )
            return dict(row) | {"lease": lease, "body": json.loads(row["body"])}

    def commit_page(self, task, rows, products, last_page, now=None):
        now = time.time() if now is None else now
        page = task["page"]
        signature = fingerprint(sorted(str(r.get("sku") or r.get("id")) for r in rows))
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            held = c.execute(
                "SELECT 1 FROM sourcing_tasks WHERE id=? AND lease=? AND lease_until>?",
                (task["id"], task["lease"], now),
            ).fetchone()
            if not held:
                raise ValueError("source lease expired")
            if (
                rows
                and c.execute(
                    "SELECT 1 FROM sourcing_pages WHERE task=? AND page<>? AND hash=?",
                    (task["id"], page, signature),
                ).fetchone()
            ):
                raise ValueError("repeated_page")
            settings = c.execute(
                "SELECT body FROM sourcing_settings WHERE owner=?", (task["owner"],)
            ).fetchone()
            filters = SourceFilters(**json.loads(settings[0])) if settings else SourceFilters()
            # Reward new qualifying SKUs, never total rows or rediscovered listings.
            qualified_new = {
                p["sku"]
                for p in products
                if assess(p, filters, now)["state"] == "qualified"
                and not c.execute(
                    "SELECT 1 FROM sourcing_products WHERE owner=? AND sku=?", (task["owner"], p["sku"])
                ).fetchone()
                and not c.execute(
                    "SELECT 1 FROM sourcing_seeds WHERE owner=? AND sku=?", (task["owner"], p["sku"])
                ).fetchone()
                and not c.execute(
                    "SELECT 1 FROM blocks WHERE owner=? AND source_key=?", (task["owner"], p["sku"])
                ).fetchone()
            }
            added = sum(
                self.put(
                    task["owner"],
                    p,
                    {
                        "channel": "maozi-ranking",
                        "task": task["id"],
                        "query": task.get("request_query", task["body"]),
                        "origin": task["body"],
                        "page": page,
                        "coverage": "ranking-exact-seller"
                        if task["kind"] == "seller_scan"
                        else "ranking-only",
                        "root_seeds": task.get("root_seeds", []),
                    },
                    c,
                )
                for p in products
            )
            cached_seller_links = 0
            for p in products:
                if task["kind"] == "seed" and p.get("category_id"):
                    for seed in c.execute(
                        "SELECT shop,offer,body FROM sourcing_seeds WHERE owner=? AND sku=? AND archived=0 AND checked>?",
                        (task["owner"], p["sku"], now - 21600),
                    ).fetchall():
                        body = json.loads(seed["body"]) | {
                            "category_id": p["category_id"],
                            "seller_id": p.get("seller_id"),
                        }
                        c.execute(
                            "UPDATE sourcing_seeds SET body=? WHERE owner=? AND shop=? AND offer=?",
                            (json.dumps(body), task["owner"], seed["shop"], seed["offer"]),
                        )
                    for mode in ("hot", "china", "potential"):
                        self.enqueue(
                            task["owner"],
                            "category",
                            {"category2": p["category_id"], "mainType": mode},
                            30,
                            c,
                        )
                    if p.get("seller_id"):
                        roots = [
                            dict(r)
                            for r in c.execute(
                                "SELECT sku,shop,offer FROM sourcing_seeds s WHERE owner=? AND archived=0 AND checked>? AND json_extract(body,'$.seller_id')=? AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)",
                                (task["owner"], now - 21600, p["seller_id"]),
                            ).fetchall()
                        ]
                        # Join fresh, already collected products before spending another API request.
                        # Limit each transaction; later seed resolutions drain remaining unlinked rows.
                        if roots:
                            cached = c.execute(
                                "SELECT body FROM sourcing_products p WHERE owner=? AND seller=? AND updated>=? AND json_extract(body,'$.source_relation.kind') IS NULL AND NOT EXISTS(SELECT 1 FROM sourcing_seeds s WHERE s.owner=p.owner AND s.sku=p.sku) AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku) ORDER BY id LIMIT 500",
                                (task["owner"], p["seller_id"], now - 604800),
                            ).fetchall()
                            for row in cached:
                                existing = json.loads(row["body"])
                                existing["source_relation"] = {
                                    "kind": "same_seller",
                                    "seller_id": p["seller_id"],
                                    "root_seeds": roots,
                                    "observed_at": now,
                                    "coverage": "cached-ranking-only",
                                }
                                self.put(
                                    task["owner"],
                                    existing,
                                    {
                                        "channel": "seed-seller-cache-join",
                                        "root_seeds": roots,
                                        "bound_at": now,
                                        "previous_evidence_hash": existing.get("evidence_hash"),
                                    },
                                    c,
                                )
                                cached_seller_links += 1
                        for mode in ("hot", "china"):
                            self.enqueue(
                                task["owner"],
                                "seller_scan",
                                {
                                    "seller_id": p["seller_id"],
                                    "category2": p["category_id"],
                                    "mainType": mode,
                                },
                                35,
                                c,
                            )
                if p.get("seller_id") and p.get("category_id"):
                    self.enqueue(
                        task["owner"],
                        "seller",
                        {"seller_id": p["seller_id"]},
                        min(20, task["priority"]),
                        c,
                    )
            c.execute("INSERT OR REPLACE INTO sourcing_pages VALUES(?,?,?)", (task["id"], page, signature))
            c.execute(
                "INSERT INTO sourcing_yields VALUES(?,1,?) ON CONFLICT(task) DO UPDATE SET requests=requests+1,qualified_added=qualified_added+excluded.qualified_added",
                (task["id"], len(qualified_new)),
            )
            self.audit_attempt(c, task, "committed", now)
            done = not rows or last_page is not None and page >= last_page
            if done:
                c.execute("DELETE FROM sourcing_pages WHERE task=?", (task["id"],))
            c.execute(
                "UPDATE sourcing_tasks SET page=?,due=?,lease=NULL,lease_until=0,failures=0,error=NULL,successes=successes+1,added=added+?,last_at=? WHERE id=?",
                (1 if done else page + 1, now + (86400 if done else 5), added, now, task["id"]),
            )
        return {
            "rows": len(rows),
            "added": added,
            "qualified_added": len(qualified_new),
            "cached_seller_links": cached_seller_links,
            "finished": done,
        }

    def fail(self, task, kind, now=None, diagnostic=None):
        now = time.time() if now is None else now
        attempts = task["failures"] + 1
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            held = c.execute(
                "SELECT state FROM sourcing_tasks WHERE id=? AND lease=?", (task["id"], task["lease"])
            ).fetchone()
            if not held:
                return
            self.audit_attempt(c, task, kind, now, diagnostic)
            c.execute(
                "UPDATE sourcing_tasks SET failures=?,error=?,state=?,due=?,lease=NULL,lease_until=0,last_at=? WHERE id=? AND lease=?",
                (
                    attempts,
                    kind,
                    "paused"
                    if held["state"] == "paused"
                    else "blocked"
                    if attempts >= 3
                    or kind
                    in ("authentication", "unsupported", "identity_mismatch", "repeated_page", "schema")
                    else "ready",
                    now + min(3600, 30 * 2 ** min(attempts, 7)),
                    now,
                    task["id"],
                    task["lease"],
                ),
            )

    def query(self, owner, filters=None, cursor=0, limit=50, state=None):
        filters = filters or SourceFilters()
        limit = max(1, min(100, int(limit)))
        clauses, parameters = ["p.owner=?", "p.id>?"], [owner, int(cursor)]
        if filters.same_seller_only:
            clauses.append("json_extract(p.body,'$.source_relation.kind')='same_seller'")
        if state == "qualified":
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM sourcing_seeds s WHERE s.owner=p.owner AND s.sku=p.sku)"
            )
            for key, operator, bound in (
                ("average_price_rub", ">=", filters.price_min),
                ("average_price_rub", "<=", filters.price_max),
                ("weight_g", "<=", filters.weight_max_g),
                ("sold_count_28d", ">=", filters.sales_min),
            ):
                if bound is not None:
                    value = (
                        "CASE WHEN json_extract(p.body,'$.live_check') IS NULL THEN json_extract(p.body,'$.weight_g') ELSE json_extract(p.body,'$.live_check.weight_g') END"
                        if key == "weight_g"
                        else f"json_extract(p.body,'$.{key}')"
                    )
                    clauses.append(f"{value} {operator} ?")
                    parameters.append(bound)
            if filters.categories:
                clauses.append(
                    "json_extract(p.body,'$.category_id') IN ("
                    + ",".join("?" for _ in filters.categories)
                    + ")"
                )
                parameters.extend(filters.categories)
            if filters.pure_fbs:
                clauses.append(
                    "CASE WHEN json_extract(p.body,'$.live_check') IS NULL THEN json_extract(p.body,'$.sales_schema') ELSE json_extract(p.body,'$.live_check.sales_schema') END='FBS'"
                )
            clauses.extend(["p.updated>=?", "p.updated<=?"])
            now = time.time()
            parameters.extend([now - filters.max_age_hours * 3600, now])
        clauses.append("NOT EXISTS (SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku)")
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT p.* FROM sourcing_products p WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT 1000",
                parameters,
            ).fetchall()
        result, scanned, next_cursor = [], 0, int(cursor)
        for row in rows:
            scanned += 1
            next_cursor = row["id"]
            p = json.loads(row["body"]) | {"first_seen": row["first_seen"]}
            p["assessment"] = assess(p, filters)
            if not state or p["assessment"]["state"] == state:
                result.append(p)
            if len(result) == limit:
                break
        return {
            "items": result,
            "cursor": next_cursor,
            "scanned": scanned,
            "has_more": scanned < len(rows) or len(rows) == 1000,
        }

    def status(self, owner):
        with self.db.connect() as c:
            counts = {
                name: c.execute(f"SELECT COUNT(*) FROM sourcing_{name} WHERE owner=?", (owner,)).fetchone()[0]
                for name in ("products", "seeds", "tasks", "evidence")
            }
            tasks = [
                dict(r)
                for r in c.execute(
                    "SELECT kind,state,COUNT(*) AS count,SUM(added) AS added FROM sourcing_tasks WHERE owner=? GROUP BY kind,state",
                    (owner,),
                )
            ]
            settings = c.execute(
                "SELECT enabled,body FROM sourcing_settings WHERE owner=?", (owner,)
            ).fetchone()
            errors = [
                dict(r)
                for r in c.execute(
                    "SELECT kind,state,error,last_at FROM sourcing_tasks WHERE owner=? AND error IS NOT NULL ORDER BY last_at DESC LIMIT 5",
                    (owner,),
                )
            ]
            seeds_ready = c.execute(
                "SELECT COUNT(*) FROM sourcing_seeds s WHERE owner=? AND archived=0 AND checked>? AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=s.owner AND b.source_key=s.sku)",
                (owner, time.time() - 21600),
            ).fetchone()[0]
        return counts | {
            "queues": tasks,
            "task_errors": errors,
            "current_seeds": seeds_ready,
            "enabled": bool(settings and settings["enabled"]),
            "filters": json.loads(settings["body"]) if settings else {},
            "capabilities": {
                "ranking": True,
                "keyword_expansion": True,
                "other_sellers": False,
                "whole_shop": False,
            },
        }


def review_candidate(product):
    """Main-software envelope for preliminary research; never invent publish inputs."""
    from .modules import image_url

    return {
        "source_key": identity(product["sku"]),
        "title": str(product["title"])[:250],
        "image": image_url(product["image"]),
        "price": None,
        "average_price_rub": product.get("average_price_rub"),
        "weight_g": product.get("weight_g"),
        "dimensions_cm": product.get("dimensions_cm"),
        "category_id": product["category_id"],
        "pure_fbs": (product.get("live_check") or {}).get("sales_schema", product.get("sales_schema"))
        == "FBS",
        "origin": product,
        "source_contract": "flowhub-source-candidates-v1",
        "review_required": True,
        "dossier_missing": ["current_price_rub", "specifications", "supplier_match"],
    }


def candidate_page(db, context):
    library = SourceLibrary(db)
    owner = context["owner"]
    with db.connect() as c:
        settings = c.execute("SELECT body FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
    filters = SourceFilters(**json.loads(settings[0])) if settings else SourceFilters()
    page = library.query(owner, filters, cursor=int(context.get("cursor") or 0), limit=10, state="qualified")
    return {"items": [review_candidate(p) for p in page["items"]], "cursor": str(page["cursor"])}


def refresh_delivery(db, owner, envelopes):
    """Recheck cached-page admission against the latest product, rule and block state."""
    with db.connect() as c:
        settings = c.execute("SELECT body FROM sourcing_settings WHERE owner=?", (owner,)).fetchone()
        filters = SourceFilters(**json.loads(settings[0])) if settings else SourceFilters()
        items = []
        for envelope in envelopes:
            origin = envelope["origin"]
            row = c.execute(
                "SELECT body FROM sourcing_products p WHERE owner=? AND sku=? AND seller=? AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.owner=p.owner AND b.source_key=p.sku) AND NOT EXISTS(SELECT 1 FROM sourcing_seeds s WHERE s.owner=p.owner AND s.sku=p.sku)",
                (owner, origin["sku"], str(origin.get("seller_id") or "")),
            ).fetchone()
            if row:
                product = json.loads(row[0])
                if assess(product, filters)["state"] == "qualified":
                    items.append(review_candidate(product))
        return items
