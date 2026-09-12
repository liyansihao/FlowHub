import {test} from 'node:test';
import assert from 'node:assert/strict';
import {nativeShopPage} from '../bridges/native-shop.mjs';

test('normal Set-Cookie round trip resolves redirect without browser or ERP token', async () => {
 let calls=0;
 const result=await nativeShopPage('1225438', 2, async (url, init)=>{
  calls++;
  assert.equal(init.redirect,'manual');
  assert.equal(init.headers.Authorization,undefined);
  assert.match(url.searchParams.get('url'), /page=2/);
  if(calls===1){
   assert.equal(init.headers.Cookie,undefined);
   return new Response('',{status:307,headers:{location:url.href+'&__rr=1','set-cookie':'__Secure-ETC=test-session; Domain=.ozon.ru; Path=/; Secure; HttpOnly'}});
  }
  assert.equal(init.headers.Cookie,'__Secure-ETC=test-session');
  return Response.json({widgetStates:{'test-widget':'{}'}});
 });
 assert.equal(calls,2);
 assert.equal(result.ok,true);
 assert.equal(result.diagnostic.coverage,'native-shop-unverified');
 assert.equal(JSON.stringify(result).includes('test-session'),false);
});
test('captcha response stops on second request and is not a successful product page',async()=>{
 let calls=0;
 const result=await nativeShopPage('12',1,async(url)=> ++calls===1
  ?new Response('',{status:307,headers:{location:url.href+'&__rr=1','set-cookie':'session=value; Path=/; Secure'}})
  :Response.json({captchaURL:'https://www.ozon.ru/captcha.html?secret=challenge',incidentId:'incident'},{status:403}));
 assert.equal(calls,2);
 assert.equal(result.error,'native_captcha_required');
 assert.equal(result.diagnostic.incident_id,'incident');
 assert.equal(JSON.stringify(result).includes('challenge'),false);
});
test('redirect budget and origin/identity boundaries are enforced',async()=>{
 let calls=0;
 const loop=await nativeShopPage('12',1,async url=>{calls++;return new Response('',{status:307,headers:{location:url.href}});});
 assert.equal(loop.error,'native_redirect_limit');assert.equal(calls,4);
 for(const [location,error] of [['https://example.com/a','native_redirect_origin'],['/api/entrypoint-api.bx/page/json/v2?url=/seller/99/','native_redirect_identity']]){
  calls=0;
  const result=await nativeShopPage('12',1,async()=>{calls++;return new Response('',{status:307,headers:{location}});});
  assert.equal(result.error,error);assert.equal(calls,1);
 }
});
test('TypeError retains low-level cause', async()=>{
 const result=await nativeShopPage('12',1, async()=>{throw new TypeError('fetch failed',{cause:Object.assign(Error('socket reset'),{code:'ECONNRESET'})});});
 assert.equal(result.diagnostic.cause,'socket reset');
 assert.equal(result.diagnostic.cause_code,'ECONNRESET');
});
test('unrecognized 200 JSON cannot be treated as an empty shop',async()=>{
 const r=await nativeShopPage('12',1,async()=>Response.json({widgetStates:{}}));
 assert.equal(r.error,'native_schema_unverified');
 await assert.rejects(nativeShopPage('../x',1), /invalid_shop_request/);
});
