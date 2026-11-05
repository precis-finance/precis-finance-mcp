# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Native analytical-driver fakes. No external services or query interpreter."""
from decimal import Decimal
from concurrent.futures import Future
from urllib.parse import urlsplit
from types import SimpleNamespace


class FakeDownloadConnection:
    """No socket; urllib3's request implementation is replaced with a failure."""
    timeout = None

    def close(self):
        pass


class FakeDownloadPoolManager:
    """Route to a supplied urllib3 pool whose connections perform no I/O."""
    def __init__(self, pool):
        self.pool = pool

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        return self.pool.urlopen(method, parsed.path+"?"+parsed.query, **kwargs)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [("amount", "decimal"), ("missing", "int")]
        self.query_id = "fake-query"
        self.closed = False
        self.cancelled = False
        self.active_result_set = SimpleNamespace(status=SimpleNamespace(name=connection.command_state))

    def execute(self, sql, parameters=None):
        self.connection.calls.append((sql, parameters))
        if self.connection.execute_error:
            raise self.connection.execute_error
        if self.connection.query_handler is not None:
            result = self.connection.query_handler(sql, parameters)
            self.description = [(name, "string") for name in result.column_names]
            self._rows = result.result_rows

    def fetchall(self):
        if self.connection.fetch_error:
            raise self.connection.fetch_error
        return getattr(self, "_rows", [(Decimal("12345678901234567890.1234"), None)])

    def close(self):
        self.closed = True

    def cancel(self):
        self.cancelled = True


class FakeDatabricksConnection:
    def __init__(self, *, execute_error=None, fetch_error=None, query_handler=None, command_state="SUCCEEDED"):
        self.execute_error = execute_error
        self.fetch_error = fetch_error
        self.query_handler = query_handler
        self.command_state = command_state
        self.cursors = []
        self.calls = []
        self.closed = False

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class FakeNativeClient:
    """Scripted native resource for pool/thread lifecycle checks, without SQL evaluation."""

    def __init__(self, query_handler, cancel_handler=None):
        self.query_handler = query_handler
        self.cancel_handler = cancel_handler
        self.close_calls = 0
        self.cancel_calls = 0

    def query(self, sql, parameters=None):
        return self.query_handler(sql, parameters)

    def execute(self, sql, parameters=None, *, settings=None):
        return self.query_handler(sql, parameters)

    def cancel(self):
        self.cancel_calls += 1
        if self.cancel_handler is not None:
            self.cancel_handler()
        return True

    def close(self):
        self.close_calls += 1


class FakeStoppingExecutor:
    """Model shutdown cancelling queued work while a report is still submitting."""

    def __init__(self):
        self.futures = []

    def submit(self, *args):
        if len(self.futures) == 2:
            raise RuntimeError("executor stopping")
        future = Future()
        if not self.futures:
            future.set_result([])
        else:
            future.cancel()
        self.futures.append(future)
        return future
