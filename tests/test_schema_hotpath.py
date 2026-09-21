from concurrent.futures import ThreadPoolExecutor

from flowhub.browser_source import BrowserSource
from flowhub.db import Database
from flowhub.pipeline_modules import control
from flowhub.source_library import SourceLibrary


def test_schema_once_is_per_database_and_idempotent(tmp_path):
    db = Database(tmp_path)
    calls = []

    db.schema_once("test", lambda: calls.append("first"))
    db.schema_once("test", lambda: calls.append("second"))

    assert calls == ["first"]
    assert db._journal_configured is True


def test_hot_path_schema_initializers_are_safe_under_concurrency(tmp_path):
    db = Database(tmp_path)

    def initialize():
        control.schema(db)
        SourceLibrary(db)
        BrowserSource(db)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: initialize(), range(12)))

    with db.connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_module_control'").fetchone()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='sourcing_products'").fetchone()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='browser_source_scans'").fetchone()
