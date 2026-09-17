"""Retain remote source deduplication once before official import.

A local absence is not proof that another publisher has not imported this SKU.
A short, account-bound successful check can be reused across pre-dispatch retries.
Already submitted intents never call this guard during reconciliation.
"""

import hashlib
import time

import httpx


async def verify_unimported(db, owner, sku, keys):
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
    transport = StepTransport(
        MeasuredTransport(bridge), ttl=15, namespace=hashlib.sha256(token.encode()).hexdigest()
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
