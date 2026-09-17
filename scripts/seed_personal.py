"""Import this owner's account connections into an unused private personal workspace."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def seed(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists():
        raise ValueError('目标已存在，保留原数据，不重复导入')
    target.mkdir(parents=True, mode=0o700)
    shutil.copyfile(source / 'master.key', target / 'master.key')
    os.chmod(target / 'master.key', 0o600)
    from flowhub.db import Database
    db = Database(target)
    with sqlite3.connect((source / 'flowhub.sqlite3').as_uri() + '?mode=ro', uri=True) as src, db.connect() as dst:
        src.row_factory = sqlite3.Row
        src.execute('BEGIN')
        dst.execute('DELETE FROM workflows')
        dst.execute('DELETE FROM users')
        for table in ['users', 'stores', 'blocks', 'workflows']:
            columns = [r[1] for r in dst.execute('PRAGMA table_info(' + table + ')')]
            source_columns = {r[1] for r in src.execute('PRAGMA table_info(' + table + ')')}
            columns = [c for c in columns if c in source_columns]
            for row in src.execute('SELECT ' + ','.join(columns) + ' FROM ' + table):
                values = dict(row)
                if table == 'workflows':
                    values.update(enabled=0,cursor='',active_store=None,last_fetch=0,notice='个人版 54fbf19 已配置；勾选店铺后启动')
                    rules=json.loads(values['rules']);rules.update(max_items=100,max_publications=3);values['rules']=json.dumps(rules)
                dst.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', [values[c] for c in columns])
        for row in src.execute('SELECT id,kind,name,driver,endpoint,enabled FROM modules'):
            dst.execute('INSERT OR IGNORE INTO modules VALUES(?,?,?,?,?,?)', tuple(row))
        dst.execute("UPDATE modules SET driver='comparebot',name='compareBot 1688 同款' WHERE id='flowb-matcher'")
        # Existing candidates stay owned by the original workspace, never replay them in this one.
        for row in src.execute('SELECT owner,source_key FROM jobs'):
            dst.execute('INSERT OR IGNORE INTO blocks VALUES(?,?,?)', (row[0],row[1],'原工作台已有任务，个人版排除重复处理'))
        counts = {t:dst.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['users','stores','blocks']}
    (target / 'INITIAL_ADMIN.txt').unlink(missing_ok=True)
    print(json.dumps({'imported':counts,'jobs':0,'enabled':False}, ensure_ascii=False))


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--target',required=True);a=p.parse_args();seed(a.source,a.target)
