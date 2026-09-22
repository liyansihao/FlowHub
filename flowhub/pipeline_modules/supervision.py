"""Recover failed background loops independently; keep evidence outside SQLite."""
import asyncio
import json
import logging
import os
import time
import traceback
from pathlib import Path


def record(directory, name, event, **details):
    row = {'at': time.time(), 'pid': os.getpid(), 'task': name, 'event': event, **details}
    line = json.dumps(row, ensure_ascii=False)
    # A database outage must not hide the exception which caused this restart.
    logging.getLogger(__name__).warning('background_task %s', line)
    with (Path(directory) / 'background-tasks.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(line + '\n')


async def run(name, factory, directory, *, restart_delays=(1, 5, 15), healthy_after=60):
    failures = 0
    while True:
        started = time.monotonic()
        record(directory, name, 'started', restart_attempt=failures)
        try:
            await factory()
            raise RuntimeError('background_loop_returned')
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError) and asyncio.current_task().cancelling():
                record(directory, name, 'stopped')
                raise
            if time.monotonic() - started >= healthy_after:
                failures = 0
            failures += 1
            record(directory, name, 'failed', error_type=type(error).__name__,
                   sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
                   sqlite_errorname=getattr(error, 'sqlite_errorname', None),
                   frames=[{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                           for f in traceback.extract_tb(error.__traceback__)], failures=failures)
            if failures > len(restart_delays):
                record(directory, name, 'exhausted')
                raise  # Escalation to Worker is a last resort after bounded local recovery.
            delay = restart_delays[failures - 1]
            record(directory, name, 'restarting', delay_seconds=delay)
            await asyncio.sleep(delay)
