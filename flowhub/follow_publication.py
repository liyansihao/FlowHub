"""ERP follow import: approved local inputs, no acquisition or official dossier gate.

The switch applies only before a publication intent exists. Historical records
keep their backend, offer and journal; disabling the switch cannot reroute them.
"""
import hashlib
import json
import math
import time

BACKEND = 'maozi_follow'
DOSSIER_MODES = ('optional', 'required')


def enabled(db):
    path = db.directory / 'publication-policy.json'
    if not path.exists():
        return False
    policy = json.loads(path.read_text())
    if policy.get('backend') not in (BACKEND, 'existing'):
        raise ValueError('invalid_publication_policy')
    return policy['backend'] == BACKEND


def dossier_mode(db):
    """Return whether the full product dossier is required for new follow jobs.

    Native ERP follow imports only need the reviewed identity and package facts.
    Keep the complete dossier available as an explicit opt-in for operators that
    want the stricter legacy gate, without changing historical publications.
    """
    path = db.directory / 'publication-policy.json'
    if not path.exists():
        return 'required'
    policy = json.loads(path.read_text())
    mode = policy.get('dossier_mode')
    if mode is None:
        return 'optional' if policy.get('backend') == BACKEND else 'required'
    if mode not in DOSSIER_MODES:
        raise ValueError('invalid_dossier_mode')
    return mode


def full_dossier_required(db, key=None):
    """Whether a new publication must pass the complete dossier gate.

    ``key`` is accepted so callers can keep a stable policy-call shape while
    historical records remain routed by their existing backend and intent.
    """
    del key
    return dossier_mode(db) == 'required'


def selected(db, key):
    with db.connect() as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_publications'").fetchone()
        prior = c.execute('SELECT body FROM plugin_publications WHERE owner=? AND sku=? AND seller=?', key).fetchone() if exists else None
    if prior:
        return json.loads(prior[0]).get('backend') == BACKEND
    return enabled(db)


def local_inputs(review):
    """Reuse the approved costing measurements, without asserting ERP acquisition."""
    candidate = review['candidate']
    facts = review['result']['evidence']['profit']['input']
    values = [float(facts[k]) for k in ('package_length', 'package_width', 'package_height', 'package_weight')]
    if not all(math.isfinite(v) and v > 0 for v in values):
        raise ValueError('pricing_facts_missing')
    if not candidate.get('title') or not candidate.get('image'):
        raise ValueError('follow_identity_missing')
    return {'source_key': candidate['source_key'], 'source': 'approved-valuation-inputs',
            'observed_at': review['finished_at'], 'detail': {
                'title': candidate['title'], 'package_length': values[0] * 10,
                'package_width': values[1] * 10, 'package_height': values[2] * 10,
                'package_weight': values[3]}}


def refresh_review(record, latest):
    old = record['review']
    for r in (old, latest):
        local_inputs(r)
    before, after = old['candidate'], latest['candidate']
    if any(before.get(k) != after.get(k) for k in ('source_key', 'title', 'image')) or any(
        before['origin'].get(k) != after['origin'].get(k) for k in ('seller_id', 'category_id')
    ):
        raise ValueError('source_identity_changed')
    if (old['result']['supplier_id'], old['result']['purchase'], old['result']['evidence']['profit']['sell_price_cny'],
        old['result']['evidence']['profit']['input']) != (
        latest['result']['supplier_id'], latest['result']['purchase'], latest['result']['evidence']['profit']['sell_price_cny'],
        latest['result']['evidence']['profit']['input']):
        raise ValueError('prepared_plan_repricing_required')
    record.setdefault('review_revisions', []).append({'at': time.time(), 'previous_review': old,
                                                    'reason': 'same_approved_follow_inputs'})
    record['review'] = latest


DOSSIER_FIELDS = ('official_dossier_pending', 'repair_full_dossier', 'pending_publication_fields',
                  'needs_dossier', 'missing_fields', 'dossier_gate_hash', 'repair_workflow',
                  'repair_retry', 'repair_reason', 'repair_dependency', 'repair_manual')


def clear_dossier(body):
    prior = {k: body.pop(k) for k in DOSSIER_FIELDS if k in body}
    if prior:
        body.setdefault('publication_migrations', []).append({'at': time.time(), 'backend': BACKEND, 'previous': prior})
    if body.get('phase') == 'awaiting_dossier':
        body.pop('phase')
    body.pop('error', None)
    body.pop('reason', None)


def release_repair(db, key, body):
    """Return publication-only repair to unchanged evaluation, never unpark review."""
    if not selected(db, key):
        return False
    if not (body.get('official_dossier_pending') or body.get('repair_full_dossier') or
            body.get('pending_publication_fields') or body.get('phase') == 'awaiting_dossier' or
            (body.get('repair_workflow') or {}).get('kind') == 'publication'):
        return False
    clear_dossier(body)
    # Review and pricing remain responsible for approval; no stale approval is renewed here.
    body['publication_recheck'] = True
    return True


def legacy_source_hold(db, owner, sku, erp_token):
    """Unknown historical source writes are isolated, never rewritten by the new lane."""
    with db.connect() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_bindings'").fetchone():
            for raw, in c.execute('SELECT t.body FROM acquisition_tasks t JOIN acquisition_bindings b ON b.task_key=t.key WHERE b.owner=? AND b.sku=?', (owner, sku)):
                w = db.open(raw)
                if w.get('unknown_favorite') or w.get('unknown_draft'):
                    return 'historical_source_write_unconfirmed'
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='source_details'").fetchone():
            key=hashlib.sha256((owner+':'+hashlib.sha256(erp_token.encode()).hexdigest()+':'+sku).encode()).hexdigest()
            row=c.execute('SELECT state,body FROM source_details WHERE key=?',(key,)).fetchone()
            if row and row['state'] not in ('ready','draft_ready'):
                data=db.open(row['body'])
                if row['state'] in ('claimed','favorite_started','draft_started') or data.get('favorite_attempted'):
                    return 'historical_source_write_unconfirmed'
    return None


async def feedback(root, product, source):
    import asyncio
    import os
    from pathlib import Path
    process = await asyncio.create_subprocess_exec(
        'node', str(Path(__file__).resolve().parents[1]/'bridges/follow-feedback.mjs'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=os.environ | {'FLOWEF_LEGACY_ROOT': str(root)})
    try:
        output, _ = await asyncio.wait_for(process.communicate(json.dumps({'product':product,'source':source}).encode()), 30)
        result=json.loads(output)
        if process.returncode or not result.get('ok'):
            raise ValueError('follow_feedback_unavailable')
        return result['result']
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
