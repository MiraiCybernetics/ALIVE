"""Per-thread state and actor (docs/16 §3: one coroutine per active conversation thread).

The actor owns the space's inbox (bounded), runs attention (when do I look?) -> debounce -> decision -> compose ->
send for each batch, and handles messages that arrive while a reply is in flight (steer / interrupt / queue).
One actor's failure is contained: exceptions are logged per batch and the actor keeps running; idle actors
exit after ``idle_gc_s`` and are recreated on the next message.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from alive.core.types import ChatLine, IncomingMessage, Trust
from alive.social import shaping
from alive.social.decision import LineView

log = logging.getLogger("alive.social")


@dataclass
class Inbound:
    """An inbound message plus everything computed once at submit time."""
    msg: IncomingMessage
    line: ChatLine
    trust: Trust
    addressed: bool                 # @me, reply to me, or vocative name
    name_mentioned: bool
    prev_ts: Optional[float]        # thread activity before this message (debounce quiet/active start)
    seq: int
    event_id: str = ""
    is_agent: bool = False
    latency: Optional[float] = None   # private: seconds since my last message, if this answers it
    gap: Optional[float] = None       # seconds since the last contact with this person (any direction)
    observed: bool = False            # already fed into the person's baseline


class ThreadState:
    """Everything the interaction layer remembers about one thread (survives actor GC; lines are reloadable)."""

    def __init__(self, thread_id: str, channel: str, chat_id: str, chat_type: str, title: str = "",
                 lines_max: int = 100):
        self.thread_id = thread_id
        self.channel = channel
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.title = title
        self.lines_max = lines_max
        self.lines: Optional[deque] = None          # ChatLines (oldest first); None = not loaded
        self.load_lock = asyncio.Lock()
        self.last_in = 0.0
        self.last_out = 0.0
        self.last_activity = 0.0
        self.last_from_me = False
        self.pending = 0                            # incoming since my last message
        self.participants: deque = deque(maxlen=12)
        self.recent: deque = deque(maxlen=400)      # LineView for 5-min stats and the turn ladder
        self.recent_in: deque = deque(maxlen=64)    # (seq, Inbound) for abort checks during send
        self.my_msg_ids: deque = deque(maxlen=300)
        self._my_ids: set = set()
        self.my_last_targets: list = []
        self.seq = 0
        self.attend_until = 0.0                     # I'm "in" this space until then (alive/social/air.py)
        self.bot_hops = 0
        self.send_lock = asyncio.Lock()
        self.muted_until = 0.0
        self.dirty = False
        self.last_cues: list = []
        self.group_ctx: dict = {}

    # ------------------------------------------------------------------ lines
    def add_line(self, line: ChatLine) -> None:
        if self.lines is not None:
            self.lines.append(line)
        self.recent.append(LineView(line.ts, line.person_key, line.mine))
        self.last_activity = max(self.last_activity, line.ts)
        self.last_from_me = line.mine
        if line.mine:
            if line.msg_id:
                self.add_my_id(line.msg_id)
        self.dirty = True

    def add_my_id(self, msg_id: str) -> None:
        if len(self.my_msg_ids) == self.my_msg_ids.maxlen:
            self._my_ids.discard(self.my_msg_ids[0])
        self.my_msg_ids.append(msg_id)
        self._my_ids.add(msg_id)

    def is_mine(self, msg_id: Optional[str]) -> bool:
        return bool(msg_id) and msg_id in self._my_ids

    def window(self, now: float, seconds: float) -> list:
        return [l for l in self.recent if now - l.ts <= seconds]

    def info(self):
        from alive.interfaces import ThreadInfo
        return ThreadInfo(thread_id=self.thread_id, channel=self.channel, chat_type=self.chat_type, title=self.title,
                          last_in=self.last_in, last_out=self.last_out, pending=self.pending,
                          participants=list(dict.fromkeys(reversed(self.participants))))


@dataclass
class InFlight:
    participants: set
    context_text: str
    phase: str = "composing"          # composing | sending
    started: float = 0.0
    steer: list = field(default_factory=list)
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    interrupts: list = field(default_factory=list)


class ThreadActor:
    def __init__(self, mgr, state: ThreadState, inbox_max: int = 200):
        self.mgr = mgr
        self.k = mgr.k
        self.state = state
        self.inbox: deque = deque()
        self.inbox_max = inbox_max
        self.dropped = 0
        self.deferred: list = []
        self._deferred_seq = 0
        self.recheck_at = 0.0
        self.inflight: Optional[InFlight] = None
        self._wake = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.busy = False
        self.batches = 0
        self.errors = 0

    # ------------------------------------------------------------------ intake (sync, called by the manager)
    def deliver(self, item: Inbound) -> None:
        if len(self.inbox) >= self.inbox_max:
            self.inbox.popleft()
            self.dropped += 1
            if self.dropped % 50 == 1:
                log.warning("thread %s inbox full; dropped %d oldest messages", self.state.thread_id, self.dropped)
        self.inbox.append(item)
        fl = self.inflight
        if fl is not None:
            mode = shaping.classify_inflight(item.msg, fl.participants, item.addressed, fl.context_text, fl.phase)
            if mode == shaping.INTERRUPT:
                fl.interrupts.append(item)
                fl.interrupt.set()
            elif mode == shaping.STEER:
                fl.steer.append(item)
        self._wake.set()

    # ------------------------------------------------------------------ waiting helpers (virtual time)
    async def _wait(self, timeout: Optional[float]) -> bool:
        """Wait for a wake-up (new message) or ``timeout`` virtual seconds. True if woken."""
        if self._wake.is_set():
            self._wake.clear()
            return True
        if timeout is not None and timeout <= 0:
            return False
        waiter = asyncio.ensure_future(self._wake.wait())
        tasks = {waiter}
        if timeout is not None:
            tasks.add(asyncio.ensure_future(self.k.clock.sleep(timeout)))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        if waiter in done:
            self._wake.clear()
            return True
        return False

    async def _wait_task_or_interrupt(self, task: asyncio.Task, fl: InFlight) -> bool:
        """Wait for ``task``; returns True if interrupted first (task left running for the caller to cancel)."""
        if fl.interrupt.is_set():
            return True
        iw = asyncio.ensure_future(fl.interrupt.wait())
        try:
            done, _ = await asyncio.wait({task, iw}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not iw.done():
                iw.cancel()
        return task not in done

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        mgr = self.mgr
        try:
            while True:
                if not self.inbox:
                    if self.deferred:
                        woke = await self._wait(max(0.0, self.recheck_at - self.k.clock.now()))
                        if not woke and not self.inbox:
                            batch, self.deferred = self.deferred, []
                            await self._safe_process(batch, deferred=True)
                        continue
                    woke = await self._wait(mgr.idle_gc_s)
                    if not woke and not self.inbox and not self.deferred and not self.state.send_lock.locked():
                        return  # idle: garbage-collect this actor
                    continue
                await self._attend()
                batch = await self._collect() if self.inbox else []
                if self.deferred:
                    batch, self.deferred = self.deferred + batch, []
                if batch:
                    await self._safe_process(batch)
        finally:
            mgr._actor_exited(self)

    async def _safe_process(self, batch: list, deferred: bool = False) -> None:
        self.busy = True
        self.batches += 1
        try:
            await self.mgr._process_batch(self, batch, deferred=deferred)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.errors += 1
            log.exception("thread %s: batch processing failed", self.state.thread_id)
        finally:
            self.busy = False
            self.inflight = None

    async def _attend(self) -> None:
        """Attention (alive/social/air.py): until I actually look at the space, lines pile up. Each later line is
        another chance for its notification to pull me in. When I look, the debounce takes over (the speaker may
        not be done yet)."""
        mgr = self.mgr
        if not self.inbox:
            return
        now = self.k.clock.now()
        delay = mgr.air.notice_delay(self.state, self.inbox[0], now)
        if delay <= 0:
            return
        look_at = now + delay
        seen = len(self.inbox)
        run = 1
        while True:
            remaining = look_at - self.k.clock.now()
            if remaining <= 1e-9:
                break
            await self._wait(remaining)
            while seen < len(self.inbox):         # each new line: does its notification pull me in?
                it = self.inbox[seen]
                prev = self.inbox[seen - 1] if seen > 0 else None
                run = run + 1 if prev is not None and prev.msg.person_key == it.msg.person_key else 1
                seen += 1
                d = mgr.air.catches(self.state, it, self.k.clock.now(), run_len=run)
                if d is not None:
                    look_at = min(look_at, self.k.clock.now() + d)

    async def _collect(self) -> list:
        """Debounce: wait for the other side to finish (MaiBot natural profile, sampled: shaping.py)."""
        mgr = self.mgr
        first = self.inbox.popleft()
        batch = [first]
        timer = shaping.DebounceTimer(mgr.debounce, first.msg, self.k.clock.now(), first.prev_ts,
                                      mgr.patience(), first.addressed, rng=mgr.pace_rng(),
                                      their_gap=mgr.burst_gap(first.msg.person_key))
        while True:
            while self.inbox:
                it = self.inbox.popleft()
                batch.append(it)
                timer.add(it.msg, self.k.clock.now(), it.addressed)
            remaining = timer.deadline - self.k.clock.now()
            if remaining <= 1e-9:
                break
            await self._wait(remaining)
        return batch

    def take_from_inbox(self, items: list) -> list:
        """Remove specific Inbound items (steer/interrupt messages merged into the current reply)."""
        ids = {id(i) for i in items}
        taken = [i for i in self.inbox if id(i) in ids]
        if taken:
            self.inbox = deque(i for i in self.inbox if id(i) not in ids)
        return taken
