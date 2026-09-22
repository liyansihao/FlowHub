"""Official-only publication of complete local dossiers using the existing durable journal.

The selected backend is pinned BEFORE any external write. A legacy record is never
adopted by this publisher, even when configuration or credentials change.
"""

import json
import time
from dataclasses import asdict

import httpx

from . import official_api
from .ozon_direct import OzonDirectPublisher, dossier
from .source_library import fingerprint


def backend(config):
    value = config.get("publication_backend", "official_only")
    if value not in ("official_preferred", "official_only", "maozi"):
        raise ValueError("invalid_publication_backend")
    return value


class LocalPublisher(OzonDirectPublisher):
    """No lazy source collection, including when an ERP token exists."""

    def __init__(self, context, db, api):
        context = context | {
            "store": context["store"]
            | {"credentials": {k: v for k, v in context["store"]["credentials"].items() if k != "erp_token"}}
        }
        super().__init__(context, db)
        self.api = api

    async def seller(self, path, body):
        response = await self.api.post(path, json=body)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("invalid_official_response")
        return result


async def hydrate_local_dossier(publisher, db, context):
    """Use local facts plus official metadata, without source acquisition."""
    if publisher.c['candidate'].get('origin', {}).get('ozon_dossier', {}).get('attributes'):
        return []
    from .source_detail import SourceCollector, map_detail
    from .modules import ModuleError

    snapshot = SourceCollector(db, context).cached_snapshot()
    if not snapshot:
        return []
    mapping = snapshot.get('mapping')
    try:
        if (not mapping or mapping.get('mapping_version') != 3
                or snapshot.get('mapping_client') != str(publisher.keys['client_id'])):
            mapping = await map_detail(snapshot, publisher.c, publisher.seller)
    except ModuleError as error:
        return [str(error)]
    issues = mapping.get('issues', []) + [f'{aid}：必填属性缺失' for aid in mapping.get('required_missing', [])]
    if not issues:
        publisher.c = publisher.c | {'prepared': {'source_dossier': mapping['dossier']}}
    return issues


async def advance_if_selected(db, owner, sku, seller):
    from flowef.application.errors import RequestNotSent
    from flowef.application.ports.test_listing import ListedProduct

    from . import plugin_publication as legacy
    from .official_inventory import ScheduledInventoryAdapter
    from .pipeline_modules.dossier import require_unchanged_source
    from .pipeline_modules.store_capacity import observe

    key = (owner, sku, seller)
    with db.connect() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL,PRIMARY KEY(owner,sku,seller))"
        )
        row = c.execute(
            "SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?", key
        ).fetchone()
        record = json.loads(row[0]) if row else None
        if record and record.get("backend") != "official":
            return None
        wf = c.execute("SELECT * FROM workflows WHERE owner=?", (owner,)).fetchone()
        if wf is None:
            raise ValueError("review_missing")
        route = (
            c.execute("SELECT * FROM plugin_routes WHERE owner=? AND sku=? AND seller=?", key).fetchone()
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_routes'").fetchone()
            else None
        )
        target = record["store_id"] if record else route["store_id"] if route else wf["active_store"]
        store = c.execute(
            "SELECT * FROM stores WHERE owner=? AND id=? AND verified=1", (owner, target)
        ).fetchone()
        if store is None:
            raise ValueError("active_verified_store_required")
        cfg = json.loads(store["config"])
        keys = db.open(store["secret"])
        mode = backend(cfg)
        if not record and mode == "maozi":
            return None
        if not keys.get("client_id") or not keys.get("api_key"):
            raise ValueError("official_credentials_required")
        r = c.execute("SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?", key).fetchone()
        if not r:
            raise ValueError("review_missing")
        latest = json.loads(r[0])
        review = record["review"] if record else latest
        permission = (
            c.execute(
                "SELECT * FROM plugin_publication_permissions WHERE owner=? AND sku=? AND seller=?", key
            ).fetchone()
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publication_permissions'").fetchone()
            else None
        )
        allow_unknown = bool(
            permission and permission["expires"] > time.time() and route and route["expires"] > time.time()
        )
        if (
            not store["enabled"]
            and not (route and route["expires"] > time.time())
            and not (record and record.get("authorized_store_id") == target)
        ):
            raise ValueError("active_verified_store_required")
        if (
            not record
            and c.execute("SELECT 1 FROM jobs WHERE owner=? AND source_key=?", (owner, sku)).fetchone()
        ):
            raise ValueError("existing_source_job")
        # A source may have another seller row, but may not acquire another publication.
        if not record and c.execute("SELECT 1 FROM plugin_publications WHERE sku=?", (sku,)).fetchone():
            raise ValueError("source_already_claimed")
    from .acquisition import enabled as acquisition_enabled
    if not record and acquisition_enabled(db,sku):
        from .dossier_gate import valid as dossier_gate_valid
        with db.connect() as c:
            certified=dossier_gate_valid(c,key)
        if not certified:
            return {'phase':'awaiting_dossier','needs_dossier':True,'verified':False,
                    'reason':'publication_dossier_gate_required','missing_fields':[], 'backend':'official'}
    rules = json.loads(wf["rules"])
    if int(rules.get("stock", 99)) != 99 or rules.get("logistics") != "ChinaPost":
        raise ValueError("unsupported_production_route")
    profit = review["result"]["evidence"]["profit"]
    source = review["result"]["evidence"]["source"]
    candidate = review["candidate"] | {"price": profit["sell_price_cny"]}
    context = {
        "owner": owner,
        "candidate": candidate,
        "match": review["result"],
        "rules": rules,
        "store": {"id": target, "config": cfg, "credentials": keys},
        "idempotency_key": record["offer_id"] if record else "pending",
    }
    # Dossier completeness is checked locally; absent facts do not trigger ERP calls here.
    async with official_api.client(db, keys) as api:
        publisher = LocalPublisher(context, db, api)
        if record:
            publisher.c = publisher.c | {"prepared": record["prepared"]}
        mapping_issues = await hydrate_local_dossier(publisher, db, context) if not record else []
        item, missing = dossier(publisher.c)
        missing.extend(mapping_issues)
        if not record and missing:
            return {
                "phase": "awaiting_dossier", "needs_dossier": True,
                "verified": False, "reason": "official_dossier_incomplete",
                "missing_fields": missing, "backend": "official",
            }
        if not record:
            legacy.approved(review, rules, allow_unknown=allow_unknown)
            dimensions = {
                "package_length": item["depth"],
                "package_width": item["width"],
                "package_height": item["height"],
                "package_weight": item["weight"],
            }
            if not legacy.same_postal_package(dimensions, profit["input"]):
                raise ValueError("draft_profit_package_mismatch")
            legacy.require_source_modes(
                (), candidate["origin"].get("plugin_detail", {}).get("monthly_sales", {}), allow_unknown
            )
            if route and route["expires"] <= time.time():
                raise ValueError("hour_window_closed")
        directory = db.directory
        journal = legacy.TestListingJournal(directory / "plugin-production.sqlite3")
        ownership = legacy.ProductionOwnership(
            journal,
            legacy.ROOT / "maozi_direct_new_method/state/global-sku-claims",
            "flowhub-plugin-production-v1",
        )

        def save():
            with db.connect() as c:
                c.execute(
                    "INSERT OR REPLACE INTO plugin_publications VALUES(?,?,?,?,?)",
                    (*key, json.dumps(record), time.time()),
                )

        async def guard(plan, product, *, writing=False):
            if writing:
                if record.get("write_deadline") and time.time() >= record["write_deadline"]:
                    raise ValueError("hour_window_closed")
                legacy.approved(review, rules, allow_unknown=allow_unknown)
                if fingerprint(latest) != fingerprint(review):
                    raise ValueError("official_review_changed")
                if (str(keys["client_id"]), str(cfg["warehouse_id"]), str(cfg["shop_id"])) != tuple(
                    record["account_binding"]
                ):
                    raise ValueError("official_account_binding_changed")
            blocks = await legacy.read_delists()
            offers = {tuple(v) for v in blocks["offers"]}
            if (
                sku in blocks["skus"]
                or product.sku in blocks["skus"]
                or (plan.shop_id, product.offer_id) in offers
                or ("*", product.offer_id) in offers
            ):
                raise ValueError("explicit_delist")
            if not ownership.owns(plan.shop_id, product.offer_id, sku):
                raise ValueError("ownership_changed")
            with db.connect() as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='product_listing_controls'").fetchone():
                    removal = c.execute(
                        "SELECT 1 FROM product_listing_controls WHERE owner=? AND sku=? AND seller=? AND action='unlist' AND state!='no_listing'",
                        key,
                    ).fetchone()
                    if removal:
                        raise ValueError("explicit_delist")
                if c.execute("SELECT 1 FROM blocks WHERE owner=? AND source_key=?", (owner, sku)).fetchone():
                    raise ValueError("product_blocked")
                current = c.execute(
                    "SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?", key
                ).fetchone()
            if not current:
                raise ValueError("current_source_packet_missing")
            require_unchanged_source(review, json.loads(current[0]))

        async def write_guard(plan, product):
            await guard(plan, product, writing=True)

        if not record:
            plan = legacy.ZeroStockListingPlan(
                shop_id=str(cfg["shop_id"]),
                sku=sku,
                title=candidate["title"],
                cover_image=candidate["image"],
                sell_price_cny=str(profit["sell_price_cny"]),
                watermark_id=str(cfg.get("watermark_id", "")),
                warehouse_id=str(cfg["warehouse_id"]),
                strategy_version="flowhub-official-v1",
                evidence_report="plugin_reviews:" + fingerprint(review),
                category_id=str(candidate["origin"]["category_id"]),
                purchase_price_cny=str(source["selected_cost_cny"]),
                stock_target=99,
                purpose="production",
                supplier_identity=str(source["selected_offer_id"]),
            )
            offer = journal.offer_id_for(plan)
            publisher.c = publisher.c | {"idempotency_key": offer}
            # No writes or ownership acquisition until official schema validation passes.
            prepared = await publisher.invoke("prepare")
            if not prepared.get("ready"):
                return {
                    "phase": "awaiting_dossier", "needs_dossier": True,
                    "verified": False,
                    "reason": "official_dossier_invalid",
                    "missing_fields": prepared.get("missing", []),
                    "backend": "official",
                }
            journal.prepare(plan)
            # Pin first: a crash must never send the same intent to the ERP fallback.
            record = {
                "backend": "official",
                "source_dedupe": "local_official_v1",
                "owner": owner,
                "sku": sku,
                "seller": seller,
                "store_id": target,
                "store_name": store["name"],
                "review": review,
                "prepared": prepared,
                "plan": plan.to_dict(),
                "offer_id": offer,
                "started_at": time.time(),
                "events": [],
                "phase": "prepared",
                "account_binding": [str(keys["client_id"]), str(cfg["warehouse_id"]), str(cfg["shop_id"])],
            }
            if route:
                record.update(
                    authorized_store_id=target, write_deadline=route["expires"], run_id=route["run_id"]
                )
            save()
            publisher.c = publisher.c | {"prepared": prepared}
        plan = legacy.ZeroStockListingPlan(**record["plan"])
        offer = record["offer_id"]
        if (str(keys["client_id"]), str(cfg["warehouse_id"]), str(cfg["shop_id"])) != tuple(
            record["account_binding"]
        ):
            raise ValueError("official_account_binding_changed")
        if not ownership.acquire(plan.shop_id, offer, sku):
            raise ValueError("source_claim_conflict")

        class Port(ScheduledInventoryAdapter):
            async def favorite_id(self, source_sku):
                if source_sku != sku:
                    raise ValueError("source_identity_changed")
                return "local-dossier:" + record["prepared"]["digest"]

            async def publish_zero(self, passed, offer_id, favorite_id):
                if passed != plan or offer_id != offer or journal.read(offer)["phase"] != "submitting":
                    raise ValueError("official_intent_mismatch")
                try:
                    await write_guard(plan, ListedProduct("", "", plan.shop_id, offer, "", ""))
                    from .official_source_guard import verify_local_official, verify_unimported

                    if record.get('source_dedupe') == 'local_official_v1':
                        await verify_local_official(db, owner, sku, seller, offer, api)
                    else:
                        await verify_unimported(db, owner, sku, keys, seller=seller)
                    legacy.require_source_modes(
                        (),
                        candidate["origin"].get("plugin_detail", {}).get("monthly_sales", {}),
                        allow_unknown,
                    )
                    q = await official_api.capacity(api)
                    observe(db, owner, target, q)
                    legacy.require_quota(q)
                except Exception as error:
                    raise RequestNotSent("official preflight blocked") from error
                try:
                    result = await publisher.invoke("publish")
                except (official_api.OfficialDeferred, httpx.HTTPStatusError) as error:
                    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code not in (
                        401,
                        403,
                        429,
                    ):
                        raise
                    # These exact responses prove non-acceptance, unlike timeout/5xx.
                    with db.connect() as c:
                        c.execute(
                            "DELETE FROM ozon_direct_writes WHERE offer_id=? AND task_id IS NULL", (offer,)
                        )
                    raise RequestNotSent("official request not accepted") from error
                if result.get("not_sent"):
                    raise RequestNotSent("official capacity exhausted")
                # Unknown means reconciliation only; no ERP fallback or replay.

            async def import_status(self, passed, offer_id):
                with db.connect() as c:
                    write = c.execute(
                        "SELECT task_id FROM ozon_direct_writes WHERE offer_id=?", (offer_id,)
                    ).fetchone()
                if not write or not write[0]:
                    return "pending"
                response = await publisher.seller("/v1/product/import/info", {"task_id": int(write[0])})
                items = response.get("result", {}).get("items")
                if (
                    not isinstance(items, list)
                    or len(items) > 1
                    or any(p.get("offer_id") != offer for p in items)
                ):
                    raise ValueError("official_import_identity_mismatch")
                if any(p.get("errors") or p.get("status") == "failed" for p in items):
                    return "failed"
                return "pending"  # Official visibility requires no ERP catalogue sync.

            async def sync_products(self, shop_id):
                raise ValueError("official_publication_must_not_sync_erp")

        port = Port(
            api,
            shop_id=plan.shop_id,
            warehouse_id=plan.warehouse_id,
            journal=journal,
            stock_guard=write_guard,
        )
        identity = await port.verify_identity()
        if "嘉兴邮政" not in str(identity.get("name") or ""):
            raise ValueError("postal_warehouse_unavailable")

        async def preflight(p, o):
            await write_guard(p, ListedProduct("", "", p.shop_id, o, "", ""))

        service = legacy.ProductionListingService(port, journal, preflight, stock_guard=guard)
        before = journal.read(offer)["phase"]
        try:
            if before in ("manual_review", "stock_verified"):
                # Late visibility may confirm an existing write, never authorize another.
                product = await port.find_product(plan.shop_id, offer)
                stocks = await port.read_stocks(product) if product and product.sku != "0" else ()
                if product:
                    await guard(plan, product)
                    if (
                        not product.issue_codes
                        and product.status == "selling"
                        and any(
                            s.warehouse_id == plan.warehouse_id and s.present == 99 and s.reserved == 0
                            for s in stocks
                        )
                    ):
                        journal.move(
                            offer,
                            before,
                            "stock_verified",
                            product=asdict(product),
                            warehouses=[asdict(s) for s in stocks],
                            reason=None,
                        )
                    elif before == "stock_verified":
                        journal.move(offer, before, "manual_review", reason="selling_observation_changed")
                elif before == "stock_verified":
                    journal.move(offer, before, "manual_review", reason="selling_observation_missing")
            else:
                await service.advance(offer)
        except RequestNotSent as error:
            cause = error.__cause__
            if isinstance(cause, (ValueError, official_api.OfficialDeferred)):
                raise cause
            raise
        finally:
            final = journal.read(offer)
            record["phase"] = final["phase"]
            record["events"] = (
                record.get("events", []) + [{"at": time.time(), "from": before, "to": final["phase"]}]
            )[-100:]
            save()
        record["verified"] = final["phase"] == "stock_verified"
        if record["verified"]:
            record.setdefault("first_verified_at", time.time())
        record["product"] = final["details"].get("product")
        record["stocks"] = final["details"].get("warehouses", [])
        save()
        with db.connect() as c:
            row = c.execute(
                "SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?", key
            ).fetchone()
            p = json.loads(row[0])
            p.setdefault("listing_review", {}).update(
                observed_at=time.time(),
                label="官方直连已回查可售，库存99" if record["verified"] else "官方直连：" + final["phase"],
                publication_ready=True,
                publication={
                    "shop_id": plan.shop_id,
                    "offer_id": offer,
                    "state": final["phase"],
                    "product": record["product"],
                    "stocks": record["stocks"],
                    "backend": "official",
                },
            )
            legacy.SourceLibrary(db).put(
                owner, p, {"channel": "official-production", "offer_id": offer}, connection=c
            )
            if record["verified"]:
                modules = {
                    k: dict(c.execute("SELECT * FROM modules WHERE id=?", (v,)).fetchone())
                    for k, v in json.loads(wf["modules"]).items()
                }
                c.execute(
                    """INSERT OR IGNORE INTO jobs(id,owner,source_key,store_id,phase,data,modules,next_at,created,updated,note)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        offer,
                        owner,
                        sku,
                        target,
                        "selling",
                        json.dumps(
                            {
                                "candidate": candidate,
                                "match": review["result"],
                                "external_publication": record,
                                "product_id": record["product"]["product_id"],
                            }
                        ),
                        json.dumps(modules),
                        0,
                        record["started_at"],
                        time.time(),
                        "官方直连已回查可售，库存99",
                    ),
                )
        return {
            "sku": sku,
            "offer_id": offer,
            "phase": final["phase"],
            "verified": record["verified"],
            "product": record["product"],
            "backend": "official",
            "reason": final["details"].get("reason"),
            "retryable_readback": final["phase"] == "manual_review"
            and final["details"].get("reason") == "reconciliation_timeout",
        }
