"""Clocks (docs/10 §3.1).

Every component must take time from a Clock so the same code runs in real time,
accelerated simulation (ScaledClock) and deterministic tests (ManualClock).
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _tz(name: Optional[str]) -> tzinfo:
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if name.upper().startswith("UTC") and len(name) > 3:
        sign = 1 if name[3] == "+" else -1
        return timezone(sign * timedelta(hours=float(name[4:])))
    return timezone.utc


class Clock:
    """Base clock. ``now()`` returns virtual epoch seconds."""

    def __init__(self, tz: Optional[str] = None):
        self.tz = _tz(tz)

    def now(self) -> float:  # pragma: no cover - abstract
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def scale(self) -> float:
        """How many virtual seconds pass per real second (1 for real time)."""
        return 1.0

    def local(self, ts: Optional[float] = None) -> datetime:
        return datetime.fromtimestamp(self.now() if ts is None else ts, self.tz)

    def hour_of_day(self, ts: Optional[float] = None) -> float:
        d = self.local(ts)
        return d.hour + d.minute / 60 + d.second / 3600

    def today(self, ts: Optional[float] = None) -> str:
        return self.local(ts).strftime("%Y-%m-%d")

    def fmt(self, ts: Optional[float] = None, with_weekday: bool = True) -> str:
        d = self.local(ts)
        wd = "一二三四五六日"[d.weekday()]
        return d.strftime("%Y-%m-%d %H:%M") + (f" 周{wd}" if with_weekday else "")

    def real_timeout(self, virtual_seconds: float) -> float:
        """Convert a virtual duration into the real seconds to wait (for asyncio timeouts)."""
        return virtual_seconds / self.scale


class RealClock(Clock):
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class ScaledClock(Clock):
    """Virtual time = start + real_elapsed * scale. Used for accelerated life simulation.

    LLM latency is real, so at scale=60 a 2 s LLM call costs 2 virtual minutes, which is
    realistic "thinking/typing" time anyway.
    """

    def __init__(self, scale: float = 60.0, start: Optional[float] = None, tz: Optional[str] = None):
        super().__init__(tz)
        self._scale = float(scale)
        self._start = time.time() if start is None else float(start)
        self._m0 = time.monotonic()

    @property
    def scale(self) -> float:
        return self._scale

    def now(self) -> float:
        return self._start + (time.monotonic() - self._m0) * self._scale

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / self._scale)


class ManualClock(Clock):
    """Deterministic discrete-event clock for tests.

    ``await clock.sleep(x)`` suspends until a test calls ``await clock.advance(...)`` past it.
    ``advance`` wakes sleepers in timestamp order and lets the event loop settle between wakeups.
    """

    def __init__(self, start: float = 1_790_000_000.0, tz: Optional[str] = "Asia/Shanghai"):
        super().__init__(tz)
        self._now = float(start)
        self._heap: list = []
        self._seq = itertools.count()
        self._settle_hooks: list = []   # async callables returning True if they were busy

    def add_settle_hook(self, hook) -> None:
        """Register ``async hook() -> bool`` that waits for work outside the loop (e.g. DB threads).
        It must return True if it had pending work, so settling repeats until everything is quiet."""
        self._settle_hooks.append(hook)

    @property
    def scale(self) -> float:
        return 1e9  # timeouts expressed in virtual time become ~instant in real time

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self._now + seconds, next(self._seq), fut))
        await fut

    async def settle(self, rounds: int = 20, max_passes: int = 200) -> None:
        """Let the loop run until coroutines and registered external workers (DB threads) are quiet."""
        for _ in range(max_passes):
            for _ in range(rounds):
                await asyncio.sleep(0)
            busy = False
            for h in self._settle_hooks:
                try:
                    busy = bool(await h()) or busy
                except Exception:
                    pass
            if not busy:
                return

    async def advance(self, seconds: float, step: Optional[float] = None) -> None:
        """Advance virtual time by ``seconds``. If ``step`` is given, advance in increments."""
        target = self._now + seconds
        while True:
            await self.settle()
            while self._heap and self._heap[0][2].done():
                heapq.heappop(self._heap)  # cancelled sleepers
            if not self._heap or self._heap[0][0] > target:
                break
            when, _, fut = heapq.heappop(self._heap)
            self._now = max(self._now, when)
            if not fut.done():
                fut.set_result(None)
        self._now = target
        await self.settle()

    def set(self, ts: float) -> None:
        self._now = float(ts)

    def pending_sleepers(self) -> int:
        return sum(1 for _, _, f in self._heap if not f.done())


def make_clock(kind: str = "real", *, scale: float = 60.0, start: Optional[float] = None,
               tz: Optional[str] = None) -> Clock:
    if kind == "real":
        return RealClock(tz)
    if kind == "scaled":
        return ScaledClock(scale, start, tz)
    if kind == "manual":
        return ManualClock(start or 1_790_000_000.0, tz)
    raise ValueError(f"unknown clock kind {kind}")
