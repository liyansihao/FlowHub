import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { sourceBrowserOptions } from '../bridges/source-browser.mjs';

async function extensionFixture() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'flowhub-source-extension-'));
  await fs.writeFile(path.join(dir, 'manifest.json'), JSON.stringify({
    manifest_version: 3,
    name: 'fixture',
    version: '1.0.0',
  }));
  return dir;
}

test('source browser mirrors ozon-playwright launch fingerprint and extension loading', async () => {
  const extension = await extensionFixture();
  const options = sourceBrowserOptions({
    FLOWHUB_SOURCE_PROFILE: '/tmp/flowhub-profile',
    FLOWHUB_SOURCE_EXTENSION_DIR: extension,
  });
  assert.equal(options.channel, 'chrome');
  assert.equal(options.headless, false);
  assert.equal(options.viewport, null);
  assert.equal(options.args.includes('--disable-blink-features=AutomationControlled'), true);
  assert.deepEqual(options.args.slice(-2), [
    `--disable-extensions-except=${extension}`,
    `--load-extension=${extension}`,
  ]);
  assert.deepEqual(options.ignoreDefaultArgs, ['--disable-extensions']);
});

test('source browser preserves installed extensions when no unpacked extension is configured', () => {
  const options = sourceBrowserOptions({ FLOWHUB_SOURCE_PROFILE: '/tmp/flowhub-profile' });
  assert.equal(options.args.some(arg => arg.startsWith('--load-extension=')), false);
  assert.deepEqual(options.ignoreDefaultArgs, ['--disable-extensions']);
});

test('source browser accepts an explicit Chromium executable', () => {
  const options = sourceBrowserOptions({
    FLOWHUB_SOURCE_PROFILE: '/tmp/flowhub-profile',
    FLOWHUB_SOURCE_CHROMIUM_EXECUTABLE: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  assert.equal(options.executablePath, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
  assert.equal('channel' in options, false);
});
