"""Opt-in stack-only evidence for event-loop stalls; never record local values."""
import asyncio
import json
import sys
import threading
import time
import traceback


async def monitor(directory):
    stop=threading.Event();pulse=[time.monotonic()];main=threading.get_ident()
    def watch():
        last=0
        while not stop.wait(1):
            now=time.monotonic()
            if now-pulse[0]<5 or now-last<10:continue
            last=now;frame=sys._current_frames().get(main)
            if frame is None:continue
            frames=[{'file':x.filename,'line':x.lineno,'function':x.name}
                    for x in traceback.extract_stack(frame)]
            try:
                with (directory/'event-loop-stalls.jsonl').open('a') as output:
                    output.write(json.dumps({'at':time.time(),'lag_seconds':now-pulse[0],'stack':frames})+'\n')
            except OSError:pass
    thread=threading.Thread(target=watch,name='loop-stall-observer',daemon=True);thread.start()
    try:
        while True:pulse[0]=time.monotonic();await asyncio.sleep(1)
    finally:stop.set()
