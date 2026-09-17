import importlib.util
import json
from pathlib import Path
import time

import httpx
import pytest
from test_manual_reviews import setup

spec=importlib.util.spec_from_file_location('migrate_review_host',Path(__file__).parents[1]/'scripts/migrate_review_host.py')
migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[None,'destination','history'])
async def test_cutover_transfers_pending_and_rolls_back_on_failure(tmp_path,monkeypatch,failure):
    db,owner=setup(tmp_path)
    old='https://old.invalid/api/reviews';new='https://new.example.workers.dev/api/reviews'
    original=f'FLOWHUB_REVIEW_SYNC_URL={old}\nFLOWHUB_REVIEW_SYNC_TOKEN=old-secret\nFLOWHUB_REVIEW_OWNER={owner}\n'
    (db.directory/'review-sync.env').write_text(original)
    (db.directory/'remote-review-runtime.json').write_text(json.dumps({'version':2,'at':time.time()}))
    item={'sku':'test','seller':'seller','revision':'a'*64,'allowed_actions':['list']}
    snapshot={'owner':owner,'items':[item]}
    pending={'id':'decision-test','status':'pending','sku':'test','seller':'seller','action':'list','revision':'a'*64}
    states={old:snapshot,new:None};events=[];imported=[]
    monkeypatch.setattr(migration,'current_snapshot',lambda *a:snapshot)
    def serve(request):
        url=str(request.url).split('?')[0];mode=request.url.params.get('mode','')
        body=json.loads(request.content) if request.method=='POST' else None
        events.append((url,mode,body))
        if mode=='refresh':
            if failure=='destination' and url==new:return httpx.Response(503,json={'error':'destination unavailable'})
            states[url]=body;return httpx.Response(200,json={'ok':True})
        if mode=='import-history':
            if failure=='history':return httpx.Response(503,json={'error':'history unavailable'})
            imported.extend(body['items']);return httpx.Response(200,json={'ok':True})
        if mode=='sync':return httpx.Response(200,json={'items':[pending] if url==old else imported})
        return httpx.Response(200,json={'total':len(states[url]['items']),'items':states[url]['items'],'history':[pending] if url==old else imported})
    client=httpx.AsyncClient
    monkeypatch.setattr(migration.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(serve),**kwargs))
    if failure:
        with pytest.raises(httpx.HTTPStatusError):await migration.migrate(db,new,True)
        assert states[old]['items'][0]['allowed_actions']==['list']
        assert (db.directory/'review-sync.env').read_text()==original
    else:
        await migration.migrate(db,new,True)
        assert states[old]['items'][0]['allowed_actions']==[]
        assert imported==[pending]
        assert new in (db.directory/'review-sync.env').read_text()
        freeze=next(i for i,e in enumerate(events) if e[0]==old and e[1]=='refresh')
        assert any(e[0]==old and e[1]=='sync' for e in events[freeze+1:])
