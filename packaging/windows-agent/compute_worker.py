"""Private persistent compareBot worker; accepts data, never executable commands."""
import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path


async def execute(kind,payload,ranker):
    if kind not in ('rank','screen','dossier'):raise ValueError('Unknown compute operation')
    if kind=='dossier':
        from dossier_packet import extract
        return extract(payload)
    manifest=payload['manifest']
    with tempfile.TemporaryDirectory(prefix='flowhub-compute-') as directory:
        root=Path(directory);inp=root/'input.json';out=root/'output.json'
        inp.write_text(json.dumps([manifest]),encoding='utf-8')
        options=dict(manifest=inp,output=out,product_id=str(manifest['product_id']),
                     top_k=10,device=ranker.device)
        if kind=='rank':
            from comparebot.interfaces import cli
            cli.DinoV2Ranker=lambda device=None:ranker
            try:await cli._run(argparse.Namespace(**options))
            except RuntimeError as exc:
                if str(exc)=='1688 image search returned no candidates':
                    return {'query':manifest,'candidates':[],'no_candidates':True}
                raise
        else:
            from comparebot.interfaces import screen_cli
            ranking=root/'ranking.json';ranking.write_text(json.dumps(payload['ranking']),encoding='utf-8')
            options.update(ranking_input=ranking,size=None,high_threshold=.86,medium_threshold=.63,
                           qwen_match_min_similarity=.82,qwen_mismatch_max_similarity=.64,
                           qwen_model=payload.get('qwen_model','qwen3-vl-plus'),
                           qwen_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')
            os.environ.pop('DASHSCOPE_API_KEY',None)
            if payload.get('api_key'):os.environ['DASHSCOPE_API_KEY']=payload['api_key']
            try:await screen_cli._run(argparse.Namespace(**options))
            finally:os.environ.pop('DASHSCOPE_API_KEY',None)
        if out.stat().st_size>2_000_000:raise ValueError('Result too large')
        return json.loads(out.read_text(encoding='utf-8'))


async def main():
    os.environ['PYTHON_DOTENV_DISABLED']='1'
    os.environ.pop('DASHSCOPE_API_KEY',None)
    with contextlib.redirect_stdout(sys.stderr):
        from comparebot.adapters.dinov2.ranker import DinoV2Ranker
        ranker=DinoV2Ranker()
        # Package dependencies and the pinned model must load before readiness.
        from comparebot.adapters.alibaba1688.image_search import Alibaba1688ImageSearchAdapter
        import search1688api
    print(json.dumps({'ready':True,'accelerator':ranker.device,'cpu_count':os.cpu_count() or 1}),flush=True)
    while line:=await asyncio.to_thread(sys.stdin.readline):
        try:
            task=json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                output=await execute(task['kind'],task['payload'],ranker)
            result={'output':output}
        except Exception as exc:
            # Typed diagnostics contain no credentials, signed URLs, or provider text.
            diagnostic=getattr(exc,'diagnostic',{})
            if task['kind']=='rank' and diagnostic.get('code')=='no_candidates':
                result={'output':{'query':task['payload']['manifest'],'candidates':[],'no_candidates':True}}
            else:result={'error':diagnostic.get('code') or type(exc).__name__}
        print(json.dumps(result),flush=True)


if __name__=='__main__':asyncio.run(main())
