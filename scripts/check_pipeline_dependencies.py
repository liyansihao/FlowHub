"""Read-only verification of the local external bridge baseline; no credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path

root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--legacy-root',type=Path,default=Path(os.environ.get('FLOWHUB_LEGACY_ROOT',root.parent)))
a=p.parse_args()
manifest=json.loads((root/'deploy/pipeline-external-baseline.json').read_text())
results=[]
for name,digest in manifest['files'].items():
    file=a.legacy_root/name
    state='missing' if not file.is_file() else 'match' if hashlib.sha256(file.read_bytes()).hexdigest()==digest else 'changed'
    results.append({'file':name,'state':state})
print(json.dumps({'baseline':manifest['description'],'results':results},indent=2))
raise SystemExit(0 if all(r['state']=='match' for r in results) else 1)
