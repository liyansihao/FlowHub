"""Track repeated repair cycles across stage changes without extending approval."""
import json


def config(db):
    path = db.directory / 'stability-policy.json'
    return {'enabled': False, 'max_stall_seconds': 7200, 'max_reentries': 3} | (json.loads(path.read_text()) if path.exists() else {})


def track(body, previous, state, now, *, attempted=True):
    intent = str(body.get('listing_control_id') or body.get('requested_at') or 'legacy')
    old = body.get('lifecycle') or {}
    if old.get('intent') != intent:
        old = {'intent': intent, 'observed_since': now, 'first_requested_at': body.get('requested_at', now),
               'last_progress_at': now, 'repair_attempts': 0, 'repair_entries': 0}
    if attempted and previous == 'needs_fields':
        old['repair_attempts'] += 1
    if state == 'needs_fields' and previous != 'needs_fields':
        old['repair_entries'] += 1
    missing = sorted(set(body.get('missing_fields') or body.get('pending_publication_fields') or
                         (body.get('repair_retry') or {}).get('missing_fields') or []))
    before = old.get('missing_fields')
    if before and missing and set(missing) < set(before):
        old['last_progress_at'] = now
    if state == 'selling' or body.get('phase') in ('stock_ready', 'stock_pending', 'stock_verified'):
        old['last_progress_at'] = now
    old['missing_fields'] = missing
    old['last_observed_at'] = now
    body['lifecycle'] = old


def reason(body, now, policy):
    if not policy['enabled']:
        return None
    data = body.get('lifecycle') or {}
    if not data:
        return None
    if data.get('repair_entries', 0) >= policy['max_reentries']:
        return 'repeated_repair_stage_entries'
    if now - data.get('last_progress_at', now) >= policy['max_stall_seconds']:
        return 'repair_lifecycle_no_progress'
    return None
