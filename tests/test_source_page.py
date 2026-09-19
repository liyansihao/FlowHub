import subprocess
from pathlib import Path


def test_browser_verification_settles_without_retrying_or_bypassing():
    module=(Path(__file__).resolve().parents[1]/'bridges/source-page.mjs').as_uri()
    script="""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {settleSourcePage} from '__MODULE__';
class Page extends EventEmitter {
  constructor(mode) { super();this.mode=mode;this.label='Antibot Challenge Page';this.waits=0; }
  mainFrame() { return this; }
  async title() { return this.label; }
  async waitForFunction(fn,arg,options) {
    assert.equal(options.timeout,30000);this.waits++;
    if(this.mode==='blocked') { const e=Error('still blocked');e.name='TimeoutError';throw e; }
    this.label='Product';
    if(this.mode==='iframe') this.emit('response',response(200,{}));
    else this.emit('response',response(200,this));
  }
}
function response(status,frame) { return {status:()=>status,request:()=>({isNavigationRequest:()=>true}),frame:()=>frame}; }
const normal=new Page('normal');normal.label='Product';const ok=response(200,normal);
assert.equal(await settleSourcePage(normal,ok),ok);assert.equal(normal.waits,0);
const recovered=new Page('recover');assert.equal((await settleSourcePage(recovered,response(403,recovered))).status(),200);
assert.equal(recovered.waits,1);assert.equal(recovered.listenerCount('response'),0);
for(const mode of ['blocked','iframe']) {
  const blocked=new Page(mode);
  await assert.rejects(settleSourcePage(blocked,response(403,blocked)),/browser_access_challenge/);
  assert.equal(blocked.listenerCount('response'),0);
}
""".replace('__MODULE__',module)
    subprocess.run(['node','--input-type=module','-e',script],check=True,capture_output=True,text=True)
