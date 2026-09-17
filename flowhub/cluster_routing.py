"""Central-owned Windows routing; the normal production lane remains owner."""
import json
import os
from pathlib import Path


def route_bridge(bridge, directory, key):
    config = Path(directory) / 'cluster' / 'production-routing.json'
    if not config.exists():
        return
    policy = json.loads(config.read_text())
    from .cluster_dispatch import device_for
    selected=device_for(Path(directory),policy,key)
    # No native retry/fallback is performed on a remote command failure.
    if selected:
        from .cluster import Coordinator
        from .cluster_erp import ERPRelay
        hub = Coordinator(Path(directory) / 'cluster')
        ERPRelay(hub)
        with hub.connect() as c:
            ready = c.execute('''SELECT 1 FROM devices d JOIN erp_devices e ON e.device=d.id
                WHERE d.id=? AND d.enabled=1 AND e.version=2 AND e.last_seen>?''',
                (selected, hub.clock() - 10)).fetchone()
        if not ready:
            # Busy/unavailable is handled by the existing pipeline without
            # changing the approval or journal state.
            raise BlockingIOError('Windows production worker unavailable')
        script = Path(__file__).resolve().parents[1] / 'bridges/cluster-flowb.mjs'
        # Per-instance environment, never mutate os.environ across workers.
        original = bridge.call
        async def remote_call(action, **args):
            if action != 'request':
                return await original(action, **args)
            import asyncio
            from flowef.application.errors import WriteOutcomeUnknown, TemporaryExternalError, RateLimited, RequestNotSent
            env = os.environ | {'FLOWEF_LEGACY_ROOT': str(bridge.workspace),
                'FLOW_EF_CONFIG_FILE': str(bridge.workspace/'flow_b_ef/state/config.json'),
                'MAOZI_ACCESS_TOKEN': bridge.token,
                'MAOZI_HTTP_BACKEND': 'native', 'MAOZI_REQUIRE_FEISHU_DELIST': '1',
                'FLOWHUB_ERP_DEVICE': selected, 'FLOWHUB_ERP_PRODUCT': json.dumps(list(key))}
            proc = await asyncio.create_subprocess_exec('node', str(script),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(json.dumps(
                    {'action': action, 'execute': bridge.execute, **args}).encode()), 110)
                result = json.loads(out)
                if result.get('ok'):
                    return result['result']
                error = result.get('error', {})
                if error.get('unknown'):
                    raise WriteOutcomeUnknown('Windows ERP outcome unknown; reconcile')
                if error.get('status') == 429:
                    raise RateLimited(int(error.get('retry_after_ms',60000)/1000))
                if error.get('code') in ('MAOZI_API_PACING_WAIT','MAOZI_PACING_LOCK_TIMEOUT'):
                    raise RequestNotSent('Shared ERP pacing wait')
                raise TemporaryExternalError('Windows ERP request failed')
            except (TimeoutError, ValueError) as exc:
                if args.get('method') != 'GET' and args.get('path') != '/api.chrome/sku3':
                    raise WriteOutcomeUnknown('Windows ERP outcome unknown; reconcile') from exc
                raise TemporaryExternalError('Windows ERP response unavailable') from exc
            finally:
                if proc.returncode is None:
                    proc.kill(); await proc.wait()
        bridge.call = remote_call
