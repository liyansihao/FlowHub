"""Install login recovery and a five-minute local monitor, without stopping live work."""
import argparse
import os
import plistlib
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(); data = root / 'data'; python = root / '.venv/bin/python'
    if not (data / 'flowhub.sqlite3').is_file() or not python.is_file():
        raise SystemExit('Existing production installation required')
    logs = data / 'stability'; logs.mkdir(exist_ok=True, mode=0o700)
    folder = Path.home() / 'Library/LaunchAgents'
    environment = {'FLOWHUB_DATA': str(data), 'FLOWHUB_WORKER_ONLY': '1',
                   'PATH': '/Users/mac/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'}
    jobs = {
        'com.flowhub.production': {'ProgramArguments': ['/usr/bin/caffeinate', '-is', str(python), '-m', 'flowhub.service_entry'],
                                  'KeepAlive': True, 'ThrottleInterval': 30},
        'com.flowhub.stability-monitor': {'ProgramArguments': [str(python), '-m', 'flowhub.stability_monitor', '--data', str(data), '--output', str(logs)],
                                         'StartInterval': 300},
    }
    for label, fields in jobs.items():
        job = {'Label': label, 'WorkingDirectory': str(root), 'EnvironmentVariables': environment,
               'RunAtLoad': True, 'StandardOutPath': str(logs / (label + '.log')),
               'StandardErrorPath': str(logs / (label + '.error.log')), **fields}
        path = folder / (label + '.plist')
        if path.exists() and plistlib.loads(path.read_bytes()) != job:
            raise SystemExit('Existing service differs: ' + label)
        path.write_bytes(plistlib.dumps(job)); path.chmod(0o600)
        if subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
        print(label + ' installed')


if __name__ == '__main__':
    main()
