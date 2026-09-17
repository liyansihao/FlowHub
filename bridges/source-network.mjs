// Network selection is scoped to the dedicated source browser, not macOS.
import fs from 'node:fs';
import path from 'node:path';

export function sourceNetworkArgs() {
  const data = process.env.FLOWHUB_DATA || path.resolve(import.meta.dirname, '../data');
  const file = path.join(data, 'source-loop.json');
  const mode = fs.existsSync(file)
    ? JSON.parse(fs.readFileSync(file, 'utf8')).browser_network || 'system'
    : 'system';
  if (mode === 'system') return [];
  if (mode === 'direct') return ['--no-proxy-server'];
  throw Error('invalid_source_browser_network');
}
