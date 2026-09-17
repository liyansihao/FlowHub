// Browser transport for the real other-offers modal; no cart or publication actions.
import {chromium} from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';
import {sourceNetworkArgs} from './source-network.mjs';
const [sku,outDir,maxRoundsArg='8']=process.argv.slice(2);
if(!/^\d+$/.test(sku||''))throw Error('numeric SKU required');
const maxRounds=Math.min(40,Math.max(1,Number(maxRoundsArg)||8));
const profile=process.env.FLOWHUB_SOURCE_PROFILE;
if(!profile)throw Error('dedicated profile required');
await fs.mkdir(outDir,{recursive:true});
const c=await chromium.launchPersistentContext(profile,{channel:'chrome',headless:false,viewport:null,args:sourceNetworkArgs(),ignoreDefaultArgs:['--disable-extensions']});
process.once('SIGTERM',()=>{void c.close().catch(()=>{});});
process.once('SIGINT',()=>{void c.close().catch(()=>{});});
let step='navigation',p;
try{
 p=await c.newPage();
 const response=await p.goto(`https://www.ozon.ru/product/${sku}/`,{waitUntil:'domcontentloaded',timeout:45000});
 if(response?.status()===403||/captcha|antibot|access denied/i.test(await p.title())){step='access_challenge';throw Error('browser_access_challenge');}
 step='product_main';
 await p.locator('[data-widget="webProductMainWidget"]').waitFor({state:'attached',timeout:20000});
 // Match the original repository's headed-session warmup: SSR markup can
 // exist before the modal click handler has hydrated.
 await p.waitForTimeout(7000);
 // Delivery and offer widgets are lazy-loaded as the main product is viewed.
 await p.evaluate(()=>window.scrollTo(0,900));
 await p.waitForTimeout(3000);
 step='other_offers_entry';
 await p.locator('[data-widget="webBestSeller"]').waitFor({state:'visible',timeout:20000});
 await p.locator('[data-widget="webBestSeller"]').click();
 step='other_offers_modal';
 await p.locator('[data-widget="webSellerList"]').waitFor({state:'visible',timeout:15000});
 let rounds=0;
 for(;rounds<maxRounds;rounds++){
  const state=await p.evaluate(()=>{
   const e=document.querySelector('[id^="state-webBestSeller-"]');
   const count=Number(JSON.parse(e?.getAttribute('data-state')||'{}').count)||null;
   const links=[...document.querySelectorAll('[data-widget="webSellerList"] a[href*="/seller/"]')];
   return {count,loaded:new Set(links.map(a=>a.getAttribute('href'))).size};
  });
  if(state.count&&state.loaded===state.count)break;
  await p.locator('[data-widget="webSellerList"]').evaluate(e=>{
   let parent=e;while(parent){if(parent.scrollHeight>parent.clientHeight+5)parent.scrollTop=parent.scrollHeight;parent=parent.parentElement;}
  });
  await p.waitForTimeout(750);
 }
 const artifact=path.join(outDir,`${sku}-${Date.now()}.html`);
 await fs.writeFile(artifact,await p.content(),{mode:0o600});
 console.log(JSON.stringify({artifact,rounds}));
}catch(error){
 if(p)await fs.writeFile(path.join(outDir,`${sku}-failure-${Date.now()}.html`),await p.content().catch(()=>''),{mode:0o600});
 console.log(JSON.stringify({error:step,kind:error.name,network:/net::ERR_/.test(error.message)}));process.exitCode=1;
}
finally{await c.close();}
