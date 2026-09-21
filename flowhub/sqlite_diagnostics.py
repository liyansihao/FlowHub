"""Opt-in investigation only: timings and code locations, never SQL or values."""
import json
import os
import sqlite3
import sys
import threading
import time


def location():
    frame = sys._getframe(2)
    return {'file': frame.f_code.co_filename, 'line': frame.f_lineno,
            'function': frame.f_code.co_name}


class TimedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opened = time.monotonic()
        self.writer_started = None
        self.writer_origin = None
        self.transactions = []
        self.slow_operations = []
        self.operation_count = 0

    def _timed(self, method, args, kwargs, origin):
        started = time.monotonic()
        error_type = None
        try:
            result = method(*args, **kwargs)
            # BEGIN IMMEDIATE has returned successfully before this timestamp.
            # For implicit transactions, the first successful write has returned.
            if self.in_transaction and self.writer_started is None:
                self.writer_started = time.monotonic()
                self.writer_origin = origin
            elif not self.in_transaction:
                self._finish_transaction()
            return result
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            elapsed = time.monotonic() - started
            self.operation_count += 1
            if elapsed >= .1 or error_type:
                if len(self.slow_operations) < 20:
                    self.slow_operations.append({'seconds': elapsed, 'origin': origin,
                                                 'error_type': error_type})

    def execute(self, *args, **kwargs):
        return self._timed(super().execute, args, kwargs, location())

    def executemany(self, *args, **kwargs):
        return self._timed(super().executemany, args, kwargs, location())

    def executescript(self, *args, **kwargs):
        # SQLite executescript retains its native implicit-commit semantics.
        return self._timed(super().executescript, args, kwargs, location())

    def _finish_transaction(self):
        if self.writer_started is not None:
            if len(self.transactions) < 20:
                self.transactions.append({'seconds_after_first_success': time.monotonic() - self.writer_started,
                                          'origin': self.writer_origin})
            self.writer_started = None

    def commit(self):
        result = super().commit()
        self._finish_transaction()
        return result

    def rollback(self):
        result = super().rollback()
        self._finish_transaction()
        return result

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            if not self.in_transaction:
                self._finish_transaction()

    def close(self):
        result = super().close()
        self._finish_transaction()
        return result

    def report(self, path):
        elapsed = time.monotonic() - self.opened
        if elapsed < 2:
            return
        record = {'at': time.time(), 'pid': os.getpid(), 'thread': threading.get_ident(),
                  'connection_seconds': elapsed, 'operation_count': self.operation_count,
                  'transactions': self.transactions, 'slow_operations': self.slow_operations}
        # Diagnostic IO failure must never change commit/rollback or caller errors.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a') as stream:
                stream.write(json.dumps(record) + '\n')
        except OSError:
            pass
