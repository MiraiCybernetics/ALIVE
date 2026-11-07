"""soc_ tables and a coalescing write-behind queue.

The interaction hot path (inbound message -> decision -> send) never awaits the database: rows are queued
here and flushed in one transaction by a background task (natural group commit: while one flush runs, the
next batch accumulates). ``flush()`` waits for everything queued so far (stop(), tests, history reads).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Sequence

from alive.runtime.store import register_schema

log = logging.getLogger("alive.social")

register_schema("social", 1, [
    """CREATE TABLE IF NOT EXISTS soc_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id TEXT NOT NULL, msg_id TEXT, sender_key TEXT, sender_name TEXT, text TEXT,
        ts REAL NOT NULL, mine INTEGER DEFAULT 0, reply_to TEXT, media TEXT, kind TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_soc_messages_thread_ts ON soc_messages(thread_id, ts)",
    "CREATE INDEX IF NOT EXISTS ix_soc_messages_ts ON soc_messages(ts)",
    """CREATE TABLE IF NOT EXISTS soc_threads (
        thread_id TEXT PRIMARY KEY, channel TEXT, chat_id TEXT, chat_type TEXT, title TEXT,
        last_in REAL, last_out REAL, participants TEXT, meta TEXT, updated REAL)""",
    """CREATE TABLE IF NOT EXISTS soc_persons (
        person_key TEXT PRIMARY KEY, kind TEXT DEFAULT 'human', names TEXT, handles TEXT, consent TEXT,
        common_ground TEXT, private_from_others TEXT, mind_model TEXT, paired INTEGER DEFAULT 0,
        blocked INTEGER DEFAULT 0, first_seen REAL, meta TEXT, updated REAL)""",
    """CREATE TABLE IF NOT EXISTS soc_relationships (
        person_key TEXT PRIMARY KEY, closeness REAL, closeness_ts REAL, warmth REAL, warmth_ts REAL,
        trust REAL, reciprocity REAL, rhythm TEXT, last_contact REAL, last_in REAL, last_out REAL,
        last_initiated_by TEXT, ignored_streak INTEGER DEFAULT 0, initiatives TEXT, interactions INTEGER DEFAULT 0,
        milestones TEXT, open_loops TEXT, updated REAL)""",
    """CREATE TABLE IF NOT EXISTS soc_aspects (
        person_key TEXT NOT NULL, aspect TEXT NOT NULL, pos REAL, neg REAL, neu REAL, weight REAL,
        entropy REAL, n INTEGER DEFAULT 0, discarded INTEGER DEFAULT 0, summary TEXT, updated REAL,
        PRIMARY KEY (person_key, aspect))""",
    """CREATE TABLE IF NOT EXISTS soc_opinions (
        id TEXT PRIMARY KEY, person_key TEXT NOT NULL, text TEXT, confidence REAL, evidence TEXT,
        status TEXT DEFAULT 'active', created REAL, updated REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_soc_opinions_person ON soc_opinions(person_key)",
    """CREATE TABLE IF NOT EXISTS soc_pairing (
        code TEXT PRIMARY KEY, channel TEXT, person_key TEXT, sender_name TEXT, chat_id TEXT,
        created REAL, expires REAL, status TEXT DEFAULT 'pending', decided_by TEXT, decided_at REAL)""",
    """CREATE TABLE IF NOT EXISTS soc_feed_seen (
        feed TEXT NOT NULL, item_id TEXT NOT NULL, ts REAL, PRIMARY KEY (feed, item_id))""",
    """CREATE TABLE IF NOT EXISTS soc_initiatives (
        id TEXT PRIMARY KEY, plan_id TEXT, person_key TEXT, thread_id TEXT, reason_type TEXT, ts REAL,
        status TEXT DEFAULT 'pending', resolved_at REAL, outcome TEXT, event_ids TEXT)""",
])
# a person's lines in every space (understanding: baseline and open topics are per person, not per space)
register_schema("social", 2, [
    "CREATE INDEX IF NOT EXISTS ix_soc_messages_sender_ts ON soc_messages(sender_key, ts)",
])


def dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, default=str, separators=(",", ":"))


def loads(s: Optional[str], default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


class WriteBehind:
    """Queue of (sql, params) executed in background transactions; one failing row never drops the batch."""

    def __init__(self, db, *, max_pending: int = 50_000, name: str = "soc.writer"):
        self.db = db
        self.max_pending = max_pending
        self.name = name
        self._rows: list = []
        self._task: Optional[asyncio.Task] = None
        self.written = 0
        self.failed = 0
        self.dropped = 0

    def add(self, sql: str, params: Sequence[Any] = ()) -> None:
        if len(self._rows) >= self.max_pending:
            self._rows.pop(0)
            self.dropped += 1
            if self.dropped % 1000 == 1:
                log.error("%s: write-behind queue full, dropped %d rows", self.name, self.dropped)
        self._rows.append((sql, tuple(params)))
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._run(), name=self.name)
            except RuntimeError:  # no loop (sync context during shutdown)
                pass

    async def _run(self) -> None:
        while self._rows:
            rows, self._rows = self._rows, []

            def job(conn, rows=rows):
                bad = 0
                for sql, params in rows:
                    try:
                        conn.execute(sql, params)
                    except Exception as e:  # keep the rest of the batch
                        bad += 1
                        log.warning("%s: row failed: %r (%s)", self.name, e, sql[:80])
                return bad
            try:
                bad = await self.db.transaction(job)
                self.failed += bad
                self.written += len(rows) - bad
            except Exception:
                self.failed += len(rows)
                log.exception("%s: batch of %d rows failed", self.name, len(rows))

    async def flush(self) -> None:
        for _ in range(1000):
            t = self._task
            if t is None or t.done():
                if not self._rows:
                    return
                self._task = asyncio.get_running_loop().create_task(self._run(), name=self.name)
                t = self._task
            try:
                await asyncio.shield(t)
            except Exception:
                pass

    @property
    def pending(self) -> int:
        return len(self._rows)
