"""Keep submission priority only while fresh readback is not accumulating."""

def readback_policy(connection,owner,requested,now):
    if not requested:return {'submission_priority':False,'reason':'normal'}
    row=connection.execute("""SELECT count(*),min(due) FROM plugin_pipeline WHERE owner=?
        AND state IN ('publishing','awaiting_remote')
        AND json_extract(body,'$.phase') IN ('submitting','reconciling','sync_pending','stock_ready','stock_pending')""",(owner,)).fetchone()
    waiting=int(row[0]);overdue=max(0,now-row[1]) if row[1] is not None else 0
    pressure=waiting>=6 or overdue>=120
    return {'submission_priority':not pressure,'reason':'readback_pressure' if pressure else 'submission_capacity',
            'waiting':waiting,'oldest_due_lag_seconds':round(overdue,1)}
