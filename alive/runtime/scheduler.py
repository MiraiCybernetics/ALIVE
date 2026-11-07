"""Scheduler and task supervision (docs/10 §3).

All background activity is a named, supervised task running on the kernel clock:
- ``spawn``     long-lived coroutine, optionally restarted with backoff if it crashes
- ``every``     periodic job; interval may be a callable (adaptive heartbeat)
- ``call_later``/``call_at`` one-shot timers (cancelable)
- ``daily``     wall-clock (persona timezone) daily job, e.g. sleep window start

Exceptions are logged and published as rt.error; one failing job never kills the process.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, Union

log = logging.getLogger("alive.sched")

CoroFn = Callable[[], Awaitable[None]]


class Handle:
    def __init__(self, name: str, task: asyncio.Task):
        self.name = name
        self.task = task

    def cancel(self) -> None:
        self.task.cancel()

    @property
    def done(self) -> bool:
        return self.task.done()


class Scheduler:
    def __init__(self, clock, bus=None):
        self.clock = clock
        self.bus = bus
        self._tasks: dict = {}
        self.errors = 0
        self._closing = False

    def _track(self, name: str, task: asyncio.Task) -> Handle:
        old = self._tasks.get(name)
        if old is not None and not old.done and old.task is not task:
            # keep unique names: suffix
            i = 2
            while f"{name}#{i}" in self._tasks and not self._tasks[f"{name}#{i}"].done:
                i += 1
            name = f"{name}#{i}"
        h = Handle(name, task)
        self._tasks[name] = h
        task.add_done_callback(lambda t, n=name: self._done(n, t))
        return h

    def _done(self, name: str, t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            self._report(name, exc)

    def _report(self, name: str, exc: BaseException) -> None:
        self.errors += 1
        log.error("task %s failed: %r", name, exc, exc_info=exc)
        if self.bus is not None and not self._closing:
            try:
                self.bus.emit_nowait("rt.error", {"task": name, "error": repr(exc)[:500]}, source="scheduler")
            except Exception:
                pass

    async def _guard(self, name: str, fn: CoroFn) -> bool:
        try:
            await fn()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._report(name, e)
            return False

    # ------------------------------------------------------------------ API
    def spawn(self, coro_fn: CoroFn, name: str, *, restart: bool = False, max_backoff: float = 300.0) -> Handle:
        async def runner():
            backoff = 1.0
            while True:
                ok = await self._guard(name, coro_fn)
                if not restart or self._closing:
                    return
                if ok:
                    backoff = 1.0
                await self.clock.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
        return self._track(name, asyncio.get_running_loop().create_task(runner(), name=name))

    def every(self, interval: Union[float, Callable[[], float]], coro_fn: CoroFn, name: str, *,
              jitter: float = 0.0, initial_delay: Optional[float] = None) -> Handle:
        async def runner():
            first = True
            while True:
                iv = interval() if callable(interval) else interval
                delay = initial_delay if (first and initial_delay is not None) else iv
                first = False
                if jitter:
                    delay *= 1 + random.uniform(-jitter, jitter)
                await self.clock.sleep(max(0.0, delay))
                await self._guard(name, coro_fn)
        return self._track(name, asyncio.get_running_loop().create_task(runner(), name=name))

    def call_later(self, delay: float, coro_fn: CoroFn, name: str) -> Handle:
        async def runner():
            await self.clock.sleep(max(0.0, delay))
            await self._guard(name, coro_fn)
        return self._track(name, asyncio.get_running_loop().create_task(runner(), name=name))

    def call_at(self, ts: float, coro_fn: CoroFn, name: str) -> Handle:
        return self.call_later(ts - self.clock.now(), coro_fn, name)

    def daily(self, hhmm: str, coro_fn: CoroFn, name: str) -> Handle:
        """Run every day at local time hh:mm (persona timezone)."""
        hh, mm = (int(x) for x in hhmm.split(":"))

        def next_delay() -> float:
            now = self.clock.local()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target.timestamp() <= now.timestamp() + 1:
                from datetime import timedelta
                target = target + timedelta(days=1)
            return target.timestamp() - now.timestamp()

        async def runner():
            while True:
                await self.clock.sleep(next_delay())
                await self._guard(name, coro_fn)
        return self._track(name, asyncio.get_running_loop().create_task(runner(), name=name))

    def cancel(self, name: str) -> None:
        h = self._tasks.get(name)
        if h:
            h.cancel()

    def running(self) -> list:
        return [n for n, h in self._tasks.items() if not h.done]

    async def close(self) -> None:
        self._closing = True
        tasks = [h.task for h in self._tasks.values() if not h.done]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
