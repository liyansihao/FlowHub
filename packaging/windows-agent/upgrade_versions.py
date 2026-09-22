"""Install only version-reporting code after all existing agents have stopped.

Keeps device credentials, command ledgers, pending results, models and venv intact.
"""
import argparse
import hashlib
import json
import os
import shutil
import time
from contextlib import ExitStack
from pathlib import Path

FILES = ('production_agent.py', 'full_agent.py', 'compute_worker.py', 'runtime_identity.py')
CHECK_ONLY = ('agent.py', 'dossier_packet.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def acquire(root, stack):
    for name in ('device.lock', 'device.compute.lock'):
        handle = stack.enter_context((root / name).open('a+b'))
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def upgrade(root, bundle):
    root, bundle = Path(root), Path(bundle)
    manifest = json.loads((bundle / 'version-update.json').read_text(encoding='utf-8'))
    if set(manifest['files']) != set(FILES) or set(manifest['check_only']) != set(CHECK_ONLY):
        raise ValueError('Invalid bundle file list')
    if not (root / 'device.json').is_file():
        raise ValueError('Existing enrolled agent required')
    with ExitStack() as stack:
        acquire(root, stack)  # Refuses active workers instead of terminating them.
        for name in FILES:
            entry = manifest['files'][name]
            if sha(bundle / name) != entry['new']:
                raise ValueError('Bundle checksum mismatch: ' + name)
            if sha(root / name) not in (entry['old'], entry['new']):
                raise ValueError('Installed code differs from supported baseline: ' + name)
        for name in CHECK_ONLY:
            if sha(root / name) != manifest['check_only'][name]:
                raise ValueError('Installed dependency differs from supported baseline: ' + name)
        if all(sha(root / name) == manifest['files'][name]['new'] for name in FILES):
            return {'ok': True, 'already_installed': True}
        backup = root / 'version-backups' / str(time.time_ns())
        backup.mkdir(parents=True)
        existed = {name: (root / name).exists() for name in FILES}
        for name in FILES:
            if existed[name]:
                shutil.copy2(root / name, backup / name)
        try:
            for name in FILES:
                temporary = root / (name + '.version-update.tmp')
                shutil.copyfile(bundle / name, temporary)
                os.replace(temporary, root / name)
        except Exception:
            for name in FILES:
                if existed[name]:
                    shutil.copy2(backup / name, root / name)
                elif (root / name).exists():
                    (root / name).unlink()
            raise
        return {'ok': True, 'backup': str(backup), 'restart': 'Start-Full.cmd'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(upgrade(args.root, Path(__file__).resolve().parent)))
    except Exception as error:
        raise SystemExit('Upgrade not completed: ' + str(error))
