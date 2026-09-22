import json
from unittest.mock import AsyncMock
import pytest
from tests.test_continuous_admission import setup
from flowhub.pipeline_modules.admission import admit_one
from flowhub.pipeline_modules.store_capacity import observe,check_one


def test_exhausted_store_gets_no_new_admissions_then_recovers(tmp_path):
    db,owner=setup(tmp_path)
    assert observe(db,owner,'test',{'total':'0/100','daily_create':'10/100'},now=100)=='blocked'
    assert admit_one(db,owner,now=100)['reason']=='no_available_target_store'
    observe(db,owner,'test',{'total':'10/100','daily_create':'10/100'},now=101)
    assert admit_one(db,owner,now=102)['state']=='admitted'


@pytest.mark.asyncio
async def test_failed_quota_refresh_keeps_store_blocked(tmp_path,monkeypatch):
    db,owner=setup(tmp_path);observe(db,owner,'test',{'total':'0/100'},now=1)
    from flowhub.maozi import MaoziPublisher
    monkeypatch.setattr(MaoziPublisher,'erp',AsyncMock(side_effect=TimeoutError()))
    from flowhub import official_api
    monkeypatch.setattr(official_api,'capacity',AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):await check_one(db)
    with db.connect() as c:assert c.execute('select state from store_publication_capacity').fetchone()[0]=='blocked'


def test_repair_capacity_reserves_a_slot_before_admitting_more(tmp_path):
    db,owner=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE pipeline_campaigns SET body=json_set(body,'$.max_inflight',2)")
        c.execute("INSERT INTO plugin_pipeline VALUES(?,?,?,'needs_fields','{}',0,0)",(owner,'repair','3'))
        c.execute("INSERT INTO plugin_pipeline VALUES(?,?,?,'queued','{}',0,0)",(owner,'active','3'))
    assert admit_one(db,owner)['state']=='backpressure'
