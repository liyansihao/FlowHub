import fs from 'node:fs';
import path from 'node:path';

function requiredPath(value, name) {
  const normalized = String(value || '').trim();
  if (!normalized) throw new Error(`${name} is required`);
  return path.resolve(normalized);
}

function validateExtension(extensionDir) {
  const manifestPath = path.join(extensionDir, 'manifest.json');
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  } catch (error) {
    throw new Error(`source browser extension manifest is unavailable: ${manifestPath}: ${error.message}`);
  }
  if (!manifest || !manifest.manifest_version) {
    throw new Error(`source browser extension manifest is invalid: ${manifestPath}`);
  }
}

/**
 * Build the persistent browser options used by both source collectors.
 * The anti-automation flag and optional unpacked extension mirror the
 * ozon-playwright launch contract. No extension is guessed or
 * downloaded: production must opt in with an explicit local directory.
 */
export function sourceBrowserOptions(env = process.env) {
  const profile = requiredPath(env.FLOWHUB_SOURCE_PROFILE, 'FLOWHUB_SOURCE_PROFILE');
  const extensionValue = String(env.FLOWHUB_SOURCE_EXTENSION_DIR || '').trim();
  const args = [
    '--disable-blink-features=AutomationControlled',
    '--no-first-run',
    '--no-default-browser-check',
  ];
  const options = { profile, headless: false, viewport: null, args, ignoreDefaultArgs: ['--disable-extensions'] };
  const executable = String(env.FLOWHUB_SOURCE_CHROMIUM_EXECUTABLE || '').trim();
  if (executable) options.executablePath = requiredPath(executable, 'FLOWHUB_SOURCE_CHROMIUM_EXECUTABLE');
  else options.channel = 'chrome';
  if (extensionValue) {
    const extensionDir = requiredPath(extensionValue, 'FLOWHUB_SOURCE_EXTENSION_DIR');
    validateExtension(extensionDir);
    options.args.push(`--disable-extensions-except=${extensionDir}`, `--load-extension=${extensionDir}`);
    options.ignoreDefaultArgs = ['--disable-extensions'];
  }
  return options;
}
