"""Local/official dedupe for new intents; retain the old guard for pinned history."""

import hashlib
import time

import httpx


async def verify_local_official(db, owner, sku, seller, offer, api):
    # The caller holds the SKU/store locks and the durable global source claim.
    # Never reinterpret a known old import as a new official publication.
    with db.connect() as c:
        if c.execute("SELECT 1 FROM jobs WHERE source_key=?", (sku,)).fetchone():
            raise ValueError("existing_source_job")
        rows = c.execute("SELECT owner,seller,body FROM plugin_publications WHERE sku=?", (sku,)).fetchall()
        import json

        for row in rows:
            record = json.loads(row['body'])
            if ((row['owner'], row['seller']) != (owner, seller)
                    or record.get('backend') != 'official' or record.get('offer_id') != offer):
                raise ValueError("source_already_claimed")
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='official_source_checks'").fetchone():
            if c.execute("SELECT 1 FROM official_source_checks WHERE sku=? AND imported=1", (sku,)).fetchone():
                raise ValueError("source_already_imported")
    response = await api.post('/v3/product/info/list', json={'offer_id': [offer]})
    response.raise_for_status()
    data = response.json()
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('official_duplicate_check_invalid')
    if items:
        raise ValueError('official_offer_already_exists')


async def verify_unimported(db, owner, sku, keys, *, seller=None):
    from . import plugin_publication as legacy
    from .pipeline_modules.favorite_lookup import PublicationSourceAdapter
    from .pipeline_modules.request_bridge import MeasuredTransport, PublicationBridge
    from .pipeline_modules.transport import StepTransport

    token = keys.get("erp_token")
    if not token:
        raise ValueError("source_duplicate_verification_required")
    account = hashlib.sha256((owner + "\0" + token).encode()).hexdigest()
    with db.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS official_source_checks(
            account TEXT,sku TEXT,checked REAL,imported INTEGER,PRIMARY KEY(account,sku))""")
        row = c.execute(
            "SELECT checked,imported FROM official_source_checks WHERE account=? AND sku=?", (account, sku)
        ).fetchone()
    if row and row["imported"]:
        raise ValueError("source_already_imported")
    if row and 0 <= time.time() - row["checked"] < 15:
        return
    bridge = PublicationBridge(legacy.ROOT, execute=True, token=token)
    if seller is not None:
        from .cluster_routing import route_bridge
        route_bridge(bridge, db.directory, (owner, sku, seller))
    transport = StepTransport(
        MeasuredTransport(bridge), ttl=15, namespace=hashlib.sha256(token.encode()).hexdigest(),
        circuit_namespace=getattr(bridge, 'execution_route', 'local'),
    )
    async with httpx.AsyncClient(base_url="https://api.maozierp.com", transport=transport) as api:
        found = await PublicationSourceAdapter(api).source_has_imports(sku)
    with db.connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO official_source_checks VALUES(?,?,?,?)",
            (account, sku, time.time(), int(found)),
        )
    if found:
        raise ValueError("source_already_imported")
