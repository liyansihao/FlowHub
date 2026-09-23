import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from flowhub_control.storage import Store
from flowhub_control.reader import Reader
from flowhub_control.controller import Controller, COMMIT
from flowhub_control.agent import tick


class FakeService:
    def __init__(self): self.is_loaded=True; self.starts=0; self.valid=True
    def loaded(self): return self.is_loaded
    def validate(self):
        if not self.valid: raise ValueError('production_source_drift')
    def start(self): self.starts+=1; self.is_loaded=True


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir()
        self.now=100000.;self.store=Store(self.root/'agent.sqlite3');self.service=FakeService()
        with sqlite3.connect(self.data/'flowhub.sqlite3') as c:
            c.executescript('''
             CREATE TABLE pipeline_module_control(module TEXT PRIMARY KEY,paused INTEGER,updated REAL);
             CREATE TABLE plugin_pipeline(owner TEXT,sku TEXT,seller TEXT,state TEXT,body TEXT,due REAL,attempts INTEGER);
             CREATE TABLE plugin_publications(owner TEXT,sku TEXT,seller TEXT,body TEXT,updated REAL);
             CREATE TABLE pipeline_module_events(id INTEGER PRIMARY KEY,module TEXT,owner TEXT,sku TEXT,started REAL,finished REAL,outcome TEXT,details TEXT);
             CREATE INDEX pipeline_module_events_time ON pipeline_module_events(finished);
             CREATE TABLE plugin_pipeline_leases(owner TEXT,sku TEXT,seller TEXT,expires REAL);
             CREATE TABLE sourcing_tasks(id TEXT,kind TEXT,state TEXT,last_at REAL,failures INTEGER,successes INTEGER,due REAL,lease_until REAL);
             CREATE TABLE acquisition_tasks(lease_until REAL);
             CREATE TABLE product_listing_controls(state TEXT);
             CREATE TABLE health(name TEXT,pid INTEGER,heartbeat REAL);
             CREATE TABLE pipeline_campaigns(owner TEXT,enabled INTEGER);
             INSERT INTO pipeline_campaigns VALUES('owner',1);
            ''')
        self.reader=Reader(self.data,self.store,clock=lambda:self.now,page_size=2)
        self.reader.health=lambda:{'instance_id':'instance','pid_alive':True,'heartbeat_fresh':True,'source_revision':COMMIT,'controls':self.reader.controls(),'state':'running','observed_at':self.now}
        self.controller=Controller(self.reader,self.store,self.service,enabled=True,clock=lambda:self.now,drain_seconds=10)

    def tearDown(self): self.tmp.cleanup()
    def sql(self,sql,args=()):
        with sqlite3.connect(self.data/'flowhub.sqlite3') as c:c.execute(sql,args)
    def command(self,action='pause',identity='1'):
        return dict(id=identity,action=action,expected_state_version=self.controller.status()['state_version'],target_revision=COMMIT,expires_at=self.now+60,idempotency_key=identity,actor='anonymous')
    def pub(self,sku='s',seller='a',verified=True,stamps=(100,200)):
        self.sql('INSERT INTO plugin_publications VALUES(?,?,?,?,?)',('o',sku,seller,json.dumps(dict(backend='maozi_follow',verified=verified,offer_id='offer',started_at=50,events=[{'to':'stock_verified','at':t} for t in stamps])),self.now))

    def test_outbox_fairness_and_uses_available_capacity(self):
        for i in range(1200):self.store.save('event',str(i),{'id':i})
        for i in range(800):self.store.save('product',str(i),{'sku':str(i)})
        self.store.save('success','one',{'verified':True})
        batch=self.store.batch()
        self.assertEqual(len(batch),1000)
        self.assertEqual(len({(r['kind'],r['key']) for r in batch}),1000)
        self.assertTrue(any(r['kind']=='success' for r in batch))
        self.assertGreater(sum(r['kind']=='product' for r in batch),150)

    def test_reads_cannot_write(self):
        with self.reader.connection() as c:
            with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM health')
    def test_first_verified_dedup_and_persist_after_unlist(self):
        self.pub();self.reader.project();self.reader.project()
        self.assertEqual(len(self.store.items('success')),1);self.assertEqual(self.store.items('success')[0]['first_verified_at'],100)
        self.sql("UPDATE plugin_publications SET body=json_set(body,'$.verified',0)");self.reader.project()
        self.assertEqual(len(self.store.items('success')),1)
    def test_unverified_and_updated_are_not_new_success(self):
        self.pub(verified=False);self.reader.project();self.assertEqual(self.store.items('success'),[])
    def test_same_sku_multiple_shops_separate(self):
        self.pub(seller='a');self.pub(seller='b');self.reader.project();self.assertEqual(len(self.store.items('success')),2)
    def test_paged_projection_marks_complete_only_after_end(self):
        for i in range(3):self.pub(sku=str(i))
        self.reader.project();self.assertIsNone(self.store.get('plugin_publications:covered_at'))
        self.reader.project();self.assertIsNotNone(self.store.get('plugin_publications:covered_at'))
        self.assertEqual(len(self.store.items('publication')),3)
    def test_error_details_never_exported(self):
        self.sql('INSERT INTO pipeline_module_events VALUES(1,?,?,?,?,?,?,?)',('submit','o','s',1,self.now,'error','token=secret123 database is locked'))
        self.reader.project();items=self.store.items('event');self.assertEqual(items[0]['error_class'],'sqlite_lock');self.assertNotIn('secret123',json.dumps(items))
    def test_partial_log_resumes_and_redacts(self):
        p=self.data/'supervisor.log';p.write_text('Error token=SECRET')
        self.reader.logs();self.assertEqual(self.store.items('log'),[])
        with p.open('a') as f:f.write('\n')
        self.reader.logs();self.assertEqual(len(self.store.items('log')),1);self.assertNotIn('SECRET',json.dumps(self.store.items('log')))
    def test_pause_changes_only_seed_preserves_queue(self):
        self.sql("INSERT INTO pipeline_module_control VALUES('publication',0,2)")
        r=self.controller.execute(self.command());self.assertEqual(r['status'],'applied');self.assertTrue(self.reader.controls()['seed']['paused']);self.assertFalse(self.reader.controls()['publication']['paused'])
    def test_duplicate_pause_replays_after_state_change(self):
        cmd=self.command();a=self.controller.execute(cmd);self.now+=1;b=self.controller.execute(cmd);self.assertEqual(a,b)
    def test_id_reuse_with_different_payload_rejected(self):
        cmd=self.command();self.controller.execute(cmd)
        with self.assertRaises(ValueError): self.controller.execute({**cmd,'action':'resume'})
    def test_external_pause_cannot_be_resumed(self):
        self.sql("INSERT INTO pipeline_module_control VALUES('seed',1,2)")
        r=self.controller.execute(self.command('resume'));self.assertEqual(r['status'],'needs_attention');self.assertTrue(self.reader.controls()['seed']['paused'])
    def test_resume_owned_pause(self):
        self.controller.execute(self.command());self.now+=1
        r=self.controller.execute(self.command('resume','2'));self.assertEqual(r['status'],'applied');self.assertFalse(self.reader.controls()['seed']['paused'])
    def test_local_edit_conflict_preserved(self):
        self.controller.execute(self.command());self.sql("UPDATE pipeline_module_control SET updated=88")
        r=self.controller.execute(self.command('resume','2'));self.assertEqual(r['status'],'needs_attention')
    def test_expired_start_never_runs(self):
        cmd=self.command('start');self.now+=61;self.assertEqual(self.controller.execute(cmd)['status'],'expired');self.assertEqual(self.service.starts,0)
    def test_state_version_conflict(self):
        cmd=self.command();self.sql("INSERT INTO pipeline_module_control VALUES('review',1,2)")
        self.assertEqual(self.controller.execute(cmd)['status'],'rejected')
    def test_code_drift_blocks(self):
        self.service.valid=False;self.assertEqual(self.controller.execute(self.command())['status'],'needs_attention')
    def test_drain_timeout_never_kills_worker(self):
        cmd=self.command('stop');a=self.controller.execute(cmd);self.assertEqual(a['status'],'draining');self.now+=11
        b=self.controller.execute(cmd);self.assertEqual(b['status'],'needs_attention');self.assertTrue(self.service.loaded());self.assertEqual(self.service.starts,0)
    def test_disabled_commands(self):
        self.controller.enabled=False;self.assertEqual(self.controller.execute(self.command())['status'],'rejected');self.assertEqual(self.reader.controls(),{})
    def test_failed_upload_keeps_outbox(self):
        self.pub()
        def unavailable(*args):raise OSError('network')
        with self.assertRaises(OSError):tick({'url':'https://example.org','token':'secret','deployment':'d'},self.reader,self.controller,self.store,unavailable)
        self.assertTrue(self.store.batch())
    def test_ack_old_version_keeps_new_value_dirty(self):
        self.store.save('product','k',{'state':'old'});batch=self.store.batch();self.store.save('product','k',{'state':'new'});self.store.ack(batch);self.assertEqual(len(self.store.batch()),1)
    def test_busy_write_bounded_and_preserves_records(self):
        other=sqlite3.connect(self.data/'flowhub.sqlite3');other.execute('BEGIN IMMEDIATE')
        try:self.assertEqual(self.controller.execute(self.command())['status'],'needs_attention')
        finally:other.rollback();other.close()
        self.assertEqual(self.reader.controls(),{})

if __name__=='__main__':unittest.main()
