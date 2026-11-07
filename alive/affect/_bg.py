"""Tiny helpers for fire-and-forget work from sync methods (event emission, write-behind persistence).

Sync interface methods must not do I/O; they mark state dirty / queue events and these helpers run the
actual awaitables on the running loop. Tasks are tracked (no GC mid-flight) and can be drained on stop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional


class Background:
    def __init__(self, logger: logging.Logger):
        self.log = logger
        self._tasks: set = set()

    def spawn(self, coro: Awaitable) -> Optional[asyncio.Task]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (sync unit use): drop silently
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]
            return None
        t = loop.create_task(coro)  # type: ignore[arg-type]
        self._tasks.add(t)
        t.add_done_callback(self._done)
        return t

    def _done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            self.log.warning("background task failed: %r", t.exception())

    def emit(self, bus, etype: str, payload: dict, **kw) -> None:
        if bus is None:
            return
        self.spawn(bus.emit(etype, payload, **kw))

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)


class WriteBehind:
    """Coalescing persistence: ``mark()`` from sync code; one flush task runs until clean."""

    def __init__(self, bg: Background, flush: Callable[[], Awaitable[None]]):
        self.bg = bg
        self._flush = flush
        self.dirty = False
        self._running = False

    def mark(self) -> None:
        self.dirty = True
        if not self._running:
            self._running = True
            if self.bg.spawn(self._run()) is None:
                self._running = False

    async def _run(self) -> None:
        try:
            while self.dirty:
                self.dirty = False
                try:
                    await self._flush()
                except asyncio.CancelledError:
                    self.dirty = True
                    raise
                except Exception:
                    self.bg.log.exception("write-behind flush failed")
                    self.dirty = True  # retried on the next mark()/flush()
                    break
        finally:
            self._running = False

    async def flush(self) -> None:
        self.dirty = False
        await self._flush()
