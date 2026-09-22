import asyncio
import json

import pytest

from flowhub import source_gateway as gateway
from flowhub.source_detail import SourceAcquisitionFailure


class Process:
    returncode = None

    def __init__(self, answer):
        self.stdin = self
        self.stdout = self
        self.answer = answer
        self.input = None

    def write(self, data):
        self.input = json.loads(data)

    async def drain(self):
        pass

    async def readline(self):
        return json.dumps({"id": self.input["id"], **self.answer}).encode() + b"\n"

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_connection_reused_and_credentials_never_in_request_body(monkeypatch):
    processes = []

    async def create(*args, **kwargs):
        p = Process({"ok": True, "data": {"result": 1}, "timing": {"network_ms": 5}})
        processes.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    try:
        for _ in range(3):
            result, timing = await gateway.request(
                {"erp_token": "synthetic-secret"}, "/api.product.collect/lists", "GET"
            )
            assert result == {"result": 1} and timing["network_ms"] == 5
        assert len(processes) == 2
        assert all("synthetic-secret" not in json.dumps(p.input) for p in processes)
    finally:
        await gateway.close()


async def test_rate_deferred_is_provably_not_sent(monkeypatch):
    async def create(*args, **kwargs):
        return Process(
            {
                "ok": False,
                "error": "MAOZI_API_PACING_WAIT",
                "diagnostic": {"not_sent": True, "retry_after_ms": 1000},
                "timing": {"network_ms": 0},
            }
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    try:
        with pytest.raises(SourceAcquisitionFailure) as error:
            await gateway.request(
                {"erp_token": "test"}, "/api.product.favorite/edit_import", "POST", body={"id": 1}
            )
        assert error.value.diagnostic["not_sent"]
    finally:
        await gateway.close()


async def test_lost_bridge_acknowledgement_marks_write_unknown(monkeypatch):
    async def create(*args, **kwargs):
        p = Process({})

        async def broken():
            return b""

        p.readline = broken
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    try:
        with pytest.raises(SourceAcquisitionFailure) as error:
            await gateway.request(
                {"erp_token": "test"}, "/api.product.favorite/edit_import", "POST", body={"id": 1}
            )
        assert error.value.diagnostic["write_outcome_unknown"]
    finally:
        await gateway.close()


async def test_explicit_proxy_is_kept_and_connection_scope_isolated(monkeypatch):
    created = []

    async def create(*args, **kwargs):
        created.append((args, kwargs["env"]))
        return Process({"ok": True, "data": {}, "timing": {}})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    try:
        await gateway.request(
            {"erp_token": "test"}, "/api.product.collect/lists", "GET", proxy="http://127.0.0.1:7897"
        )
        await gateway.request({"erp_token": "test"}, "/api.product.collect/lists", "GET")
        assert "--use-env-proxy" in created[0][0]
        assert created[0][1]["HTTPS_PROXY"] == "http://127.0.0.1:7897"
        assert "--use-env-proxy" not in created[1][0]
    finally:
        await gateway.close()
