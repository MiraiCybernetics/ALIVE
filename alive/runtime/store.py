"""SQLite storage with a single writer thread and a pool of reader threads (docs/10 §5).

Why this shape:
- SQLite allows one writer at a time. Funnelling all writes through one dedicated thread
  removes lock contention and lets us *group-commit*: many small writes from different
  coroutines are executed in one transaction, which is the main throughput lever.
- WAL mode lets readers run concurrently with the writer, so reads go to a small thread
  pool with their own connections and never block the event loop.
- Each subsystem registers its own schema with ``register_schema``; migrations are applied
  at start in registration order, versioned per module.

Usage (async):
    rowid = await db.execute("INSERT ...", params)
    rows  = await db.fetchall("SELECT ...", params)      # list[sqlite3.Row]
    res   = await db.transaction(lambda conn: ...)       # atomic multi-statement unit in writer thread

The database file must live on a Linux-native filesystem (not /mnt/c) for WAL locking to be reliable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence, TypeVar

log = logging.getLogger("alive.store")
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Schema registry
# ---------------------------------------------------------------------------
_SCHEMAS: dict = {}  # module -> list[(version, [statements])]


def register_schema(module: str, version: int, statements: Sequence[str]) -> None:
    """Register DDL for ``module`` at ``version`` (1, 2, ...). Idempotent per (module, version).

    Statements for version N run once, in order, when the stored version for the module is < N.
    Use ``CREATE TABLE IF NOT EXISTS`` anyway so dev resets are painless.
    """
    lst = _SCHEMAS.setdefault(module, [])
    for v, _ in lst:
        if v == version:
            return
    lst.append((version, list(statements)))
    lst.sort(key=lambda x: x[0])


register_schema("core", 1, [
    """CREATE TABLE IF NOT EXISTS kv (
        ns TEXT NOT NULL, k TEXT NOT NULL, v TEXT, updated REAL,
        PRIMARY KEY (ns, k))""",
])


@dataclass
class _WriteJob:
    fn: Callable[[sqlite3.Connection], Any]
    fut: asyncio.Future
    loop: asyncio.AbstractEventLoop


class Database:
    def __init__(self, path: str, *, readers: int = 4, batch_max: int = 256,
                 batch_window_ms: float = 2.0):
        self.path = os.path.expanduser(path)
        self.readers = max(1, readers)
        self.batch_max = batch_max
        self.batch_window = batch_window_ms / 1000.0
        self._q: "queue.Queue[Optional[_WriteJob]]" = queue.Queue()
        self._writer: Optional[threading.Thread] = None
        self._wconn: Optional[sqlite3.Connection] = None
        self._rpool: Optional[ThreadPoolExecutor] = None
        self._local = threading.local()
        self._rconns: list = []
        self._rconns_lock = threading.Lock()
        self._started = False
        self.stats = {"write_jobs": 0, "write_batches": 0, "reads": 0}
        self.pending = 0  # submitted but unresolved jobs (writes + reads)

    # ------------------------------------------------------------------ lifecycle
    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False,
                               isolation_level=None)  # autocommit; we manage transactions
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32000")
        conn.execute("PRAGMA mmap_size=268435456")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        return conn

    async def start(self) -> None:
        if self._started:
            return
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._wconn = self._connect()
        mode = self._wconn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            log.warning("SQLite journal_mode is %s (WAL unavailable on this filesystem?)", mode)
        self._wconn.execute("PRAGMA synchronous=NORMAL")
        self._wconn.execute("PRAGMA wal_autocheckpoint=2000")
        self._migrate(self._wconn)
        self._writer = threading.Thread(target=self._writer_loop, name="alive-db-writer", daemon=True)
        self._writer.start()
        self._rpool = ThreadPoolExecutor(max_workers=self.readers, thread_name_prefix="alive-db-reader")
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False
        self._q.put(None)
        if self._writer:
            await asyncio.get_running_loop().run_in_executor(None, self._writer.join, 10)
        if self._rpool:
            self._rpool.shutdown(wait=True)
        with self._rconns_lock:
            for c in self._rconns:
                try:
                    c.close()
                except Exception:
                    pass
            self._rconns.clear()
        if self._wconn:
            try:
                self._wconn.execute("PRAGMA optimize")
                self._wconn.close()
            except Exception:
                pass

    def ensure_schema(self) -> None:
        """Apply schemas registered after start (e.g. plugins loaded late). Runs in caller thread."""
        if self._wconn is not None:
            self._submit_sync(lambda c: self._migrate(c, in_tx=True))

    def _migrate(self, conn: sqlite3.Connection, in_tx: bool = False) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_versions (
            module TEXT PRIMARY KEY, version INTEGER NOT NULL, applied REAL)""")
        for module, versions in list(_SCHEMAS.items()):
            row = conn.execute("SELECT version FROM schema_versions WHERE module=?", (module,)).fetchone()
            cur = row[0] if row else 0
            for v, stmts in versions:
                if v <= cur:
                    continue
                if not in_tx:
                    conn.execute("BEGIN IMMEDIATE")
                try:
                    for s in stmts:
                        conn.execute(s)
                    conn.execute("INSERT OR REPLACE INTO schema_versions VALUES (?,?,?)",
                                 (module, v, time.time()))
                    if not in_tx:
                        conn.execute("COMMIT")
                    log.debug("schema %s -> v%d", module, v)
                except Exception:
                    if not in_tx:
                        conn.execute("ROLLBACK")
                    raise

    # ------------------------------------------------------------------ writer
    def _writer_loop(self) -> None:
        conn = self._wconn
        assert conn is not None
        while True:
            job = self._q.get()
            if job is None:
                return
            batch = [job]
            deadline = time.monotonic() + self.batch_window
            while len(batch) < self.batch_max:
                timeout = deadline - time.monotonic()
                try:
                    nxt = self._q.get(timeout=max(0.0, timeout)) if timeout > 0 else self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._q.put(None)  # re-queue sentinel, finish this batch first
                    break
                batch.append(nxt)
            self._run_batch(conn, batch)

    def _run_batch(self, conn: sqlite3.Connection, batch: list) -> None:
        results: list = []
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception as e:  # pragma: no cover
            for j in batch:
                self._resolve(j, exc=e)
            return
        for j in batch:
            try:
                conn.execute("SAVEPOINT job")
                r = j.fn(conn)
                conn.execute("RELEASE SAVEPOINT job")
                results.append((j, r, None))
            except Exception as e:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT job")
                    conn.execute("RELEASE SAVEPOINT job")
                except Exception:
                    pass
                results.append((j, None, e))
        try:
            conn.execute("COMMIT")
        except Exception as e:  # pragma: no cover
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            for j, _, _ in results:
                self._resolve(j, exc=e)
            return
        self.stats["write_batches"] += 1
        self.stats["write_jobs"] += len(batch)
        for j, r, e in results:
            self._resolve(j, r, e)

    def _resolve(self, job: _WriteJob, result: Any = None, exc: Optional[BaseException] = None) -> None:
        def _set():
            self.pending -= 1
            if job.fut.done():
                return
            if exc is not None:
                job.fut.set_exception(exc)
            else:
                job.fut.set_result(result)
        try:
            job.loop.call_soon_threadsafe(_set)
        except RuntimeError:  # loop closed
            pass

    def _submit_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Blocking submit for non-async contexts (startup/tests). Do not call from the loop."""
        if self._writer is None or not self._writer.is_alive():
            assert self._wconn is not None
            return fn(self._wconn)
        ev = threading.Event()
        box: dict = {}

        class _F:
            def done(self):
                return False

            def set_result(self, r):
                box["r"] = r
                ev.set()

            def set_exception(self, e):
                box["e"] = e
                ev.set()

        class _L:
            @staticmethod
            def call_soon_threadsafe(cb):
                cb()

        self.pending += 1
        self._q.put(_WriteJob(fn, _F(), _L()))  # type: ignore[arg-type]
        ev.wait()
        if "e" in box:
            raise box["e"]
        return box.get("r")

    async def transaction(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` atomically in the writer thread. ``fn`` must not block on I/O."""
        if not self._started:
            raise RuntimeError("database not started")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending += 1
        self._q.put(_WriteJob(fn, fut, loop))
        return await fut

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute one write statement. Returns lastrowid (INSERT) or rowcount."""
        def _f(c: sqlite3.Connection):
            cur = c.execute(sql, params)
            return cur.lastrowid if sql.lstrip()[:6].upper() == "INSERT" else cur.rowcount
        return await self.transaction(_f)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> int:
        rows = list(seq)

        def _f(c: sqlite3.Connection):
            return c.executemany(sql, rows).rowcount
        return await self.transaction(_f)

    # ------------------------------------------------------------------ readers
    def _reader_conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect(readonly=True)
            self._local.conn = c
            with self._rconns_lock:
                self._rconns.append(c)
        return c

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run a read-only function on a reader connection in the pool."""
        if not self._started:
            raise RuntimeError("database not started")
        self.stats["reads"] += 1
        loop = asyncio.get_running_loop()
        self.pending += 1
        try:
            return await loop.run_in_executor(self._rpool, lambda: fn(self._reader_conn()))
        finally:
            self.pending -= 1

    async def wait_idle(self, timeout: float = 5.0) -> bool:
        """For deterministic tests: wait (real time) until no DB job is in flight. Returns True if it waited."""
        import time as _t
        waited = False
        deadline = _t.monotonic() + timeout
        while self.pending > 0 and _t.monotonic() < deadline:
            waited = True
            await asyncio.sleep(0.0005)
        return waited

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list:
        return await self.read(lambda c: c.execute(sql, params).fetchall())

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return await self.read(lambda c: c.execute(sql, params).fetchone())

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        return default if row is None else row[0]

    # ------------------------------------------------------------------ kv helpers
    async def kv_get(self, ns: str, k: str, default: Any = None) -> Any:
        import json
        v = await self.fetchval("SELECT v FROM kv WHERE ns=? AND k=?", (ns, k))
        return default if v is None else json.loads(v)

    async def kv_set(self, ns: str, k: str, v: Any, ts: Optional[float] = None) -> None:
        import json
        await self.execute("INSERT OR REPLACE INTO kv (ns,k,v,updated) VALUES (?,?,?,?)",
                           (ns, k, json.dumps(v, ensure_ascii=False), ts or time.time()))

    async def kv_all(self, ns: str) -> dict:
        import json
        rows = await self.fetchall("SELECT k, v FROM kv WHERE ns=?", (ns,))
        return {r["k"]: json.loads(r["v"]) for r in rows}
