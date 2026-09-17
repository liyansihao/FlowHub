import pytest

from flowhub.db import Database
from flowhub.modules import ModuleError, Pending
from flowhub.source_detail import SourceCollector, map_detail


@pytest.fixture
def context():
    return {
        "owner": "a",
        "candidate": {"source_key": "123", "title": "title", "image": "https://example.com/a", "price": 10},
        "store": {"credentials": {"erp_token": "test"}},
    }


async def test_collection_cache_and_exact_source_no_publishing(tmp_path, context):
    db = Database(tmp_path)
    calls = []

    async def call(path, method="GET", query=None, body=None):
        calls.append((path, method, query, body))
        if path.endswith("/lists"):
            return {"data": [{"id": 9, "sku": "wrong"}, {"id": 7, "sku": "123"}], "total": 2}
        if path.endswith("/edit_import"):
            assert body == {"id": 7}
            return {"jump_id": 88}
        return {"title": "source", "skus": [{}]}

    first = SourceCollector(db, context)
    first.call = call
    result = await first.collect()
    second = SourceCollector(db, context)
    second.call = call
    assert await second.collect() == result
    assert len(calls) == 4
    assert all("/import_ozon" not in c[0] and "/stocks" not in c[0] for c in calls)
    assert b"test" not in db.path.read_bytes()


async def test_lost_draft_response_not_replayed(tmp_path, context):
    db = Database(tmp_path)
    writes = 0

    async def call(path, method="GET", query=None, body=None):
        nonlocal writes
        if path.endswith("/lists"):
            return {"data": [{"id": 7, "sku": "123"}], "total": 1}
        writes += 1
        raise TimeoutError()

    one = SourceCollector(db, context)
    one.call = call
    with pytest.raises(TimeoutError):
        await one.collect()
    two = SourceCollector(db, context)
    two.call = call
    with pytest.raises(Pending):
        await two.collect()
    assert writes == 1


async def test_acknowledged_draft_retries_only_detail(tmp_path, context):
    db = Database(tmp_path)
    one = SourceCollector(db, context)
    one.save("draft_ready", {"source_key": "123", "draft_id": 44})

    async def call(path, method="GET", query=None, body=None):
        assert path == "/api.product.collect/detail" and query["id"] == 44
        return {"title": "source"}

    one.call = call
    assert (await one.collect())["detail"]["title"] == "source"


async def test_mapping_validates_dictionary_and_does_not_copy_seller_terms(context):
    snapshot = {
        "source_key": "123",
        "draft_id": 44,
        "observed_at": 1,
        "detail": {
            "category_id": [1, 2, 3],
            "title": "source",
            "vat": "20",
            "shop_id": 99,
            "description": "description",
            "common_attributes": [{"id": 85, "values": [5]}, {"id": 9048, "values": "model"}],
            "skus": [
                {
                    "name": "variant",
                    "price": 999,
                    "offer_id": "old",
                    "images": ["https://example.com/image"],
                    "attributes": [],
                }
            ],
        },
    }

    async def seller(path, body):
        if path.endswith("/attribute"):
            return {
                "result": [
                    {"id": 85, "dictionary_id": 1, "is_required": True},
                    {"id": 9048, "dictionary_id": 0, "is_required": True},
                    {"id": 4191, "dictionary_id": 0},
                ]
            }
        return {"result": [{"id": 5, "value": "brand"}]}

    mapped = await map_detail(snapshot, context, seller)
    assert mapped["mapped_attributes"] == 3 and not mapped["required_missing"]
    raw = mapped["dossier"]
    assert raw["name"] == "variant"
    assert not any(k in raw for k in ("vat", "shop_id", "price", "offer_id", "weight"))
    assert raw["attributes"][0]["values"] == [{"dictionary_value_id": 5, "value": "brand"}]
    snapshot["source_key"] = "wrong"
    with pytest.raises(ModuleError):
        await map_detail(snapshot, context, seller)


async def test_multiple_variants_and_conflicts_never_silently_pick(context):
    snapshot = {
        "source_key": "123",
        "draft_id": 1,
        "observed_at": 1,
        "detail": {"category_id": [1, 2, 3], "skus": [{}, {}]},
    }
    with pytest.raises(ModuleError):
        await map_detail(snapshot, context, None)
    snapshot["detail"]["skus"] = [{"attributes": [{"id": 9, "values": "blue"}]}]
    snapshot["detail"]["common_attributes"] = [{"id": 9, "values": "red"}]

    async def seller(path, body):
        return {"result": [{"id": 9, "dictionary_id": 0}]}

    assert (await map_detail(snapshot, context, seller))["issues"]


async def test_failed_read_can_retry_without_replaying_writes(tmp_path, context):
    db = Database(tmp_path)
    one = SourceCollector(db, context)

    async def failed(path, method="GET", query=None, body=None):
        raise TimeoutError()

    one.call = failed
    with pytest.raises(TimeoutError):
        await one.collect()
    assert one.load()[0] == "new"


async def test_obsolete_brand_id_resolves_only_exact_official_text(context):
    snapshot = {
        "source_key": "123",
        "draft_id": 44,
        "observed_at": 1,
        "detail": {
            "category_id": [1, 2, 3],
            "brand_id": 5,
            "brand_select": {"id": 5, "value": "QIACHIP"},
            "skus": [{}],
        },
    }

    async def seller(path, body):
        if path.endswith("/attribute"):
            return {"result": [{"id": 85, "dictionary_id": 1, "is_required": True}]}
        if path.endswith("/search"):
            return {"result": [{"id": 66, "value": "QIACHIP"}, {"id": 67, "value": "QIACHIP Other"}]}
        return {"result": []}

    result = await map_detail(snapshot, context, seller)
    assert not result["issues"] and not result["required_missing"]
    assert result["dossier"]["attributes"][0]["values"][0]["dictionary_value_id"] == 66


async def test_original_russian_title_wins_over_english_draft_label(context):
    snapshot = {
        "source_key": "123",
        "draft_id": 44,
        "observed_at": 1,
        "detail": {
            "category_id": [1, 2, 3],
            "title": "Folder A3",
            "common_attributes": [{"id": 4180, "values": "Папка для документов А3"}],
            "skus": [{"name": "SKU 123 Folder A3", "attributes": []}],
        },
    }

    async def seller(path, body):
        return {"result": [{"id": 4180, "dictionary_id": 0}]}

    result = await map_detail(snapshot, context, seller)
    assert result["dossier"]["name"] == "Папка для документов А3"


async def test_existing_favorite_only_never_creates_unpriced_favorite(tmp_path, context):
    context['existing_favorite_only'] = True
    context['candidate']['price'] = None
    collector = SourceCollector(Database(tmp_path), context)
    calls = []

    async def call(path, method='GET', query=None, body=None):
        calls.append((path, method))
        assert method == 'GET'
        return []

    collector.call = call
    with pytest.raises(Pending, match='real price'):
        await collector.collect()
    assert calls == [('/api.product.favorite/lists', 'GET')] * 2


async def test_capacity_refusal_can_retry_but_unknown_write_still_cannot(tmp_path,context):
    import time
    from flowhub.source_detail import SourceAcquisitionFailure
    db=Database(tmp_path);one=SourceCollector(db,context);writes=[]
    async def full(path,method='GET',query=None,body=None):
        if path.endswith('/lists'):return {'data':[{'id':7,'sku':'123'}],'total':1}
        writes.append(path)
        raise SourceAcquisitionFailure('ERP_SOURCE_REJECTED',{'api_code':0,'api_message':'采集箱已满，上限1000个','operation':path})
    one.call=full
    with pytest.raises(SourceAcquisitionFailure):await one.collect()
    assert one.load()[0]=='draft_rejected'
    with db.connect() as c:c.execute('UPDATE source_details SET updated=?',(time.time()-121,))
    async def freed(path,method='GET',query=None,body=None):
        if path.endswith('/lists'):return {'data':[{'id':7,'sku':'123'}],'total':1}
        if path.endswith('/edit_import'):writes.append(path);return {'jump_id':88}
        return {'skus':[{}]}
    two=SourceCollector(db,context);two.call=freed
    assert (await two.collect())['draft_id']==88
    assert len(writes)==2


@pytest.mark.parametrize('ambiguous',[False,True])
async def test_unknown_draft_is_recovered_by_exact_listing_without_writes(tmp_path,context,ambiguous):
    import time
    db=Database(tmp_path);one=SourceCollector(db,context);one.recovery_pages.clear()
    one.save('draft_started',{'source_key':'123','favorite_id':7})
    with db.connect() as c:c.execute('UPDATE source_details SET updated=?',(time.time()-121,))
    calls=[]
    async def read(path,method='GET',query=None,body=None):
        assert method=='GET';calls.append(path)
        if path.endswith('/lists'):
            rows=[{'id':88,'goods_id':'123','collect_from':'ozon'},{'id':90,'goods_id':'999','collect_from':'ozon'}]
            if ambiguous:rows.append({'id':89,'goods_id':'123','collect_from':'ozon'})
            return {'data':rows,'total':len(rows)}
        assert query=={'id':88,'is_online':0}
        return {'skus':[{}]}
    one.call=read
    if ambiguous:
        with pytest.raises(Pending,match='ambiguous'):await one.collect()
        assert one.load()[0]=='draft_started'
    else:
        assert (await one.collect())['draft_id']==88
        assert one.load()[0]=='ready'


async def test_explicit_favorite_capacity_refusal_is_retryable_after_backoff(tmp_path,context):
    import time
    from flowhub.source_detail import SourceAcquisitionFailure
    db=Database(tmp_path);one=SourceCollector(db,context);writes=[]
    async def call(path,method='GET',query=None,body=None):
        if path.endswith('/lists'):return []
        writes.append(path)
        raise SourceAcquisitionFailure('ERP_SOURCE_REJECTED',{'api_code':0,'operation':path,'write_outcome_unknown':False,'api_message':'收藏数量已达上限（3000个），请先删除部分收藏'})
    one.call=call
    with pytest.raises(SourceAcquisitionFailure):await one.collect()
    state,data,_=one.load()
    assert state=='favorite_rejected' and not data.get('favorite_attempted')
    with pytest.raises(Pending):await one.collect()
    assert len(writes)==1
    with db.connect() as c:c.execute('UPDATE source_details SET updated=?',(time.time()-121,))
    with pytest.raises(SourceAcquisitionFailure):await one.collect()
    assert len(writes)==2


def age_collection(collector):
    import time
    with collector.db.connect() as connection:
        connection.execute('UPDATE source_details SET updated=? WHERE key=?',
                           (time.time() - 121, collector.key))


async def test_favorite_lookup_reads_both_groups_and_server_capped_pages(tmp_path, context):
    collector = SourceCollector(Database(tmp_path), context)
    calls = []

    async def read(path, method='GET', query=None, body=None):
        assert method == 'GET'
        assert query['sku'] == '123' and query['page_size'] == 100
        group, page = query['is_imported'], query['page']
        calls.append((group, page))
        if group == 0:
            return {'data': [], 'total': 0}
        # The server caps pages at one row and ignores the SKU filter.
        row = {'id': 8, 'sku': '999'} if page == 1 else {'id': 7, 'sku': '123'}
        return {'data': [row], 'total': '2', 'per_page': 1}

    collector.call = read
    assert (await collector.find_favorite())['id'] == 7
    assert calls == [(0, 1), (1, 1), (1, 2)]


@pytest.mark.parametrize('field', ['data', 'list', 'rows', 'items', None])
async def test_favorite_lookup_without_total_requires_empty_terminal_page(tmp_path, context, field):
    collector = SourceCollector(Database(tmp_path), context)

    async def read(path, method='GET', query=None, body=None):
        rows = [{'id': 7, 'sku': '123'}] if query['page'] == 1 else []
        return {field: rows} if field else rows

    collector.call = read
    assert (await collector.find_favorite())['id'] == 7  # Same ID across groups is one favorite.


@pytest.mark.parametrize('response', [None, {}, {'data': {}}, {'data': [None]},
                                     {'data': [{'sku': '123'}]}, {'data': [], 'total': 'bad'},
                                     {'data': [], 'total': 1}, {'data': [], 'total': True}])
async def test_invalid_listing_never_authorizes_creation(tmp_path, context, response):
    collector = SourceCollector(Database(tmp_path), context)

    async def read(path, method='GET', query=None, body=None):
        assert method == 'GET'
        return response

    collector.call = read
    with pytest.raises(Pending):
        await collector.collect()
    assert collector.load()[0] == 'new'


async def test_repeated_page_and_duplicate_sku_do_not_authorize_write(tmp_path, context):
    collector = SourceCollector(Database(tmp_path), context)

    async def repeated(path, method='GET', query=None, body=None):
        assert method == 'GET'
        return {'data': [{'id': 7, 'sku': '123'}], 'total': 2}

    collector.call = repeated
    with pytest.raises(Pending, match='repeated'):
        await collector.collect()

    async def ambiguous(path, method='GET', query=None, body=None):
        assert method == 'GET'
        return {'data': [{'id': 7 + query['is_imported'], 'sku': '123'}], 'total': 1}

    collector.call = ambiguous
    with pytest.raises(ModuleError, match='ambiguous'):
        await collector.collect()


@pytest.mark.parametrize('existing_only', [False, True])
async def test_legacy_unknown_is_not_reset_by_missing_favorite_or_old_rejection(tmp_path, context, existing_only):
    context['existing_favorite_only'] = existing_only
    collector = SourceCollector(Database(tmp_path), context)
    collector.save('favorite_started', {'favorite_attempted': True})
    # Historical events are owner/SKU scoped, not bound to this account/write attempt.
    with collector.db.connect() as connection:
        connection.execute('CREATE TABLE plugin_repair_events(owner TEXT,sku TEXT,body TEXT)')
        connection.execute('INSERT INTO plugin_repair_events VALUES(?,?,?)',
                           ('a', '123', '{"api_code":0,"api_message":"收藏数量已达上限","write_outcome_unknown":false}'))

    async def read(path, method='GET', query=None, body=None):
        assert method == 'GET'
        return {'data': [], 'total': 0}

    collector.call = read
    for _ in range(2):
        age_collection(collector)
        with pytest.raises(Pending, match='favorite creation not confirmed'):
            await collector.collect()
        assert collector.load()[0] == 'favorite_started'
        assert collector.load()[1]['favorite_attempted'] is True


@pytest.mark.parametrize('override', [ {'write_outcome_unknown': True}, {'write_outcome_unknown': None},
                                      {'api_code': 1}, {'operation': '/other'}, {'api_message': 'timeout'}])
async def test_inexact_capacity_failure_remains_unknown(tmp_path, context, override):
    from flowhub.source_detail import SourceAcquisitionFailure
    collector = SourceCollector(Database(tmp_path), context)
    writes = []

    async def call(path, method='GET', query=None, body=None):
        if method == 'GET':
            return []
        writes.append(path)
        raise SourceAcquisitionFailure('ERP_SOURCE_REJECTED', {
            'api_code': 0, 'operation': path, 'write_outcome_unknown': False,
            'api_message': '收藏数量已达上限', **override,
        })

    collector.call = call
    with pytest.raises(SourceAcquisitionFailure):
        await collector.collect()
    assert collector.load()[0] == 'favorite_started'
    age_collection(collector)
    with pytest.raises(Pending, match='not confirmed'):
        await collector.collect()
    assert len(writes) == 1


async def test_lost_favorite_acknowledgement_is_not_replayed_after_read_failure(tmp_path, context):
    collector = SourceCollector(Database(tmp_path), context)
    writes = []

    async def call(path, method='GET', query=None, body=None):
        if method == 'GET':
            return []
        writes.append(path)
        raise TimeoutError()

    collector.call = call
    with pytest.raises(TimeoutError):
        await collector.collect()
    age_collection(collector)

    async def failed_read(path, method='GET', query=None, body=None):
        assert method == 'GET'
        raise TimeoutError()

    collector.call = failed_read
    with pytest.raises(TimeoutError):
        await collector.collect()
    assert collector.load()[0] == 'favorite_started'
    age_collection(collector)
    collector.call = call
    with pytest.raises(Pending):
        await collector.collect()
    assert len(writes) == 1


async def test_imported_favorite_recovers_existing_draft_without_import(tmp_path, context):
    collector = SourceCollector(Database(tmp_path), context)
    collector.recovery_pages.clear()
    collector.save('favorite_started', {'favorite_attempted': True})
    age_collection(collector)

    async def read(path, method='GET', query=None, body=None):
        assert method == 'GET'
        if path == '/api.product.favorite/lists':
            rows = [{'id': 7, 'sku': '123', 'is_imported': 1}] if query['is_imported'] else []
            return {'data': rows, 'total': len(rows)}
        if path == '/api.product.collect/lists':
            return {'data': [{'id': 88, 'goods_id': '123', 'collect_from': 'ozon'}], 'total': 1}
        assert query == {'id': 88, 'is_online': 0}
        return {'skus': [{}]}

    collector.call = read
    assert (await collector.collect())['draft_id'] == 88


@pytest.mark.parametrize('unknown', [False, True])
async def test_lost_claim_during_lookup_does_not_overwrite_new_owner(tmp_path, context, unknown):
    collector = SourceCollector(Database(tmp_path), context)
    collector.save('favorite_started' if unknown else 'claimed', {'favorite_attempted': unknown})
    age_collection(collector)

    async def read(path, method='GET', query=None, body=None):
        assert method == 'GET'
        collector.save('draft_started', {'favorite_id': 99})
        return {'data': [], 'total': 0}

    collector.call = read
    with pytest.raises(Pending, match='claim changed'):
        await collector.collect()
    assert collector.load()[:2] == ('draft_started', {'favorite_id': 99})

async def test_recent_cache_write_does_not_renew_old_draft_observation(tmp_path,context):
    import time
    db=Database(tmp_path);collector=SourceCollector(db,context)
    collector.save('ready',{'source_key':'123','draft_id':88,'observed_at':time.time()-21601,'detail':{'title':'old'}})
    calls=[]
    async def call(path,method='GET',query=None,body=None):
        calls.append((path,method));assert query=={'id':88,'is_online':0}
        return {'title':'fresh'}
    collector.call=call
    result=await collector.collect()
    assert calls==[('/api.product.collect/detail','GET')]
    assert result['detail']['title']=='fresh' and time.time()-result['observed_at']<2
