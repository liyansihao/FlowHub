import json
import httpx
import pytest
from flowhub import remote_reviews
from test_manual_reviews import setup, rev


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['repair','stale','wrong_owner','ack_lost'])
async def test_sync_applies_refreshes_and_replays_safely(tmp_path,monkeypatch,case):
    db,owner=setup(tmp_path)
    monkeypatch.setenv('FLOWHUB_REVIEW_SYNC_URL','https://review.invalid/api/reviews')
    monkeypatch.setenv('FLOWHUB_REVIEW_SYNC_TOKEN','test-token')
    monkeypatch.setenv('FLOWHUB_REVIEW_OWNER',owner)
    item={'id':'decision-1','owner':owner,'sku':'123','seller':'456','reviewer':'human','action':'repair','note':'补全资料后重算','revision':rev(db,owner)}
    if case=='stale':item['revision']='0'*64
    if case=='wrong_owner':item['owner']='another-tenant'
    snapshots=[];acks=[]
    def serve(request):
        assert request.headers['x-sync-token']=='test-token'
        mode=request.url.params['mode']
        if mode=='refresh':snapshots.append(json.loads(request.content));return httpx.Response(200,json={'ok':True})
        if mode=='sync':return httpx.Response(200,json={'items':[item]})
        ack=json.loads(request.content);acks.append(ack)
        return httpx.Response(503 if case=='ack_lost' and len(acks)==1 else 200,json={'ok':True})
    client=httpx.AsyncClient
    monkeypatch.setattr(remote_reviews.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(serve),**kwargs))
    expected=0 if case in ('stale','wrong_owner') else 1
    assert await remote_reviews.once(db)==expected
    assert acks[-1]['status']==('rejected' if not expected else 'applied')
    assert len(snapshots)==1 and snapshots[-1]['total']==1
    if case=='ack_lost':
        assert await remote_reviews.once(db)==1
        assert acks[-1]['status']=='applied'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM human_reviews').fetchone()[0]==expected
        if expected:assert json.loads(c.execute('SELECT body FROM plugin_pipeline').fetchone()[0])['repair_full_dossier']


def test_file_configuration_overrides_inherited_supervisor_environment(tmp_path,monkeypatch):
    db,owner=setup(tmp_path)
    monkeypatch.setenv('FLOWHUB_REVIEW_SYNC_URL','https://old.invalid/api/reviews')
    (db.directory/'review-sync.env').write_text('FLOWHUB_REVIEW_SYNC_URL=https://new.invalid/api/reviews\nFLOWHUB_REVIEW_SYNC_TOKEN=new-token\n')
    assert remote_reviews.settings(db)['FLOWHUB_REVIEW_SYNC_URL']=='https://new.invalid/api/reviews'


@pytest.mark.asyncio
async def test_migration_lock_prevents_refresh_and_command_execution(tmp_path,monkeypatch):
    import fcntl
    db,owner=setup(tmp_path)
    async def forbidden(*args):pytest.fail('sync must not run while migration holds lock')
    monkeypatch.setattr(remote_reviews,'sync_once',forbidden)
    with (db.directory/'remote-review-sync.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert await remote_reviews.once(db)==0


@pytest.mark.asyncio
async def test_fast_poll_reads_commands_without_building_or_uploading_snapshot(tmp_path,monkeypatch):
    db,owner=setup(tmp_path)
    monkeypatch.setenv('FLOWHUB_REVIEW_SYNC_URL','https://review.invalid/api/reviews')
    monkeypatch.setenv('FLOWHUB_REVIEW_SYNC_TOKEN','test-token')
    monkeypatch.setenv('FLOWHUB_REVIEW_OWNER',owner)
    monkeypatch.setattr(remote_reviews,'current_snapshot',lambda *a:pytest.fail('empty fast poll should not build snapshot'))
    modes=[]
    def serve(request):
        modes.append(request.url.params['mode']);return httpx.Response(200,json={'items':[]})
    client=httpx.AsyncClient
    monkeypatch.setattr(remote_reviews.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(serve),**kw))
    assert await remote_reviews.once(db,refresh=False)==0
    assert modes==['sync']
