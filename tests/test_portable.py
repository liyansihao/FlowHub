from pathlib import Path

from flowhub import portable
from flowhub.db import Database


def test_portable_bootstrap_preserves_runtime_and_registers_modules(tmp_path, monkeypatch):
    db = Database(tmp_path / "app")
    monkeypatch.setattr(portable, "Database", lambda: db)
    seed = tmp_path / "seed"
    target = tmp_path / "runtime"
    (seed / "x/state").mkdir(parents=True)
    (seed / "x/state/config.json").write_text('{"stock":99}')
    (seed / "x/main.mjs").write_text("new code")
    (seed / "node_modules").mkdir()
    (target / "x/state").mkdir(parents=True)
    (target / "x/state/config.json").write_text('{"stock":7}')
    portable.prepare(seed, target)
    portable.prepare(seed, target)
    assert (target / "x/state/config.json").read_text() == '{"stock":7}'
    assert (target / "x/main.mjs").read_text() == "new code"
    with db.connect() as c:
        assert c.execute("select count(*) from modules where driver='flowb'").fetchone()[0] == 3
        assert c.execute("select count(*) from stores").fetchone()[0] == 0
        assert c.execute("select count(*) from workflows where enabled=1").fetchone()[0] == 0


def test_windows_local_port_and_private_volume_contract():
    root = Path(__file__).resolve().parents[1] / "packaging/windows"
    compose = (root / "compose.yaml").read_text()
    assert "127.0.0.1:38427:38427" in compose
    assert "flowhub_data:/data" in compose
    assert "restart: unless-stopped" in compose
    script = (root / "FlowHub.ps1").read_text()
    assert "compose stop" in script and "-C /data ." in script
    assert "down -v" not in script
