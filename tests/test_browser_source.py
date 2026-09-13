import pytest
from flowhub.browser_source import BrowserSource
from flowhub.db import Database
from flowhub.storefront import StorefrontError
from test_storefront import packet


def test_pause_restart_atomic_cursor_dedup_and_recommendation_exclusion(tmp_path):
    db=Database(tmp_path);s=BrowserSource(db);key=('a','scan1','12')
    s.prepare('a','scan1',{'12':[{'sku':'root'}]})
    assert s.next_request(*key)['url'] is None
    s.control(*key,'resume');url=s.next_request(*key)['url']
    first=packet()
    assert s.ingest(*key,url,first,'first')['added']==1
    s.control(*key,'pause');s=BrowserSource(Database(tmp_path))
    with pytest.raises(StorefrontError,match='task_paused'):s.ingest(*key,url+'?page=2',packet(2,'124'),'second')
    assert s.ingest(*key,url,first,'first')['state']=='replay'
    s.control(*key,'resume');n=s.next_request(*key)
    assert n['page']==2
    assert s.ingest(*key,n['url'],packet(2,'124',continuation=False),'second')['added']==1
    assert s.next_request(*key)['state']=='done'
    with db.connect() as c:assert [r[0] for r in c.execute('SELECT sku FROM sourcing_products ORDER BY sku')]==['123','124']


def test_failed_page_does_not_advance_and_new_scan_preserves_old(tmp_path):
    db=Database(tmp_path);s=BrowserSource(db);key=('a','scan1','12')
    s.prepare('a','scan1',{'12':[{'sku':'root'}]});s.control(*key,'resume');url=s.next_request(*key)['url']
    assert s.ingest(*key,url,packet(seller='99'),'bad')['reason']=='identity_mismatch'
    assert s.next_request(*key)['page']==1
    with pytest.raises(ValueError,match='retry_required'):s.control(*key,'resume')
    s.control(*key,'retry');assert s.ingest(*key,url,packet(),'good')['added']==1
    s.prepare('a','scan2',{'12':[{'sku':'root'}]});s.control('a','scan2','12','resume')
    assert s.ingest('a','scan2','12',url,packet(),'new-scan')['added']==0
    assert s.next_request(*key)['page']==2


def test_verified_seller_slug_without_numeric_id_keeps_cursor_binding(tmp_path):
    db=Database(tmp_path);s=BrowserSource(db);key=('a','scan','12')
    s.prepare('a','scan',{'12':[{'sku':'root'}]});s.control(*key,'resume')
    first=packet().replace('/seller/12/products/','/seller/shop-name/products/')
    result=s.ingest(*key,s.next_request(*key)['url'],first,'first')
    assert result['state']=='committed' and '/shop-name/' in result['next_url']
    second=packet(2,'124').replace('/seller/12/products/','/seller/shop-name/products/')
    assert s.ingest(*key,result['next_url'],second,'second')['added']==1


def test_slug_does_not_override_page_seller_id():
    from flowhub.storefront import parse_packet
    wrong=packet(seller='99').replace('/seller/99/products/','/seller/shop-name/products/')
    with pytest.raises(StorefrontError,match='identity_mismatch'):
        parse_packet(wrong,'12','https://www.ozon.ru/seller/shop-name/products/')
