"""Event bus: append-only persisted event log + in-process pub/sub (docs/10 §2, ADR-0001/0002).

- ``publish`` assigns id/ts, persists (group-committed by the writer thread) and then fans out
  to subscribers whose pattern matches the event type.
- Every subscription has its own bounded queue and worker task, so delivery to one subscriber
  is ordered and a slow subscriber never blocks the publisher or other subscribers.
- If a subscriber queue is full the oldest *undelivered* event for that subscriber is dropped
  and counted (events are still persisted, so nothing is lost from the log).
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
from dataclasses import asdict
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Sequence

from alive.core.types import Event, Trust, new_id
from alive.runtime.store import Database, register_schema

log = logging.getLogger("alive.bus")

register_schema("events", 1, [
    """CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        type TEXT NOT NULL,
        source TEXT,
        trust INTEGER,
        thread_id TEXT,
        causal_parent TEXT,
        payload TEXT,
        cost TEXT,
        schema_v INTEGER DEFAULT 1)""",
    "CREATE INDEX IF NOT EXISTS ix_events_type_ts ON events(type, ts)",
    "CREATE INDEX IF NOT EXISTS ix_events_thread_ts ON events(thread_id, ts)",
    "CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)",
])

Handler = Callable[[Event], Awaitable[None]]


def _dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, default=str, separators=(",", ":"))


class Subscription:
    def __init__(self, bus: "EventBus", pattern: str, handler: Handler, name: str, maxsize: int):
        self.bus = bus
        self.patterns = [p.strip() for p in pattern.split("|") if p.strip()]
        self.handler = handler
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.delivered = 0
        self.errors = 0
        self.task: Optional[asyncio.Task] = None

    def matches(self, etype: str) -> bool:
        return any(fnmatch.fnmatchcase(etype, p) for p in self.patterns)

    def offer(self, ev: Event) -> None:
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            self.dropped += 1
            self.queue.put_nowait(ev)

    async def run(self) -> None:
        while True:
            ev = await self.queue.get()
            try:
                await self.handler(ev)
                self.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors += 1
                log.exception("subscriber %s failed on %s", self.name, ev.type)
            finally:
                self.queue.task_done()

    def cancel(self) -> None:
        if self.task:
            self.task.cancel()
        if self in self.bus._subs:
            self.bus._subs.remove(self)


class EventBus:
    def __init__(self, db: Database, clock, *, persist_filter: Optional[Callable[[Event], bool]] = None):
        self.db = db
        self.clock = clock
        self._subs: list = []
        self._persist_filter = persist_filter
        self.counts: dict = {}

    # ------------------------------------------------------------------ publish
    def _prepare(self, ev: Event) -> Event:
        if not ev.id:
            ev.id = new_id("e")
        if not ev.ts:
            ev.ts = self.clock.now()
        return ev

    @staticmethod
    def _row(ev: Event) -> tuple:
        return (ev.id, ev.ts, ev.type, ev.source, int(ev.trust), ev.thread_id, ev.causal_parent,
                _dumps(ev.payload), _dumps(ev.cost) if ev.cost else None, ev.schema_v)

    async def publish(self, ev: Event, *, persist: bool = True) -> Event:
        """Persist then dispatch. Returns the event with id/ts filled in."""
        self._prepare(ev)
        if persist and (self._persist_filter is None or self._persist_filter(ev)):
            row = self._row(ev)
            await self.db.transaction(lambda c: c.execute(
                "INSERT INTO events (id,ts,type,source,trust,thread_id,causal_parent,payload,cost,schema_v)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", row))
        self._dispatch(ev)
        return ev

    async def emit(self, etype: str, payload: Optional[dict] = None, *, source: str = "system",
                   trust: Trust = Trust.SYSTEM, thread_id: Optional[str] = None,
                   causal_parent: Optional[str] = None, cost: Optional[dict] = None,
                   persist: bool = True) -> Event:
        """Convenience wrapper around ``publish``."""
        return await self.publish(Event(type=etype, payload=payload or {}, source=source, trust=trust,
                                        thread_id=thread_id, causal_parent=causal_parent, cost=cost),
                                  persist=persist)

    def emit_nowait(self, etype: str, payload: Optional[dict] = None, **kw) -> None:
        """Fire-and-forget emit from sync code running inside the loop."""
        asyncio.get_running_loop().create_task(self.emit(etype, payload, **kw))

    def _dispatch(self, ev: Event) -> None:
        self.counts[ev.type] = self.counts.get(ev.type, 0) + 1
        for s in list(self._subs):
            if s.matches(ev.type):
                s.offer(ev)

    # ------------------------------------------------------------------ subscribe
    def subscribe(self, pattern: str, handler: Handler, *, name: str = "", maxsize: int = 2000) -> Subscription:
        """Subscribe ``handler`` to event types matching ``pattern`` (fnmatch, '|' separated)."""
        sub = Subscription(self, pattern, handler, name or getattr(handler, "__qualname__", "sub"), maxsize)
        sub.task = asyncio.get_running_loop().create_task(sub.run(), name=f"sub:{sub.name}")
        self._subs.append(sub)
        return sub

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait until all subscriber queues are empty (tests / shutdown)."""
        async def _all():
            for s in list(self._subs):
                await s.queue.join()
        try:
            await asyncio.wait_for(_all(), timeout)
        except asyncio.TimeoutError:  # pragma: no cover
            log.warning("bus drain timed out")

    async def close(self) -> None:
        for s in list(self._subs):
            s.cancel()
        self._subs.clear()

    def stats(self) -> dict:
        return {"counts": dict(self.counts),
                "subs": [{"name": s.name, "q": s.queue.qsize(), "dropped": s.dropped,
                          "delivered": s.delivered, "errors": s.errors} for s in self._subs]}

    # ------------------------------------------------------------------ query / replay
    @staticmethod
    def _from_row(r) -> Event:
        return Event(id=r["id"], ts=r["ts"], type=r["type"], source=r["source"] or "",
                     trust=Trust(r["trust"] if r["trust"] is not None else 4), thread_id=r["thread_id"],
                     causal_parent=r["causal_parent"], payload=json.loads(r["payload"] or "{}"),
                     cost=json.loads(r["cost"]) if r["cost"] else None, schema_v=r["schema_v"] or 1)

    async def query(self, *, types: Optional[Sequence[str]] = None, since: Optional[float] = None,
                    until: Optional[float] = None, thread_id: Optional[str] = None,
                    limit: int = 1000, desc: bool = False, type_prefix: Optional[str] = None) -> list:
        where, params = [], []
        if types:
            where.append("type IN (%s)" % ",".join("?" * len(types)))
            params += list(types)
        if type_prefix:
            where.append("type LIKE ?")
            params.append(type_prefix + "%")
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts < ?")
            params.append(until)
        if thread_id is not None:
            where.append("thread_id = ?")
            params.append(thread_id)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts %s, id %s LIMIT ?" % (("DESC",) * 2 if desc else ("ASC",) * 2)
        params.append(limit)
        rows = await self.db.fetchall(sql, params)
        return [self._from_row(r) for r in rows]

    async def get(self, event_id: str) -> Optional[Event]:
        r = await self.db.fetchone("SELECT * FROM events WHERE id=?", (event_id,))
        return self._from_row(r) if r else None

    async def replay(self, *, batch: int = 2000, **filters) -> AsyncIterator[Event]:
        """Iterate events in order, in pages, without loading everything in memory."""
        since = filters.pop("since", None)
        last_id = ""
        while True:
            evs = await self.query(since=since, limit=batch, **filters)
            evs = [e for e in evs if (e.ts, e.id) > ((since or 0), last_id)] if last_id else evs
            if not evs:
                return
            for e in evs:
                yield e
            since, last_id = evs[-1].ts, evs[-1].id
            if len(evs) < batch:
                return
