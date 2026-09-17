"""Build a small baseline-checked Windows telemetry update without credentials."""
import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(output, baseline):
    source = ROOT / 'packaging/windows-agent'
    manifest = {'baseline': baseline, 'files': {}, 'check_only': {}}
    def previous(path):
        r = subprocess.run(['git', '-C', str(ROOT), 'show', baseline + ':' + path], capture_output=True)
        return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else None
    for name in ('production_agent.py', 'full_agent.py', 'compute_worker.py', 'runtime_identity.py'):
        manifest['files'][name] = {'old': previous('packaging/windows-agent/' + name),
            'new': hashlib.sha256((source / name).read_bytes()).hexdigest()}
    for name, path in [('agent.py', 'packaging/windows-agent/agent.py'), ('dossier_packet.py', 'flowhub/dossier_packet.py')]:
        manifest['check_only'][name] = previous(path)
        if manifest['check_only'][name] is None:
            raise ValueError('Missing supported baseline: ' + name)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in [*manifest['files'], 'upgrade_versions.py', 'Upgrade-Versions.cmd']:
            archive.write(source / name, name)
        archive.writestr('version-update.json', json.dumps(manifest, indent=2))
        archive.write(ROOT / 'docs/RUNTIME_VERSIONS_WINDOWS.md', 'README.md')
    return {'path': str(output), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', default='c5a1aa4617b2dccd8920bd1d4b830cc5b7c18eaa')
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.baseline)))
