import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("FLOWHUB_DATA", ROOT / "data"))
DEFAULT_RULES = dict(
    profit_min=30, image_min=70, dhash_min=55, stock=99, logistics="ChinaPost", interval=30, live=False
)
DEFAULT_MODULES = {k: f"demo-{k}" for k in ("candidates", "matcher", "profit", "publisher")}


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ":" + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def password_ok(password, hashed):
    return secrets.compare_digest(password_hash(password, hashed.split(":")[0]), hashed)


def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


class Database:
    def __init__(self, directory=DATA):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "flowhub.sqlite3"
        self._schema_ready = set()
        self._schema_lock = threading.RLock()
        self._journal_configured = False
        self._journal_lock = threading.Lock()
        key = self.directory / "master.key"
        if not key.exists():
            private_write(key, Fernet.generate_key().decode())
        self.cipher = Fernet(key.read_bytes())
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,must_change INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_attempts(bucket TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,config TEXT NOT NULL,secret TEXT NOT NULL,enabled INTEGER DEFAULT 1,verified INTEGER DEFAULT 0,position INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS workflows(owner TEXT PRIMARY KEY,enabled INTEGER DEFAULT 0,rules TEXT NOT NULL,modules TEXT NOT NULL,secrets TEXT NOT NULL,cursor TEXT DEFAULT '',active_store TEXT,updated REAL NOT NULL,last_fetch REAL DEFAULT 0,notice TEXT DEFAULT '尚未启动');
            CREATE TABLE IF NOT EXISTS modules(id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,driver TEXT NOT NULL,endpoint TEXT NOT NULL DEFAULT '',enabled INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,source_key TEXT NOT NULL,store_id TEXT,phase TEXT NOT NULL,data TEXT NOT NULL,plan TEXT,modules TEXT NOT NULL,attempts INTEGER DEFAULT 0,next_at REAL NOT NULL,lease TEXT,lease_until REAL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,note TEXT NOT NULL DEFAULT '',UNIQUE(owner,source_key));
            CREATE TABLE IF NOT EXISTS blocks(owner TEXT NOT NULL,source_key TEXT NOT NULL,reason TEXT,PRIMARY KEY(owner,source_key));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,owner TEXT NOT NULL,job_id TEXT,code TEXT NOT NULL,message TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS health(name TEXT PRIMARY KEY,pid INTEGER,heartbeat REAL,started REAL,processed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS demo_effects(id TEXT PRIMARY KEY,owner TEXT NOT NULL,stock INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS health_samples(at REAL PRIMARY KEY,pid INTEGER NOT NULL,processed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS candidate_pages(owner TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ozon_direct_writes(offer_id TEXT PRIMARY KEY,owner TEXT NOT NULL,binding_hash TEXT NOT NULL,payload_hash TEXT NOT NULL,task_id TEXT,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS quotas(store_id TEXT PRIMARY KEY,remaining INTEGER NOT NULL,reset_at REAL NOT NULL,observed REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(phase,next_at,lease_until);
            CREATE INDEX IF NOT EXISTS events_owner ON events(owner,created);
            """)
            if "selected_store_ids" not in {r[1] for r in db.execute("PRAGMA table_info(workflows)")}:
                db.execute("ALTER TABLE workflows ADD COLUMN selected_store_ids TEXT")
            for kind, title in [
                ("candidates", "示例候选池"),
                ("matcher", "示例同款识别"),
                ("profit", "示例利润计算"),
                ("publisher", "模拟上架"),
            ]:
                db.execute(
                    "INSERT OR IGNORE INTO modules(id,kind,name,driver) VALUES(?,?,?,?)",
                    (f"demo-{kind}", kind, title, "demo"),
                )
            db.execute(
                "INSERT OR IGNORE INTO modules(id,kind,name,driver) VALUES('source-library-candidates','candidates','商品来源库（初筛与待核查）','source-library')"
            )
            db.execute(
                "INSERT OR IGNORE INTO modules(id,kind,name,driver) VALUES('maozi-publisher','publisher','毛子ERP + Ozon 回查','maozi')"
            )
            db.execute(
                "INSERT OR IGNORE INTO modules(id,kind,name,driver) VALUES('ozon-direct-publisher','publisher','Ozon 官方直发（本地商品资料）','ozon-direct')"
            )
            db.execute(
                "INSERT OR IGNORE INTO modules(id,kind,name,driver) VALUES('flowb-matcher','matcher','compareBot 1688 同款','comparebot')"
            )
            db.execute(
                "UPDATE modules SET name='compareBot 1688 同款',driver='comparebot' WHERE id='flowb-matcher' AND driver='flowb'"
            )
            for provider, title in [("ChinaPost", "邮政本地利润"), ("GUOO", "GUOO 本地利润")]:
                db.execute(
                    "INSERT OR IGNORE INTO modules(id,kind,name,driver,endpoint) VALUES(?,?,?,?,?)",
                    ("local-profit-" + provider.lower(), "profit", title, "local-profit", provider),
                )
            if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                password = secrets.token_urlsafe(18)
                self.create_user(db, "admin", password, "admin")
                private_write(
                    self.directory / "INITIAL_ADMIN.txt",
                    f"网址：http://127.0.0.1:38427\n账号：admin\n临时密码：{password}\n首次登录必须修改密码。此文件仅保存在本机，请勿分享。\n",
                )
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        # WAL mode is a persistent database property. Re-applying it on every
        # short-lived connection can itself contend with writers and was a
        # source of long claim stalls under concurrent lanes.
        if not self._journal_configured:
            with self._journal_lock:
                if not self._journal_configured:
                    db.execute("PRAGMA journal_mode=WAL")
                    self._journal_configured = True
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def schema_once(self, name, initializer):
        """Run a module schema initializer once per Database instance.

        Schema setup belongs to startup or an explicit migration, not to every
        claim/admission tick. Keeping the cache on the Database instance avoids
        global state leaking between test databases and production workers.
        """
        with self._schema_lock:
            if name in self._schema_ready:
                return
            initializer()
            self._schema_ready.add(name)

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def open(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def create_user(self, db, username, password, role="user"):
        user = secrets.token_hex(12)
        db.execute(
            "INSERT INTO users(id,username,password,role,created) VALUES(?,?,?,?,?)",
            (user, username, password_hash(password), role, time.time()),
        )
        db.execute(
            "INSERT INTO workflows(owner,rules,modules,secrets,updated) VALUES(?,?,?,?,?)",
            (user, json.dumps(DEFAULT_RULES), json.dumps(DEFAULT_MODULES), self.seal({}), time.time()),
        )
        return user

    def event(self, db, owner, code, message, job=None):
        # Call sites supply controlled messages, never external error text or credentials.
        db.execute(
            "INSERT INTO events(owner,job_id,code,message,created) VALUES(?,?,?,?,?)",
            (owner, job, code, message, time.time()),
        )

    def health(self, name, processed=0):
        with self.connect() as db:
            if name == "worker":
                last = db.execute("SELECT MAX(at) FROM health_samples").fetchone()[0] or 0
                if time.time() - last >= 30:
                    db.execute(
                        "INSERT INTO health_samples VALUES(?,?,?)", (time.time(), os.getpid(), processed)
                    )
            db.execute(
                "INSERT INTO health VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET pid=excluded.pid,heartbeat=excluded.heartbeat,processed=health.processed+excluded.processed",
                (name, os.getpid(), time.time(), time.time(), processed),
            )
