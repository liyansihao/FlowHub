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
            return [{"id": 9, "sku": "wrong"}, {"id": 7, "sku": "123"}]
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
    assert len(calls) == 3
    assert all("/import_ozon" not in c[0] and "/stocks" not in c[0] for c in calls)
    assert b"test" not in db.path.read_bytes()


async def test_lost_draft_response_not_replayed(tmp_path, context):
    db = Database(tmp_path)
    writes = 0

    async def call(path, method="GET", query=None, body=None):
        nonlocal writes
        if path.endswith("/lists"):
            return [{"id": 7, "sku": "123"}]
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
    assert calls == [('/api.product.favorite/lists', 'GET')]


async def test_capacity_refusal_can_retry_but_unknown_write_still_cannot(tmp_path,context):
    import time
    from flowhub.source_detail import SourceAcquisitionFailure
    db=Database(tmp_path);one=SourceCollector(db,context);writes=[]
    async def full(path,method='GET',query=None,body=None):
        if path.endswith('/lists'):return [{'id':7,'sku':'123'}]
        writes.append(path)
        raise SourceAcquisitionFailure('ERP_SOURCE_REJECTED',{'api_code':0,'api_message':'采集箱已满，上限1000个','operation':path})
    one.call=full
    with pytest.raises(SourceAcquisitionFailure):await one.collect()
    assert one.load()[0]=='draft_rejected'
    with db.connect() as c:c.execute('UPDATE source_details SET updated=?',(time.time()-121,))
    async def freed(path,method='GET',query=None,body=None):
        if path.endswith('/lists'):return [{'id':7,'sku':'123'}]
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
