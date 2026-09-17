import json
from flowhub.pipeline_modules.repair_cleanup import cleanup
from flowhub.pipeline_modules.control import set_paused
from tests.test_continuous_admission import setup


def insert(db,owner,sku='repair',state='needs_fields',attempts=3,first=1000):
    body={'offer_id':'keep-original','submitted':True,'repair_retry':{'attempts':attempts,'first_attempt_at':first},'price':123}
    with db.connect() as c:
        c.execute('INSERT INTO plugin_pipeline VALUES(?,?,?,?,?,?,?)',(owner,sku,'3',state,json.dumps(body),900,9))
    return body


def test_cleanup_is_idempotent_and_preserves_restore_evidence(tmp_path):
    db,owner=setup(tmp_path);body=insert(db,owner)
    assert cleanup(db,now=1200)['parked']==1
    assert cleanup(db,now=1201)['parked']==0
    with db.connect() as c:
        r=c.execute('SELECT * FROM repair_cleanup_receipts').fetchone()
        assert json.loads(r['previous_body'])==body
        q=c.execute("SELECT * FROM plugin_pipeline WHERE sku='repair'").fetchone();b=json.loads(q['body'])
        assert q['state']=='needs_review' and q['attempts']==9
        assert b['offer_id']=='keep-original' and b['price']==123 and b['submitted'] is True


def test_cleanup_leaves_live_lease_fresh_tasks_and_publication_states(tmp_path):
    db,owner=setup(tmp_path)
    insert(db,owner,'leased');insert(db,owner,'fresh',attempts=1)
    for state in ('selling','awaiting_remote','publishing'):insert(db,owner,state,state=state)
    with db.connect() as c:c.execute('INSERT INTO plugin_pipeline_leases VALUES(?,?,?,?,?)',(owner,'leased','3','token',2000))
    assert cleanup(db,now=1200)['parked']==0


def test_timeout_and_pause(tmp_path):
    db,owner=setup(tmp_path);insert(db,owner,attempts=1)
    set_paused(db,'seed',True);assert cleanup(db,now=3000)['state']=='paused'
    set_paused(db,'seed',False)
    assert cleanup(db,now=3000)['tasks'][0]['reason']=='repair_wait_over_30_minutes'


def test_capacity_relief_preserves_untried_candidates(tmp_path):
    db,owner=setup(tmp_path)
    for i in range(36):insert(db,owner,str(i),attempts=2 if i==0 else 0)
    result=cleanup(db,now=1700)
    assert result['parked']==1 and result['tasks'][0]['reason']=='repair_capacity_pressure'


def test_never_started_repair_times_out_from_queue_entry_not_creation(tmp_path):
    db,owner=setup(tmp_path);insert(db,owner,attempts=0)
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET body=json_set(body,'$.requested_at',1,'$.repair_wait_started_at',2000)")
    assert cleanup(db,now=2100)['parked']==0
    result=cleanup(db,now=3800)
    assert result['parked']==1
    assert result['tasks'][0]['reason']=='repair_queue_wait_over_30_minutes'


def test_legacy_queue_without_failed_attempts_uses_last_transition(tmp_path):
    db,owner=setup(tmp_path);insert(db,owner,attempts=0)
    with db.connect() as c:c.execute("UPDATE plugin_pipeline SET body=json_set(body,'$.updated_at',1000)")
    assert cleanup(db,now=2800)['parked']==1
