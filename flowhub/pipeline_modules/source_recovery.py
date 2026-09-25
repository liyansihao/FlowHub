"""Explicit, bounded recovery of source scans after the browser route is proven.

This module is deliberately not called by the worker. An operator must first
verify the same dedicated profile can read a real product and seller page.
"""

import time


RECOVERABLE_REASONS = (
    'browser_access_challenge',
    'browser_navigation_TimeoutError',
    'browser_navigation_net::ERR_FAILED',
)


def rearm(db, owner, run_id, *, limit=1, seller=None, now=None):
    if not 1 <= limit <= 10:
        raise ValueError('source_recovery_limit_out_of_range')
    if seller is not None and (not str(seller).isdigit() or int(seller) <= 0):
        raise ValueError('invalid_source_recovery_seller')
    now = time.time() if now is None else now
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('''
          SELECT m.seller,m.last_state,s.page,s.next_url,
            (SELECT f.reason FROM browser_source_failures f
             WHERE f.owner=m.owner AND f.run_id=m.run_id AND f.seller=m.seller
             ORDER BY f.at DESC LIMIT 1) reason
          FROM source_loop_stores m
          JOIN browser_source_scans s
            ON s.owner=m.owner AND s.run_id=m.run_id AND s.seller=m.seller
          WHERE m.owner=? AND m.run_id=? AND (? IS NULL OR m.seller=?) AND s.state='blocked'
            AND m.last_state IN ('blocked','retry_exhausted')
          ORDER BY m.updated,m.seller
        ''', (owner, run_id, seller, seller))
        selected = []
        for row in rows:
            if row['reason'] not in RECOVERABLE_REASONS:
                continue
            selected.append(dict(row))
            if len(selected) == limit:
                break
        for row in selected:
            key = (owner, run_id, row['seller'])
            c.execute("UPDATE browser_source_scans SET state='ready',updated=? "
                      "WHERE owner=? AND run_id=? AND seller=? AND state='blocked'",
                      (now, *key))
            c.execute("UPDATE source_loop_stores SET due=?,failures=0,last_state='operator_rearmed',updated=? "
                      "WHERE owner=? AND run_id=? AND seller=?",
                      (now, now, *key))
    return selected
