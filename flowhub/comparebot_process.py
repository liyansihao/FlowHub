"""Private persistent subprocesses reuse DINO weights; credentials are per request."""
import asyncio
import json
import os
import sys
from pathlib import Path
from .modules import ModuleError

ROOT = Path(__file__).resolve().parents[1]
_workers = {}


class ScreeningFailure(ModuleError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(f"compareBot {diagnostic['stage']}/{diagnostic['code']}")


class ScreeningWorker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.process = None

    async def stop(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        self.process = None

    async def call(self, mode, args, api_key):
        async with self.lock:
            try:
                if self.process is None or self.process.returncode is not None:
                    env = os.environ.copy()
                    env.pop('DASHSCOPE_API_KEY', None)
                    env.update(PYTHONPATH=str(ROOT / 'vendor/compareBot/src'), PYTHON_DOTENV_DISABLED='1')
                    executable = os.environ.get('FLOWHUB_COMPAREBOT_PYTHON') or str(ROOT.parent / 'FlowHub-comparebot/.venv/bin/python')
                    if not Path(executable).is_file():
                        executable = sys.executable
                    self.process = await asyncio.create_subprocess_exec(
                        executable, '-u', str(ROOT / 'bridges/comparebot-worker.py'),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, env=env, limit=2_100_000)
                request = json.dumps({'mode': mode, 'args': args, 'api_key': api_key}) + '\n'
                self.process.stdin.write(request.encode())
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), 90 if mode == 'rank' else 70)
                if not line:
                    raise RuntimeError('compareBot worker exited; review remains unapproved')
                result = json.loads(line)
                if not result.get('ok'):
                    diagnostic = result.get('diagnostic')
                    if result.get('reusable') and isinstance(diagnostic, dict):
                        raise ScreeningFailure(diagnostic)
                    raise RuntimeError('compareBot worker failed; review remains unapproved')
            except ScreeningFailure:
                # The request failed upstream; the DINO model remains healthy.
                raise
            except BaseException:
                await self.stop()
                raise


async def screen(mode, args, api_key=''):
    worker = _workers.setdefault(mode, ScreeningWorker())
    await worker.call(mode, args, api_key)


async def close_workers():
    await asyncio.gather(*(worker.stop() for worker in _workers.values()))
    _workers.clear()
