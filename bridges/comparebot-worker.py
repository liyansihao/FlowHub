"""Local stdin/stdout service wrapping the existing compareBot algorithms unchanged."""
import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path


async def main():
    rankers = {}
    while line := await asyncio.to_thread(sys.stdin.readline):
        try:
            request = json.loads(line)
            args = request['args']
            with contextlib.redirect_stdout(sys.stderr):
                if request['mode'] == 'rank':
                    from comparebot.interfaces import cli
                    from comparebot.adapters.dinov2.ranker import DinoV2Ranker
                    device = args.get('device')
                    if device not in rankers:
                        rankers[device] = DinoV2Ranker(device=device)
                    cli.DinoV2Ranker = lambda device=None: rankers[device]
                    function = cli._run
                elif request['mode'] == 'screen':
                    from comparebot.interfaces import screen_cli
                    function = screen_cli._run
                else:
                    raise ValueError('unknown operation')
                for key in ('manifest', 'output', 'ranking_input'):
                    if args.get(key) is not None:
                        args[key] = Path(args[key])
                os.environ.pop('DASHSCOPE_API_KEY', None)
                if request.get('api_key'):
                    os.environ['DASHSCOPE_API_KEY'] = request['api_key']
                try:
                    await function(argparse.Namespace(**args))
                finally:
                    os.environ.pop('DASHSCOPE_API_KEY', None)
            result = {'ok': True}
        except Exception as error:
            # No exception text: upstream messages can include account credentials.
            diagnostic = getattr(error, 'diagnostic', None)
            if diagnostic is None and str(error) == '1688 image search returned no candidates':
                diagnostic = {'stage': 'search1688', 'code': 'no_candidates'}
            if diagnostic is None and str(error) == 'no 1688 candidate image could be downloaded':
                diagnostic = {'stage': 'candidate_download', 'code': 'no_images'}
            result = {'ok': False, 'error': type(error).__name__,
                      'diagnostic': diagnostic, 'reusable': diagnostic is not None}
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
