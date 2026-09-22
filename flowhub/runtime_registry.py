"""Device-bound version reports; no task execution or remote update commands."""
import json
from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class RuntimeReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    report_schema: Literal[1] = Field(alias='schema')
    component: Literal['erp-agent', 'full-agent', 'compute-worker']
    instance_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    pid: int = Field(ge=1)
    started_at: float = Field(gt=0, allow_inf_nan=False)
    source_revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{40,64}$')
    source_dirty: bool | None = None
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_files: int = Field(ge=0, le=100000)
    source_complete: bool
    dependency_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    lock_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    comparebot_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    external_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    python: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    protocols: dict[Literal['acceptance', 'erp', 'compute'], int] = Field(max_length=3)
    model_name: str | None = Field(default=None, max_length=100, pattern=r'^[A-Za-z0-9_./-]+$')
    model_revision: str | None = Field(default=None, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$')
    evidence: Literal['startup_disk_snapshot']


def install_routes(app, hub, bearer):
    with hub.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS runtime_reports(
            device TEXT,component TEXT,instance_id TEXT,body TEXT,seen REAL,
            PRIMARY KEY(device,component,instance_id))''')

    @app.post('/v1/runtime')
    def runtime(body: RuntimeReport, authorization: str | None = Header(default=None)):
        with hub.connect() as c:
            # Report identity comes exclusively from the existing device credential.
            from .cluster import digest
            token = bearer(authorization)
            row = c.execute('SELECT id FROM devices WHERE token_hash=? AND enabled=1', (digest(token),)).fetchone()
            if row is None:
                raise HTTPException(401, 'Device not authorized')
            device = row[0]
            encoded = json.dumps(body.model_dump(by_alias=True), sort_keys=True)
            prior = c.execute('SELECT body FROM runtime_reports WHERE device=? AND component=? AND instance_id=?',
                              (device, body.component, body.instance_id)).fetchone()
            if prior and prior[0] != encoded:
                raise HTTPException(409, 'Startup identity cannot change within one process instance')
            c.execute('INSERT OR REPLACE INTO runtime_reports VALUES(?,?,?,?,?)',
                      (device, body.component, body.instance_id, encoded, hub.clock()))
            # Bounded historical processes per device/component. Current reports stay.
            c.execute('''DELETE FROM runtime_reports WHERE device=? AND component=? AND instance_id NOT IN
                (SELECT instance_id FROM runtime_reports WHERE device=? AND component=? ORDER BY seen DESC LIMIT 8)''',
                (device, body.component, device, body.component))
        return {'ok': True}
