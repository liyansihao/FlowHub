// Read one product page. No cart, ERP, collection or publication writes.
import {chromium} from 'playwright';
import {sourceBrowserOptions} from './source-browser.mjs';
import {sourceNetworkArgs} from './source-network.mjs';
import {settleSourcePage} from './source-page.mjs';

const [sku] = process.argv.slice(2);
if (!/^[1-9][0-9]*$/.test(sku || '')) throw Error('numeric SKU required');
const options = sourceBrowserOptions();
options.args.push(...sourceNetworkArgs());
let context;
const close = () => context?.close().catch(() => {});
process.once('SIGTERM', () => { void close(); });
process.once('SIGINT', () => { void close(); });
try {
  context = await chromium.launchPersistentContext(options.profile, options);
  const page = await context.newPage();
  let response = await page.goto(`https://www.ozon.ru/product/${sku}/`,
    {waitUntil: 'domcontentloaded', timeout: 20000});
  response = await settleSourcePage(page, response, 20000);
  if (!response?.ok()) throw Error('browser_http_' + (response?.status() || 'missing'));
  await page.locator('[id^="state-webProductMainWidget-"]').waitFor({state: 'attached', timeout: 10000});
  // Reading the rendered state allows the site's own verification/navigation to settle.
  const html = await page.content();
  console.log(JSON.stringify({html, url: page.url(), observed_at: Date.now() / 1000}));
} catch (error) {
  console.log(JSON.stringify({error: error.message === 'browser_access_challenge'
    || /^browser_http_\d+$/.test(error.message) ? error.message : error.name}));
  process.exitCode = 1;
} finally {
  await close();
}
