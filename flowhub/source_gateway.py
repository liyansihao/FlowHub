"""Bounded persistent acquisition connections using the existing global ERP ledger."""

import asyncio
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

POOLS = {}


async def close():
    slots = [slot for _, slots in POOLS.values() for slot in slots]
    POOLS.clear()
    await asyncio.gather(*(slot.close() for slot in slots))


class Slot:
    process = None

    async def close(self):
        p, self.process = self.process, None
        if p and p.returncode is None:
            p.kill()
            await p.wait()


async def request(keys, path, method, query=None, body=None, *, proxy=None):
    from .source_detail import SourceAcquisitionFailure

    write = method != "GET" and path != "/api.chrome/sku3"
    token = keys["erp_token"]
    loop = asyncio.get_running_loop()
    key = (loop, hashlib.sha256(token.encode()).hexdigest(), proxy)
    if key not in POOLS:
        queue = asyncio.Queue()
        slots = [Slot() for _ in range(2)]
        for slot in slots:
            queue.put_nowait(slot)
        POOLS[key] = queue, slots
    queue, _ = POOLS[key]
    try:
        slot = queue.get_nowait()
    except asyncio.QueueEmpty:
        raise SourceAcquisitionFailure("POOL_BUSY", {"not_sent": True, "retry_after_ms": 1000}) from None
    began = time.monotonic()
    sent = False
    try:
        if not slot.process or slot.process.returncode is not None:
            environment = os.environ | {"MAOZI_ACCESS_TOKEN": token}
            flags = []
            if proxy:
                environment.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, NO_PROXY="")
                flags = ["--use-env-proxy"]
            slot.process = await asyncio.create_subprocess_exec(
                "node",
                *flags,
                str(Path(__file__).resolve().parents[1] / "bridges/acquisition-request.mjs"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=16 * 1024 * 1024,
                env=environment,
            )
        identity = secrets.token_hex(16)
        slot.process.stdin.write(
            (
                json.dumps({"id": identity, "path": path, "method": method, "query": query, "body": body})
                + "\n"
            ).encode()
        )
        sent = True
        await slot.process.stdin.drain()
        raw = await asyncio.wait_for(slot.process.stdout.readline(), 95)
        result = json.loads(raw)
        if result.get("id") != identity:
            raise ValueError("acquisition_response_identity_mismatch")
        if not result.get("ok"):
            raise SourceAcquisitionFailure(
                result["error"], result.get("diagnostic", {}) | {"timing": result.get("timing", {})}
            )
        return result["data"], result["timing"]
    except SourceAcquisitionFailure:
        raise
    except BaseException as error:
        await slot.close()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise SourceAcquisitionFailure(
            "BRIDGE_RESPONSE_UNKNOWN" if write and sent else "BRIDGE_READ_FAILURE",
            {
                "write_outcome_unknown": write and sent,
                "operation": path,
                "timing": {"total_ms": (time.monotonic() - began) * 1000},
                "exception": type(error).__name__,
            },
        ) from error
    finally:
        queue.put_nowait(slot)
