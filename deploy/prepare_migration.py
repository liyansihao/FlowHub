"""Create a private, consistent cutover snapshot without mutating the live source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


def prepare(source, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    if output.exists():
        raise ValueError('目标已存在，拒绝覆盖')
    db_path, key_path = source / 'flowhub.sqlite3', source / 'master.key'
    if not db_path.is_file() or not key_path.is_file():
        raise ValueError('需要完整的数据库和 master.key')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.flowhub-migration-', dir=output.parent))
    try:
        with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(staging / 'flowhub.sqlite3') as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库完整性检查未通过')
                if dst.execute('SELECT 1 FROM workflows WHERE enabled=1').fetchone():
                    raise ValueError('请先暂停原服务新增，再制作最终迁移快照')
                if dst.execute("SELECT 1 FROM jobs WHERE phase IN ('publishing','reconciling','stock_ready','stock_pending','checking') OR lease_until>strftime('%s','now')").fetchone():
                    raise ValueError('原服务仍有在途操作，完成回查后再迁移')
                counts = {t: dst.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in ['users', 'stores', 'jobs']}
                # Destination sessions must be newly authenticated. Keep account hashes and encrypted store keys.
                for table in ['sessions', 'login_attempts', 'health', 'health_samples']:
                    dst.execute('DELETE FROM ' + table)
                dst.execute('UPDATE jobs SET lease=NULL,lease_until=0')
                dst.execute("UPDATE workflows SET enabled=0,notice='账号数据已迁移；执行服务核验后可启动'")
                dst.commit()
        shutil.copyfile(key_path, staging / 'master.key')
        for name in ['flowhub.sqlite3', 'master.key']:
            os.chmod(staging / name, 0o600)
        manifest = {'counts': counts, 'worker_enabled': False, 'files': {
            name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
            for name in ['flowhub.sqlite3', 'master.key']
        }}
        (staging / 'migration.json').write_text(json.dumps(manifest, indent=2) + '\n')
        os.chmod(staging / 'migration.json', 0o600)
        if output.exists():
            raise ValueError('目标已存在，拒绝覆盖')
        staging.rename(output)
        return manifest
    except BaseException:
        shutil.rmtree(staging)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.source, args.output), ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error) + '\n')
