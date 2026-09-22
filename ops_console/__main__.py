import argparse
import fcntl
import os
from pathlib import Path

import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description='Ozon多店铺运营工作台（独立于上架流水线）')
    parser.add_argument('--data', required=True)
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    directory = Path(args.data).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (directory / 'service.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('该运营数据目录已有服务运行') from None
    if args.demo:
        from .demo import setup
        stores, api = setup(directory)
        app = create_app(directory, stores=stores, api=api, background=False, demo=True)
    else:
        app = create_app(directory)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
