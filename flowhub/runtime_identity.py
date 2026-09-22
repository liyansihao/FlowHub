"""Portable, stdlib-only startup fingerprints. Never read credentials or task data.

These describe files observed at startup, not a cryptographic proof of every
loaded module. Keep the captured object for the life of its process.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import time
import uuid
from pathlib import Path


def digest_files(files):
    entries = {}
    for label, path in sorted(files.items()):
        try:
            h = hashlib.sha256()
            with Path(path).open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(chunk)
            entries[label] = h.hexdigest()
        except OSError:
            entries[label] = 'unavailable'
    encoded = json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest(), len(entries), 'unavailable' not in entries.values()


def source_files(root):
    root = Path(root)
    files = {}
    # Explicit source directories only: no data, browser profiles, logs or .env.
    if (root / 'flowhub').is_dir():
        for directory in ('flowhub', 'bridges', 'vendor/compareBot/src'):
            base = root / directory
            for path in base.rglob('*'):
                if path.is_file() and not path.is_symlink() and path.suffix in ('.py', '.mjs', '.js', '.json') and '__pycache__' not in path.parts:
                    files[str(path.relative_to(root)).replace('\\', '/')] = path
    else:
        for name in ('agent.py', 'production_agent.py', 'full_agent.py', 'compute_worker.py',
                     'dossier_packet.py', 'runtime_identity.py'):
            if (root / name).is_file():
                files[name] = root / name
    return files


def capture(component, root, protocols=None):
    root = Path(root)
    source_hash, count, complete = digest_files(source_files(root))
    versions = sorted((d.metadata.get('Name', ''), d.version) for d in importlib.metadata.distributions())
    deps = hashlib.sha256(json.dumps(versions, separators=(',', ':')).encode()).hexdigest()
    locks = {name: root / name for name in ('requirements.lock.txt', 'requirements-tested.txt', 'package-lock.json') if (root / name).is_file()}
    revision = None
    dirty = None
    if (root / '.git').exists():
        try:
            revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], timeout=3, stderr=subprocess.DEVNULL, text=True).strip()
            dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal'], timeout=3, stderr=subprocess.DEVNULL, text=True).strip())
        except (OSError, subprocess.SubprocessError):
            revision = None
    package_hash = None
    external_hash = None
    if (root / 'flowhub').is_dir():
        external = {}
        for relative in ('FlowEF-production/src', 'FlowEF-production/bridges', 'ozon-runtime/lib', 'maozi_direct_new_method/lib', 'flow_b_ef/lib'):
            for path in (root.parent / relative).rglob('*'):
                if path.is_file() and not path.is_symlink() and path.suffix in ('.py', '.mjs', '.js') and '__pycache__' not in path.parts:
                    external[str(path.relative_to(root.parent))] = path
        for name in ('maozi_direct_new_method/maozi_new_method_direct.mjs',):
            if (root.parent / name).is_file():
                external[name] = root.parent / name
        if external:
            external_hash = digest_files(external)[0]
    try:
        spec = importlib.util.find_spec('comparebot')
        if spec and spec.submodule_search_locations:
            base = Path(next(iter(spec.submodule_search_locations)))
            package_hash = digest_files({str(p.relative_to(base)).replace('\\', '/'): p for p in base.rglob('*.py') if '__pycache__' not in p.parts})[0]
    except (ImportError, ValueError, OSError):
        pass
    return dict(schema=1, component=component, instance_id=uuid.uuid4().hex,
                pid=os.getpid(), started_at=time.time(), source_revision=revision,
                source_dirty=dirty, source_sha256=source_hash, source_files=count,
                source_complete=complete, dependency_sha256=deps,
                lock_sha256=digest_files(locks)[0] if locks else None,
                comparebot_sha256=package_hash, external_sha256=external_hash, python=platform.python_version(),
                protocols=protocols or {}, model_name=None, model_revision=None,
                evidence='startup_disk_snapshot')


def save(directory, identity):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('runtime-' + identity['component'] + '.json')
    temporary = path.with_suffix('.' + identity['instance_id'] + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        if os.name != 'nt':
            os.chmod(temporary, 0o600)
        json.dump({'identity': identity, 'observed_at': time.time()}, stream)
    os.replace(temporary, path)


def report_loop(client, identity, stop):
    """Optional telemetry, separate from task leases and capability heartbeats."""
    while not stop.is_set():
        try:
            client.call('/v1/runtime', identity)
        except Exception:
            # Old coordinators may return 404; reporting never stops business work.
            pass
        stop.wait(30)
