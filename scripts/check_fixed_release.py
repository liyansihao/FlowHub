"""Read-only verification of the 2026-09-17 source baseline. Never loads runtime data."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-root', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    legacy = args.legacy_root or root.parent
    results = []
    for name, directory in [('runtime-source-baseline-20260917.json', root),
                            ('runtime-external-source-20260917.json', legacy)]:
        manifest = json.loads((root / 'deploy' / name).read_text())
        differences = []
        for relative, digest in manifest['files'].items():
            file = directory / relative
            if not file.is_file():
                differences.append({'file': relative, 'state': 'missing'})
            elif hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                differences.append({'file': relative, 'state': 'changed'})
        results.append({'manifest': name, 'checked': len(manifest['files']), 'differences': differences})
    print(json.dumps({'ok': all(not r['differences'] for r in results), 'results': results}, indent=2))
    return int(any(r['differences'] for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
