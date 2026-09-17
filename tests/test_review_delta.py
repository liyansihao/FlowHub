import json
import httpx
import pytest
from types import SimpleNamespace
from flowhub.review_delta import prepare,upload,read,next_delay
CONFIG={'FLOWHUB_REVIEW_SYNC_URL':'https://review.invalid/api','FLOWHUB_REVIEW_OWNER':'one'}
def snap(items=None):return {'owner':'one','generated_at':'one','items':items if items is not None else [{'sku':'123','seller':'456','title':'x'}],'local_history':[{'id':'local-1','note':'ok'}]}

def test_first_then_noop_changed_deleted_and_owner_isolation():
    s=snap();mode,body,cp=prepare(s,CONFIG,{},'');assert mode=='refresh'
    assert prepare(s|{'generated_at':'later'},CONFIG,cp,cp['cursor'])[0] is None
    mode,body,cp2=prepare(snap([{'sku':'123','seller':'456','title':'y'},{'sku':'789','seller':'456','title':'z'}]),CONFIG,cp,cp['cursor']);assert mode=='delta' and len(body['upserts'])==2 and body['history']==[]
    mode,body,_=prepare(snap([]),CONFIG,cp2,cp2['cursor']);assert len(body['removed'])==2
    assert prepare(s,CONFIG,cp,'different')[0]=='refresh'
    assert prepare(s,CONFIG|{'FLOWHUB_REVIEW_SYNC_URL':'https://other.invalid'},cp,cp['cursor'])[0]=='refresh'

@pytest.mark.asyncio
async def test_lost_ack_never_advances_checkpoint_and_noop_skips_network(tmp_path):
    db=SimpleNamespace(directory=tmp_path);calls=[]
    async def failure(*a,**kw):raise httpx.ReadTimeout('simulated')
    with pytest.raises(httpx.ReadTimeout):await upload(db,CONFIG,None,snap(),'',failure)
    assert not (tmp_path/'review-delta-checkpoint.json').exists()
    async def ok(*a,**kw):calls.append(kw);return httpx.Response(200,json={'cursor':kw['body']['cursor']},request=httpx.Request('POST','https://review.invalid'))
    await upload(db,CONFIG,None,snap(),'',ok);cp=read(tmp_path/'review-delta-checkpoint.json');assert len(calls)==1
    await upload(db,CONFIG,None,snap(),cp['cursor'],ok);assert len(calls)==1
    assert read(tmp_path/'review-delta-metrics.json')['skipped_uploads']==1


def test_idle_backoff_and_reactivation():
    delay=30
    for _ in range(6):delay=next_delay(delay,30,600,False,False)
    assert delay==600
    assert next_delay(600,30,600,True,False)==30
    assert next_delay(600,30,600,False,True)==30
