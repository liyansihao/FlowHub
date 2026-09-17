"""Recover only the pre-publication favorite stage, with bounded acknowledged retries."""
import time


def favorite_wait(record):
    return record['phase']=='favorite_pending' or (
        record['phase']=='manual_review'
        and record['details'].get('reason') in ('reconciliation_timeout','favorite_visibility_exhausted')
        and record['details'].get('unresolved_phase')=='favorite_pending')


async def recover_favorite(port, journal, offer, plan, preflight, events, *, now=None):
    from flowef.application.services.test_listing import _NOT_ACCEPTED
    now=time.time() if now is None else now
    record=journal.read(offer)
    ready=record['phase']=='ready'
    if not ready and not favorite_wait(record):return False
    phase=record['phase'];details=record['details']
    favorite_id=await port.favorite_id(plan.sku)
    if ready and favorite_id is not None:return False
    if ready:
        # A previously confirmed favorite disappeared before any import dispatch.
        # Stop the zero-delay ready loop; only this pre-import stage may recover it.
        if not journal.move(offer,'ready','favorite_pending',waiting_since=now,waiting_reason='favorite_visibility'):
            return True
        phase='favorite_pending'
    if favorite_id is not None:
        journal.move(offer,phase,'ready',favorite_id=favorite_id,waiting_since=None,
                     waiting_reason=None,reason=None,unresolved_phase=None,favorite_recovered_at=now)
        return True
    if details.get('reason')=='favorite_visibility_exhausted':
        # An original offer can become visible after the favorite disappeared.
        # Reconcile that exact identity; never prepare a replacement offer.
        product=await port.find_product(plan.shop_id,offer) if hasattr(port,'find_product') else None
        if product and product.shop_id==plan.shop_id and product.offer_id==offer:
            journal.move(offer,phase,'reconciling',reason=None,unresolved_phase=None,
                         waiting_since=now,favorite_recovery={'classification':'original_offer_visible','checked_at':now})
            return True
        previous=details.get('favorite_recovery') or {}
        journal.move(offer,phase,phase,favorite_recovery={
            'classification':'favorite_and_original_offer_absent','checked_at':now,
            'unchanged_checks':previous.get('unchanged_checks',0)+1,
            'next_action':'observe_only_no_write_budget_reset'})
        return True
    # Older successful prepared -> pending events were saved only after add_favorite
    # returned. Errors/unknown outcomes have no such event and cannot authorize a retry.
    ack=details.get('favorite_acknowledged_at')
    if ready and ack is None and details.get('favorite_id'):
        ack=now-120
    attempts=details.get('favorite_attempts',1)
    if ack is None and 'favorite_attempts' not in details:
        ack=next((e['at'] for e in events if e.get('from')=='prepared'
                  and e.get('to')=='favorite_pending' and not e.get('error')),None)
    if ack is None:
        if now-details.get('waiting_since',now)>=1200:
            journal.move(offer,phase,'manual_review',reason='reconciliation_timeout',unresolved_phase='favorite_pending')
        return True
    if now-max(ack,details.get('favorite_last_attempt_at',0))<120:return True
    if attempts>=3:
        journal.move(offer,phase,'manual_review',reason='favorite_visibility_exhausted',unresolved_phase='favorite_pending')
        return True
    await preflight(plan,offer)
    if await port.source_has_imports(plan.sku):
        journal.move(offer,phase,'manual_review',reason='source_already_imported',unresolved_phase='favorite_pending')
        return True
    # Persist before dispatch. A crash/unknown result clears permission to retry.
    if not journal.move(offer,phase,'favorite_pending',favorite_attempts=attempts+1,
                        favorite_last_attempt_at=now,favorite_acknowledged_at=None,
                        waiting_since=now,waiting_reason='favorite_visibility',reason=None,unresolved_phase=None):
        return True
    try:
        await port.add_favorite(plan)
    except _NOT_ACCEPTED:
        journal.move(offer,'favorite_pending','favorite_pending',favorite_acknowledged_at=ack)
        raise
    journal.move(offer,'favorite_pending','favorite_pending',favorite_acknowledged_at=now)
    favorite_id=await port.favorite_id(plan.sku)
    if favorite_id is not None:
        journal.move(offer,'favorite_pending','ready',favorite_id=favorite_id,waiting_since=None,
                     waiting_reason=None,favorite_recovered_at=now)
    return True
