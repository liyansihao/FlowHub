// Attach to an explicitly configured existing Playwright CLI daemon; never launch a browser.
const fs=require('fs');
(async()=>{
 let input='';for await(const chunk of process.stdin)input+=chunk;
 const c=JSON.parse(input);if(!/^\d+$/.test(c.sku))throw Error('numeric SKU required');
 if(!fs.statSync(c.socketPath).isSocket())throw Error('existing session unavailable');
 const {Session}=require(c.cliLib+'/tools/cli-client/session.js');
 const {createClientInfo}=require(c.cliLib+'/tools/cli-client/registry.js');const info=createClientInfo();
 const session=new Session({config:{name:c.session,version:info.version,socketPath:c.socketPath}});
 const code=`async page => {
  const p=await page.context().newPage();
  try {
   const initial=await p.goto('https://www.ozon.ru/product/${c.sku}/',{waitUntil:'domcontentloaded',timeout:35000});
   await p.locator('[data-widget="webProductMainWidget"]').waitFor({state:'attached',timeout:20000});
   await p.waitForTimeout(4000);
   const description=p.getByTitle('Перейти к описанию',{exact:true});
   if(await description.count())await description.first().click({timeout:3000}).catch(()=>{});
   for(let i=0;i<5;i++){await p.evaluate(()=>window.scrollBy(0,900));await p.waitForTimeout(500);}
   return {initial_status:initial?.status(),url:p.url(),title:await p.title(),html:await p.content()};
  } finally {await p.close();}
 }`;
 const raw=await session.run(info,{_:['run-code',code]},{json:true});
 const envelope=JSON.parse(raw.text);if(!envelope.result)throw Error('session did not return page');
 const result=typeof envelope.result==='string'?JSON.parse(envelope.result):envelope.result;
 if(!result.html||!result.url.startsWith('https://www.ozon.ru/'))throw Error('invalid product document');
 process.stdout.write(JSON.stringify(result));
})().catch(error=>{process.stdout.write(JSON.stringify({error:error.name||'session_error'}));process.exitCode=1;});
