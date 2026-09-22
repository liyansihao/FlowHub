"""Versioned pre-publication evidence certificate; never authorizes a write itself."""

import json
import time

from .source_library import fingerprint


def schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS dossier_gate_certificates(
      hash TEXT PRIMARY KEY,owner TEXT,sku TEXT,seller TEXT,body TEXT,created REAL)""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS dossier_gate_identity ON dossier_gate_certificates(owner,sku,seller,created)"
    )


def inputs(c, key):
    review = c.execute("SELECT body FROM plugin_reviews WHERE owner=? AND sku=? AND seller=?", key).fetchone()
    product = c.execute(
        "SELECT body FROM sourcing_products WHERE owner=? AND sku=? AND seller=?", key
    ).fetchone()
    route = c.execute(
        "SELECT store_id FROM plugin_routes WHERE owner=? AND sku=? AND seller=?", key
    ).fetchone()
    if not all((review, product, route)):
        return None
    store = c.execute(
        "SELECT config,secret FROM stores WHERE owner=? AND id=?", (key[0], route[0])
    ).fetchone()
    if not store:
        return None
    r = json.loads(review[0])
    p = json.loads(product[0])
    packet = {
        k: p.get(k)
        for k in (
            "sku",
            "seller_id",
            "title",
            "image",
            "url",
            "plugin_detail",
            "ozon_dossier",
            "proposed_sale_price",
        )
    }
    return {
        "review_hash": fingerprint(r),
        "product_hash": fingerprint(packet),
        "store_id": route[0],
        "store_hash": fingerprint([store["config"], store["secret"]]),
        "review_state": r.get("state"),
    }


def record(c, key):
    schema(c)
    value = inputs(c, key)
    if not value or value["review_state"] != "matched":
        return None
    digest = fingerprint(value)
    c.execute(
        "INSERT OR REPLACE INTO dossier_gate_certificates VALUES(?,?,?,?,?,?)",
        (digest, *key, json.dumps(value), time.time()),
    )
    return digest


def valid(c, key, now=None):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='dossier_gate_certificates'").fetchone():
        return False
    value = inputs(c, key)
    if not value:
        return False
    row = c.execute(
        "SELECT created FROM dossier_gate_certificates WHERE hash=? AND owner=? AND sku=? AND seller=?",
        (fingerprint(value), *key),
    ).fetchone()
    return bool(row and 0 <= (time.time() if now is None else now) - row[0] < 900)
