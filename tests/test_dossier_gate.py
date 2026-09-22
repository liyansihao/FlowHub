import json
import time

from flowhub.dossier_gate import record, valid
from tests.test_maozi_field_repair import setup


def test_certificate_is_bound_to_review_source_and_store(tmp_path):
    db, owner = setup(tmp_path)
    key = (owner, "1", "2")
    with db.connect() as c:
        c.execute(
            "INSERT INTO plugin_reviews VALUES(?,?,?,?)",
            (*key, json.dumps({"state": "matched", "decision": "original"})),
        )
        digest = record(c, key)
        assert digest and valid(c, key)
        # UI bookkeeping does not invalidate actual evidence.
        c.execute(
            "UPDATE sourcing_products SET body=json_set(body,'$.listing_review.pipeline.state','queued')"
        )
        assert valid(c, key)
        c.execute("UPDATE sourcing_products SET body=json_set(body,'$.plugin_detail.weight_g',999)")
        assert not valid(c, key)
        record(c, key)
        c.execute("UPDATE stores SET config='{" + '"warehouse_id":"different"' + "}'")
        assert not valid(c, key)
        record(c, key)
        c.execute("UPDATE plugin_reviews SET body=json_set(body,'$.decision','changed')")
        assert not valid(c, key)


def test_manual_review_cannot_receive_gate_certificate(tmp_path):
    db, owner = setup(tmp_path)
    with db.connect() as c:
        c.execute("INSERT INTO plugin_reviews VALUES(?,?,?,?)", (owner, "1", "2", '{"state":"needs_review"}'))
        assert record(c, (owner, "1", "2")) is None
        assert not valid(c, (owner, "1", "2"))


def test_gate_expires_without_changing_original_facts(tmp_path):
    db, owner = setup(tmp_path)
    key = (owner, "1", "2")
    with db.connect() as c:
        c.execute("INSERT INTO plugin_reviews VALUES(?,?,?,?)", (*key, '{"state":"matched"}'))
        record(c, key)
        assert not valid(c, key, now=time.time() + 901)


async def test_pipeline_mints_certificate_after_review_synchronization(tmp_path, monkeypatch):
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.repair import PriceRepairModule
    from flowhub.source_library import SourceLibrary
    from tests.test_repaired_review_sync import fixture

    db, owner = setup(tmp_path)
    pipeline.schema(db)
    review, p = fixture()
    now = time.time()
    review["finished_at"] = now
    p["collected_at"] = now
    p["plugin_detail"]["observed_at"] = now
    p["plugin_detail"]["monthly_sales"]["observed_at"] = now
    review["candidate"]["origin"]["price_evidence"]["observed_at"] = now
    SourceLibrary(db).put(owner, p, {"channel": "test"})
    with db.connect() as c:
        c.execute("INSERT INTO plugin_reviews VALUES(?,?,?,?)", (owner, "1", "2", json.dumps(review)))
        c.execute(
            "INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)",
            (owner, "1", "2", "needs_fields", '{"official_dossier_pending":true}', 0, 0),
        )

    async def repaired(*args):
        return {"state": "ready", "reason": "complete_dossier", "workflow": {"kind": "publication"}}

    monkeypatch.setattr(PriceRepairModule, "run", repaired)
    assert await pipeline.tick(db, lane="seed_repair")
    with db.connect() as c:
        row = c.execute("SELECT state,body FROM plugin_pipeline").fetchone()
        assert row["state"] == "publishing"
        assert json.loads(row["body"])["dossier_gate_hash"]
        assert valid(c, (owner, "1", "2"))
        saved = json.loads(c.execute("SELECT body FROM plugin_reviews").fetchone()[0])
        assert saved["finished_at"] == now and saved["result"] == review["result"]
