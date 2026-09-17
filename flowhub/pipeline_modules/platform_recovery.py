"""Resume the original import only after a fresh nonblocking observation and guards."""
from dataclasses import asdict
import time
from flowef.domain.rules.platform_issues import product_issue


async def recover_platform_warning(port, journal, offer, plan, preflight):
    current = journal.read(offer)
    if current['phase'] != 'manual_review' or current['details'].get('reason') != 'platform_issue_requires_review':
        return False
    product = await port.find_product(plan.shop_id, offer)
    if product is None or product.shop_id != plan.shop_id or product.offer_id != offer:
        return False
    if product_issue(product) or product.status not in ('ready_to_sell', 'selling', 'out_of_stock'):
        return False
    # Includes current approval, exact target, source binding and delist exclusions.
    # Never renew an expired approval or replay the original import.
    await preflight(plan, offer)
    return journal.move(offer, 'manual_review', 'reconciling', reason=None,
                        platform_issue_codes=list(product.issue_codes), product=asdict(product),
                        waiting_since=time.time(), platform_warning_recovered_at=time.time())
