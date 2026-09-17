import asyncio
import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from flowhub.pipeline_modules.repair_reads import RepairReads


@pytest.fixture(autouse=True)
def clear_reads():
    RepairReads.cache.clear(); RepairReads.inflight.clear(); RepairReads.failures.clear()


@pytest.mark.asyncio
async def test_identical_reads_coalesce_and_exchange_cache_is_account_scoped():
    calls=[]
    async def erp(*args, **kwargs):
        calls.append(args); await asyncio.sleep(.01)
        return {'RUBCNY': {'value': .09}}
    api=SimpleNamespace(keys={'erp_token':'a'},erp=erp)
    a,b=await asyncio.gather(*(RepairReads.get(api,'/api.exchange_rate/index') for _ in range(2)))
    a['RUBCNY']['value']=999
    assert b['RUBCNY']['value']==.09
    assert (await RepairReads.get(api,'/api.exchange_rate/index'))['RUBCNY']['value']==.09
    assert len(calls)==1
    await RepairReads.get(SimpleNamespace(keys={'erp_token':'b'},erp=erp),'/api.exchange_rate/index')
    assert len(calls)==2


@pytest.mark.asyncio
async def test_failed_endpoint_cools_down_without_blocking_other_accounts_or_paths(monkeypatch):
    clock=[100.];monkeypatch.setattr('flowhub.pipeline_modules.repair_reads.time.monotonic',lambda:clock[0])
    calls=[]
    async def erp(*args,**kwargs):
        calls.append(args);raise httpx.ConnectTimeout('unavailable')
    api=SimpleNamespace(keys={'erp_token':'a'},erp=erp)
    for sku in range(4):
        with pytest.raises(httpx.TransportError):await RepairReads.get(api,'/detail',params={'sku':sku})
    assert len(calls)==3
    with pytest.raises(httpx.TransportError):await RepairReads.get(api,'/other')
    with pytest.raises(httpx.TransportError):await RepairReads.get(SimpleNamespace(keys={'erp_token':'b'},erp=erp),'/detail')
    assert len(calls)==5
    clock[0]+=61
    with pytest.raises(httpx.TransportError):await RepairReads.get(api,'/detail')
    assert len(calls)==6


def test_local_snapshot_does_not_reuse_wrong_account_sku_stale_or_unfinished(tmp_path):
    from flowhub.db import Database
    from flowhub.source_detail import SourceCollector
    db=Database(tmp_path)
    context={'owner':'a','store':{'credentials':{'erp_token':'one'}},'candidate':{'source_key':'1'}}
    collector=SourceCollector(db,context)
    packet={'source_key':'1','observed_at':time.time(),'detail':{'skus':[{}]}}
    collector.save('ready',packet)
    assert collector.cached_snapshot()==packet
    other=copy.deepcopy(context);other['store']['credentials']['erp_token']='two'
    assert SourceCollector(db,other).cached_snapshot() is None
    for state,change in [('draft_started',{}),('ready',{'source_key':'2'}),('ready',{'observed_at':time.time()-21601}),('ready',{'detail':{'skus':[{},{}]}})]:
        collector.save(state,packet|change)
        assert collector.cached_snapshot() is None


@pytest.mark.asyncio
async def test_local_completed_draft_precedes_all_remote_reads(tmp_path,monkeypatch):
    from test_maozi_field_repair import setup
    from flowhub.source_detail import SourceCollector
    from flowhub.maozi import MaoziPublisher
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,owner=setup(tmp_path)
    now=time.time()
    with db.connect() as c:
        p=json.loads(c.execute('SELECT body FROM sourcing_products').fetchone()[0])
        p['plugin_detail']={}
        p['proposed_sale_price']={'value':30,'currency':'CNY','observed_at':now}
        c.execute('UPDATE sourcing_products SET body=?',(json.dumps(p),))
    context={'owner':owner,'store':{'credentials':{'erp_token':'test'}},'candidate':{'source_key':'1'}}
    packet={'source_key':'1','observed_at':now,'draft_id':44,'detail':{'skus':[{}],'package_weight':25,'package_length':100,'package_width':80,'package_height':10,'common_attributes':[{'id':1}]}}
    SourceCollector(db,context).save('ready',packet)
    async def unexpected(*args,**kwargs):pytest.fail('completed local draft must avoid network')
    monkeypatch.setattr(MaoziPublisher,'erp',unexpected)
    monkeypatch.setattr(SourceCollector,'collect',unexpected)
    result=await PriceRepairModule().run(db,owner,'1','2',full_dossier=True)
    assert result['state']=='ready' and result['missing_fields']==[]
    with db.connect() as c:
        event=json.loads(c.execute('SELECT body FROM plugin_repair_events').fetchone()[0])
    assert event['steps'][0]['source']=='maozi-erp-draft-cache'
    assert event['steps'][0]['observed_at']==now


@pytest.mark.asyncio
async def test_approved_retry_has_priority_but_due_time_and_lease_still_apply(tmp_path,monkeypatch):
    from test_modular_pipeline import setup
    from flowhub import plugin_pipeline as pipeline
    from flowhub.pipeline_modules.repair import PriceRepairModule
    db,owner=setup(tmp_path,3);seen=[]
    with db.connect() as c:
        c.execute("UPDATE plugin_pipeline SET state='needs_fields',due=0,body='{}'")
        for sku in ('2','3'):
            c.execute('UPDATE plugin_pipeline SET body=?,due=? WHERE sku=?',
                      (json.dumps({'pending_publication_fields':['attributes'],'repair_retry':{'attempts':1}}),time.time()+600 if sku=='3' else 0,sku))
    async def repair(self,db,owner,sku,seller):
        seen.append(sku);return {'state':'waiting','reason':'network unavailable','failure_class':'network','missing_fields':['attributes']}
    monkeypatch.setattr(PriceRepairModule,'run',repair)
    await pipeline.tick(db,lane='seed_repair')
    await pipeline.tick(db,lane='seed_repair')
    assert seen==['2','1']


@pytest.mark.asyncio
async def test_draft_read_outage_is_shared_but_writes_are_never_retried(tmp_path,monkeypatch):
    from flowhub.db import Database
    from flowhub.source_detail import SourceCollector,SourceAcquisitionFailure
    SourceCollector.read_failures.clear()
    db=Database(tmp_path);calls=[]
    async def request(path,method,*args):
        calls.append(method)
        raise SourceAcquisitionFailure('UND_ERR_CONNECT_TIMEOUT',{})
    monkeypatch.setattr('flowhub.source_detail.request',request)
    for sku in range(4):
        c=SourceCollector(db,{'owner':'a','store':{'credentials':{'erp_token':'test'}},'candidate':{'source_key':str(sku)}})
        with pytest.raises(SourceAcquisitionFailure):await c.call('/api.product.collect/detail')
    assert calls==['GET']*3
    with pytest.raises(SourceAcquisitionFailure):await c.call('/api.product.favorite/edit_import','POST')
    assert calls==['GET']*3+['POST']
    SourceCollector.read_failures.clear()


def test_window_counts_first_verification_once_and_excludes_end_boundary(tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('measure_window',Path(__file__).resolve().parents[1]/'scripts/measure_throughput_window.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    c=sqlite3.connect(tmp_path/'flowhub.sqlite3')
    c.executescript('CREATE TABLE plugin_publications(body TEXT); CREATE TABLE plugin_repair_events(at REAL,body TEXT); CREATE TABLE plugin_pipeline(state TEXT);')
    for stamps in ([90,120],[110,120],[200]):
        p={'events':[{'at':t,'to':'stock_verified'} for t in stamps],'review':{'finished_at':80}}
        c.execute('INSERT INTO plugin_publications VALUES(?)',(json.dumps(p),))
    c.commit();c.close()
    report=module.measure(tmp_path,100,200)
    assert report['counts']['first_stock_verified']==1
    assert report['review_to_verified_seconds']=={'samples':1,'median':30}
