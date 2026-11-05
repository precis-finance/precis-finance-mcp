# SPDX-License-Identifier: Elastic-2.0
"""Small process-local native-client pools; no user scheduling or SQL policy."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from threading import Condition, Lock
from time import monotonic

from .contracts import ClientClosedError, ExecutionError

MAX_CONNECTIONS = 3


class ClientPool:
    """Lazily retain up to three exclusive clients with a bounded checkout wait."""

    def __init__(self, factory, *, capacity=MAX_CONNECTIONS, wait_timeout=30, idle_timeout=300):
        self._factory = factory
        self._capacity = capacity
        self._wait_timeout = wait_timeout
        self._idle_timeout = idle_timeout
        self._condition = Condition()
        self._idle = []
        self._count = 0
        self._closed = False

    def acquire(self):
        deadline = monotonic() + self._wait_timeout
        while True:
            expired = None
            with self._condition:
                if self._closed:
                    raise ClientClosedError("Analytical resources are closed.")
                if self._idle:
                    client, returned_at = self._idle.pop()
                    if monotonic() - returned_at < self._idle_timeout:
                        return client
                    expired = client
                    self._count -= 1
                    self._condition.notify()
                elif self._count < self._capacity:
                    self._count += 1
                    break
                else:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise ExecutionError("Analytical connections are busy; retry the request.")
                    self._condition.wait(remaining)
            if expired is not None:
                with suppress(Exception):
                    expired.close()
        try:
            return self._factory()
        except BaseException:
            with self._condition:
                self._count -= 1
                self._condition.notify()
            raise

    def release(self, client, *, reusable=True):
        with self._condition:
            discard = self._closed or not reusable
            if discard:
                self._count -= 1
            else:
                self._idle.append((client, monotonic()))
            self._condition.notify()
        if discard:
            with suppress(Exception):
                client.close()

    def close(self):
        with self._condition:
            self._closed = True
            idle, self._idle = self._idle, []
            self._count -= len(idle)
            self._condition.notify_all()
        for client, _ in idle:
            with suppress(Exception):
                client.close()


class PooledQueries:
    """Request-owned execution handle; cancellation only targets its own loans."""

    def __init__(self, pool):
        self._pool = pool
        self._condition = Condition()
        self._closed = False
        self._inflight = 0
        self._active = set()

    def query(self, sql, parameters=None):
        return self._run("query", sql, parameters)

    def execute(self, sql, parameters=None, *, settings=None):
        return self._run("execute", sql, parameters, settings=settings)

    def _run(self, method, sql, parameters, **options):
        with self._condition:
            if self._closed:
                raise ClientClosedError("Analytical client is closed.")
            self._inflight += 1
        client = None
        reusable = False
        try:
            client = self._pool.acquire()
            with self._condition:
                self._active.add(client)
            result = getattr(client, method)(sql, parameters=parameters, **options)
            reusable = True
            return result
        finally:
            if client is not None:
                with self._condition:
                    self._active.remove(client)
                self._pool.release(client, reusable=reusable)
            with self._condition:
                self._inflight -= 1
                self._condition.notify_all()

    def cancel(self):
        attempted = False
        failed = False
        # Removal from _active precedes returning the connection to the pool.
        # Holding this lock prevents cancellation reaching its next borrower.
        with self._condition:
            for client in self._active:
                cancel = getattr(client, "cancel", None)
                if cancel is not None:
                    try:
                        attempted = cancel() or attempted
                    except Exception:
                        failed = True
        if failed:
            raise ExecutionError("Analytical cancellation could not be confirmed.") from None
        return attempted

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            self._condition.wait_for(lambda: self._inflight == 0)


_lock = Lock()
_pools = {}
_executor = None


def get_pool(backend, key, factory):
    """One current configuration per backend; retire incompatible resources."""
    old = None
    with _lock:
        previous = _pools.get(backend)
        if previous is not None and previous[0] == key:
            return previous[1]
        if previous is not None:
            old = previous[1]
        pool = ClientPool(factory)
        _pools[backend] = (key, pool)
    if old is not None:
        old.close()
    return pool


def query_executor():
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=MAX_CONNECTIONS, thread_name_prefix="precis-sql")
        return _executor


def close_analytical_resources():
    """Drain SQL workers before retiring their sessions; safe for repeated shutdown."""
    global _executor
    with _lock:
        executor, _executor = _executor, None
        pools = [pool for _, pool in _pools.values()]
        _pools.clear()
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    for pool in pools:
        pool.close()
