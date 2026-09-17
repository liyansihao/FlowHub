// List-page transport only. No ERP credentials, plugin, favorites, reviews or writes.
import {chromium} from 'playwright';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import fs from 'node:fs/promises';
import path from 'node:path';
import {sourceNetworkArgs} from './source-network.mjs';
const exec=promisify(execFile);
const root=path.resolve(import.meta.dirname,'..');
const [owner,runId,seller,maxPagesArg='3']=process.argv.slice(2);
if(!owner||!runId||!/^\d+$/.test(seller||'')||!/^[a-zA-Z0-9_-]+$/.test(runId))throw Error('owner run-id numeric-seller [max-pages] required');
const maxPages=Number(maxPagesArg);
if(!Number.isSafeInteger(maxPages)||maxPages<1||maxPages>1000)throw Error('invalid max-pages');
const profile=process.env.FLOWHUB_SOURCE_PROFILE;
if(!profile)throw Error('FLOWHUB_SOURCE_PROFILE required; never use the personal default Chrome profile');
const out=path.join(root,'output/playwright',runId,seller);await fs.mkdir(out,{recursive:true});
async function api(action,args=[]){
 const {stdout}=await exec(path.join(root,'.venv/bin/python'),['-m','flowhub.browser_source',action,'--owner',owner,'--run-id',runId,'--seller',seller,...args],{cwd:root,maxBuffer:2_000_000});
 return JSON.parse(stdout);
}
const context=await chromium.launchPersistentContext(path.resolve(profile),{
 channel:'chrome',headless:false,viewport:null,
 args:['--no-first-run','--no-default-browser-check',...sourceNetworkArgs()],
 ignoreDefaultArgs:['--disable-extensions'],
});
const page=await context.newPage();
let stopped=false;
process.once('SIGTERM',()=>{stopped=true;void context.close().catch(()=>{});});
process.once('SIGINT',()=>{stopped=true;void context.close().catch(()=>{});});
try{
 for(let count=0;count<maxPages;count++){
  if(stopped)break;
  const task=await api('next');
  if(task.state!=='ready'){console.log(JSON.stringify(task));break;}
  let response;
  try{response=await page.goto(task.url,{waitUntil:'domcontentloaded',timeout:45000});}
  catch(error){console.log(JSON.stringify(await api('fail',['--reason','browser_navigation_'+(error.message.match(/net::ERR_[A-Z_]+/)?.[0]||error.name)])));break;}
  const title=await page.title();
  if(response?.status()===403||/captcha|antibot|access denied/i.test(title)){
   console.log(JSON.stringify(await api('fail',['--reason','browser_access_challenge'])));break;
  }
  if(!response||!response.ok()){
   console.log(JSON.stringify(await api('fail',['--reason','browser_http_'+(response?.status()||'missing')])));break;
  }
  // Save the browser's actual page state, not an all-page link scrape.
  // The Python parser validates seller, page, real next cursor and product grid.
  let html;
  try {
   // Ozon may navigate again after the initial 200. A response handle then
   // loses its body; read the current rendered page after its state is present.
   await page.waitForFunction(() => document.querySelector('[id^="state-tileGridDesktop-"]') || document.documentElement.innerHTML.includes('window.__NUXT__.state=') || /captcha|antibot|access denied/i.test(document.title), undefined, {timeout:20000});
   html=await page.content();
   if(/captcha|antibot|access denied/i.test(await page.title()))throw Error('browser_access_challenge');
   if(count===0)await page.screenshot({path:path.join(out,`page-${task.page}.png`)});
  } catch(error) {
   console.log(JSON.stringify(await api('fail',['--reason',error.message==='browser_access_challenge'?error.message:'browser_page_state_unavailable'])));break;
  }
  const artifact=path.join(out,`page-${task.page}.html`);
  await fs.writeFile(artifact,html,{mode:0o600});
  if(stopped||(await api('next')).state!=='ready')break;
  const result=await api('ingest',['--url',task.url,'--html',artifact]);
  console.log(JSON.stringify(result));
  if(result.state!=='committed')break;
 }
}finally{await context.close();}
