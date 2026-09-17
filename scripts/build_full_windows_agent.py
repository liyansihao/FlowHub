"""Package portable source and the already verified pinned model; no credentials."""
import hashlib
import json
import zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
source=root/'packaging/windows-agent'
out=root/'output/FlowHub-Windows-Full-Agent-v3.zip'
files=['agent.py','production_agent.py','full_agent.py','compute_worker.py',
       'Upgrade-Full.ps1','Upgrade-Full.cmd','Start-Full.cmd','requirements-tested.txt','FULL-README.md']
revision='ed25f3a31f01632728cabb09d1542f84ab7b0056'
model=Path.home()/'.cache/huggingface/hub/models--facebook--dinov2-small/snapshots'/revision
checks={}
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
    for name in files:z.write(source/name,name)
    z.write(root/'flowhub/dossier_packet.py','dossier_packet.py')
    vendor=root/'vendor/compareBot'
    for p in (vendor/'src').rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.html','.css','.js'):
            z.write(p,'compareBot/'+str(p.relative_to(vendor)))
    z.write(vendor/'pyproject.toml','compareBot/pyproject.toml')
    for name in ('LICENSE','LICENSE.txt','README.md'):
        if (vendor/name).is_file():z.write(vendor/name,'compareBot/'+name)
    for name in ('config.json','preprocessor_config.json','model.safetensors'):
        p=model/name
        if not p.is_file():raise RuntimeError('Pinned model cache missing: '+name)
        checks[name]=hashlib.sha256(p.read_bytes()).hexdigest()
        # Dereference Mac symlinks: ordinary files work on Windows without admin.
        z.write(p,'model-cache/hub/models--facebook--dinov2-small/snapshots/'+revision+'/'+name)
    z.writestr('model-sha256.json',json.dumps({'revision':revision,'files':checks},indent=2))
print(json.dumps({'path':str(out),'bytes':out.stat().st_size,'sha256':hashlib.sha256(out.read_bytes()).hexdigest()}))
