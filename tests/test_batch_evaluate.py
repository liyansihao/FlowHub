import json
import pytest
from flowhub import batch_evaluate as batch

@pytest.mark.asyncio
async def test_restart_preserves_completed_rows_and_never_repeats_them(tmp_path,monkeypatch):
    monkeypatch.setattr(batch,'RUN',tmp_path)
    monkeypatch.setattr(batch,'Database',lambda:object())
    (tmp_path/'manifest.json').write_text(json.dumps([{'sku':str(i+1),'seller_id':'2'} for i in range(922)]))
    called=[]
    async def one(db,row):
        called.append(row['sku']);batch.set_stop()
        return 'matched','',{'result':{'purchase':4.1}},20
    async def sleep(*args):pass
    monkeypatch.setattr(batch,'one',one);monkeypatch.setattr(batch.asyncio,'sleep',sleep)
    batch.stop=False
    await batch.main()
    assert batch.summary()['counts']=={'matched':1,'pending':921}
    batch.stop=False
    await batch.main()
    assert called==['1','2']
    assert batch.summary()['counts']=={'matched':2,'pending':920}
    batch.stop=False
