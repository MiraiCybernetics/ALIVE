"""Inbound shaping (docs/07 §2.3): dedupe, debounce timing, in-flight message classification.

Debounce defaults follow MaiBot's "natural" profile (research_notes chinese_community_projects §1):
quiet start 2 s (previous message > 30 s ago), active start 5 s, same-sender continuation resets to 2 s,
speaker change 0.5 s, media-only start >= 4 s (media inside a batch may only shorten the wait), whole batch
<= 8 s, @/mention <= 1 s. All values are virtual seconds and (except the mention cap) scale with the affect
``patience`` modulator.

Split utterances (docs/22 §4): people split one sentence over several messages. Completion is tracked PER
SPEAKER, not per space: a line that stops before a transition-relevance place (Sacks, Schegloff & Jefferson 1974:
trailing comma or connective, a bare @mention) keeps that speaker's utterance open, so another speaker cutting in
or a mention does not close the batch on half a sentence. The whole batch may then stretch to ``batch_abs_max_s``.

The numbers above are MEDIANS, not fixed waits (ADR-0007): with an rng, every wait is drawn from
LogNormal(median × patience × g, 0.35), where g follows the speaker's own pace -- how long they usually pause between
the pieces of one utterance (PersonBaseline.gap_median; g = clamp((gap / 3 s)^0.6, 0.6, 3)). Someone who types in
slow fragments gets waited for longer, a fast typist less. Without an rng (unit tests of the medians) the waits are
the medians themselves.
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from alive.core.types import IncomingMessage
from alive.social import text as T


class Dedupe:
    """(channel, msg_id) seen-cache with TTL and size cap (OpenClaw: 20 min / 5000 entries)."""

    def __init__(self, ttl: float = 1200.0, max_size: int = 5000):
        self.ttl = float(ttl)
        self.max_size = int(max_size)
        self._seen: "OrderedDict[tuple, float]" = OrderedDict()
        self.hits = 0

    def seen(self, key: tuple, now: float) -> bool:
        """Return True if ``key`` was seen within TTL; otherwise remember it and return False."""
        self._expire(now)
        exp = self._seen.get(key)
        if exp is not None and exp > now:
            self.hits += 1
            return True
        self._seen[key] = now + self.ttl
        self._seen.move_to_end(key)
        while len(self._seen) > self.max_size:
            self._seen.popitem(last=False)
        return False

    def _expire(self, now: float) -> None:
        while self._seen:
            k, exp = next(iter(self._seen.items()))
            if exp > now:
                break
            self._seen.popitem(last=False)

    def __len__(self) -> int:
        return len(self._seen)


@dataclass
class DebounceParams:
    quiet_start_s: float = 2.0
    active_start_s: float = 5.0
    quiet_gap_s: float = 30.0          # previous message older than this -> "quiet start"
    same_sender_s: float = 2.0
    speaker_change_s: float = 0.5
    media_only_s: float = 4.0
    batch_max_s: float = 8.0
    mention_max_s: float = 1.0
    patience_min: float = 0.5
    patience_max: float = 2.0
    unfinished_wait_s: float = 7.0     # a speaker whose last line is visibly unfinished gets this long to go on
    batch_abs_max_s: float = 25.0      # absolute cap for a batch kept open by an unfinished utterance

    @classmethod
    def from_cfg(cls, cfg) -> "DebounceParams":
        p = cls()
        for k in p.__dataclass_fields__:
            v = cfg.get(k) if cfg is not None else None
            if v is not None:
                setattr(p, k, float(v))
        return p


_UNFINISHED_END = re.compile(
    r"([，,、：:；;]|\-\-|然后|就是|但是|可是|不过|因为|所以|而且|还有|那个|就|我跟你说|你知道吗|话说|结果|"
    r"关键是|问题是|比如|像是|如果|要是)\s*$")
_BARE_MENTION = re.compile(r"^\s*(@\S+\s*)+$")


def looks_unfinished(text: Optional[str]) -> bool:
    """True if a message visibly stops before the speaker's utterance is complete (trailing comma/connective, or
    nothing but an @mention). Conservative: a short line without punctuation is NOT treated as unfinished."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_UNFINISHED_END.search(t) or _BARE_MENTION.match(t))


class DebounceTimer:
    """Stateful deadline computation for one batch. Pure: the caller does the waiting.

    Usage: ``t = DebounceTimer(params, first, now, prev_activity_ts, patience, addressed)``;
    ``t.deadline`` is when to close the batch; on every new message call ``t.add(msg, now, addressed)``.
    """

    def __init__(self, p: DebounceParams, first: IncomingMessage, now: float, prev_ts: Optional[float],
                 patience: float = 1.0, addressed: bool = False, rng=None, their_gap: Optional[float] = None):
        self.p = p
        self.k = max(p.patience_min, min(p.patience_max, float(patience or 1.0)))
        self.rng = rng
        self.g = min(3.0, max(0.6, (float(their_gap) / 3.0) ** 0.6)) if their_gap else 1.0
        self.start = now
        quiet = prev_ts is None or (first.ts - prev_ts) > p.quiet_gap_s
        self.kind = "quiet" if quiet else "active"
        wait = p.quiet_start_s if quiet else p.active_start_s
        if first.media_only:
            wait = max(wait, p.media_only_s)
            self.kind += "+media"
        self.hard = now + p.batch_max_s * self.k * self.g
        self.abs_hard = now + max(p.batch_max_s, p.batch_abs_max_s) * self.k * self.g
        self.mention_deadline: Optional[float] = None
        self._deadline = now + self._w(wait)
        self.unfinished_wait = self._w(p.unfinished_wait_s)
        self.last_sender = first.sender_id
        self.open: dict = {}               # speaker -> time their visibly unfinished line arrived
        self._note(first, now)
        if addressed and first.sender_id not in self.open:
            self.mention_deadline = now + self._w(p.mention_max_s, patience=False, pace=False)
            self.kind += "+mention"

    def _w(self, median: float, patience: bool = True, pace: bool = True) -> float:
        m = median * (self.k if patience else 1.0) * (self.g if pace else 1.0)
        if self.rng is None:
            return m
        return math.exp(self.rng.gauss(math.log(max(1e-3, m)), 0.35))

    def _note(self, msg: IncomingMessage, now: float) -> None:
        if looks_unfinished(msg.text) and not msg.media_only:
            self.open[msg.sender_id] = now
        else:
            self.open.pop(msg.sender_id, None)

    @property
    def deadline(self) -> float:
        d = min(self._deadline, self.hard)
        if self.mention_deadline is not None:
            d = min(d, self.mention_deadline)
        if self.open:                       # someone is mid-utterance: wait for them (bounded)
            d = max(d, min(self.abs_hard, max(self.open.values()) + self.unfinished_wait))
        return d

    def add(self, msg: IncomingMessage, now: float, addressed: bool = False) -> None:
        if msg.media_only or (msg.text and T.is_emoji_only(msg.text)):
            # stickers / bare media inside a batch may only bring the deadline forward
            cand = now + self._w(self.p.same_sender_s if msg.sender_id == self.last_sender else self.p.speaker_change_s)
            self._deadline = min(self._deadline, cand)
        elif msg.sender_id == self.last_sender:
            self._deadline = now + self._w(self.p.same_sender_s)
        else:
            self._deadline = now + self._w(self.p.speaker_change_s, pace=False)
        self.last_sender = msg.sender_id
        self._note(msg, now)
        if addressed and msg.sender_id not in self.open:
            md = now + self._w(self.p.mention_max_s, patience=False, pace=False)
            self.mention_deadline = md if self.mention_deadline is None else min(self.mention_deadline, md)


# ---------------------------------------------------------------------------
# In-flight classification (steer / interrupt / queue)
# ---------------------------------------------------------------------------
STEER = "steer"
INTERRUPT = "interrupt"
QUEUE = "queue"
IGNORE = "ignore"


def hint_of(msg: IncomingMessage) -> str:
    """Adapter / test supplied hint: raw['alive_hint'] in {'correction','cancel','topic_change'}; edits count
    as corrections."""
    raw = msg.raw or {}
    h = str(raw.get("alive_hint") or "")
    if not h and raw.get("edited"):
        h = "correction"
    return h


def is_correction_like(msg: IncomingMessage, context_text: str = "") -> bool:
    """Correction, cancellation, or abrupt topic change relative to ``context_text``."""
    h = hint_of(msg)
    if h in ("correction", "cancel", "topic_change"):
        return True
    t = msg.text or ""
    if T.is_correction(t) or T.is_cancel(t):
        return True
    if T.is_topic_shift_marker(t):
        return True
    if context_text and len(t) >= 8 and T.has_question(t) and T.similarity(t, context_text) < 0.05:
        return True
    return False


def classify_inflight(msg: IncomingMessage, participants: set, addressed: bool, context_text: str,
                      phase: str) -> str:
    """Decide what a message arriving during composition/sending does to the in-flight reply.

    - from a participant of the batch being answered (or addressed to me):
        correction / cancellation / topic change -> INTERRUPT
        trivial acknowledgement / sticker         -> IGNORE (stays queued as context for the next batch)
        anything else while composing             -> STEER (merge, recompose before sending)
    - otherwise (other people in a group)         -> QUEUE (next batch)
    """
    if msg.person_key not in participants and not addressed:
        return QUEUE
    if is_correction_like(msg, context_text):
        return INTERRUPT
    if msg.media_only or T.is_trivial_ack(msg.text):
        return IGNORE
    return STEER if phase == "composing" else QUEUE
