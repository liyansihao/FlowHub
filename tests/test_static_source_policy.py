import copy
import pytest
from flowhub.pipeline_modules.dossier import reviewed_snapshot, require_unchanged_source


def packet():
    detail={'contract':'direct-field-dossier-v1','sku':'1','observed_at':100,
            'weight_g':20,'dimensions_mm':[100,50,10],'attributes':[{'id':1,'values':['a']}],
            'field_observations':{k:{'sku':'1','source':'maozi-erp-draft','observed_at':100}
                                  for k in ('weight_g','dimensions_mm','attributes')}}
    product={'sku':'1','seller_id':'2','title':'a','image':'https://example.com/a.jpg','plugin_detail':detail}
    review={'candidate':{'source_key':'1','title':'a','image':product['image'],'origin':copy.deepcopy(product)}}
    return review,product


def test_old_static_evidence_is_reused_without_renewing_its_timestamp():
    review,_=packet()
    assert reviewed_snapshot(review,100+15*3600)['observed_at']==100
    assert reviewed_snapshot(review,100+7*86400) is None
    review['candidate']['origin']['plugin_detail']['field_observations']['attributes']['sku']='99'
    assert reviewed_snapshot(review,100+15*3600) is None


@pytest.mark.parametrize('field,value',[
    ('weight_g',30),('dimensions_mm',[100,50,20]),('attributes',[{'id':1,'values':['b']}]),
    ('variant_id','new'),('monthly_sales',{'blocked_by_seller':True}),
    ('monthly_sales',{'sales_schema':'FBO'})])
def test_newer_changed_facts_or_explicit_restrictions_still_block(field,value):
    review,product=packet();require_unchanged_source(review,product)
    product['plugin_detail'][field]=value
    with pytest.raises(ValueError):require_unchanged_source(review,product)


@pytest.mark.parametrize('field,value',[('sku','9'),('seller_id','9'),('title','other'),('image','https://example.com/new.jpg')])
def test_changed_source_identity_requires_another_review(field,value):
    review,product=packet();product[field]=value
    with pytest.raises(ValueError):require_unchanged_source(review,product)


def test_repair_reuses_same_static_packet_as_publication_without_renewal(monkeypatch):
    from flowhub.pipeline_modules.repair import missing_fields
    review,product=packet();product['url']='https://www.ozon.ru/product/1/'
    now=100+15*3600;monkeypatch.setattr('time.time',lambda:now)
    original=copy.deepcopy(product)
    missing=missing_fields(product)
    assert 'fresh_dossier' not in missing
    assert 'sale_price' in missing  # Static evidence must not renew price approval.
    assert product==original
    product['plugin_detail']['field_observations']['attributes']['sku']='99'
    assert 'fresh_dossier' in missing_fields(product)


def test_repair_keeps_expired_or_incomplete_dossier_missing(monkeypatch):
    from flowhub.pipeline_modules.repair import missing_fields
    _,product=packet()
    monkeypatch.setattr('time.time',lambda:100+7*86400)
    assert 'fresh_dossier' in missing_fields(product)
    monkeypatch.setattr('time.time',lambda:100+15*3600)
    product['plugin_detail']['attributes']=[]
    assert {'attributes','fresh_dossier'}<=set(missing_fields(product))
