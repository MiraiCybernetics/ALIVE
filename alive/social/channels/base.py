"""Channel adapter base class.

An adapter owns one IM connection. It normalises platform events into ``IncomingMessage`` and hands them to
``kernel.social.submit_incoming``; it exposes outbound primitives (text, typing, reactions, recall), capability
flags, platform quotas and its own identity (for mention detection and "never reply to myself").

Adapters never decide anything: deciding, delaying and governance checks are the social layer's job.
Network loops must reconnect with backoff (``Backoff``) and use ``kernel.clock.sleep`` for waits.
"""
from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass, field
from typing import Optional

from alive.core.types import IncomingMessage

log = logging.getLogger("alive.social")


@dataclass
class Capabilities:
    typing: bool = False
    reactions: bool = False
    recall: bool = False
    reply: bool = True
    max_text: int = 4000          # longer texts are split by the adapter


@dataclass
class Quotas:
    """Platform-imposed limits the initiative engine must treat as hard constraints (docs/07 §5.4).
    None = no platform limit known."""
    proactive_private_per_day: Optional[int] = None
    proactive_group_per_month: Optional[int] = None
    proactive_group_per_day: Optional[int] = None
    messages_per_second: Optional[float] = None
    note: str = ""


@dataclass
class SelfIdentity:
    user_id: str = ""
    username: str = ""
    display_names: list = field(default_factory=list)


class Backoff:
    """Exponential backoff with full jitter: base·2^n capped, reset on success."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, rng: Optional[random.Random] = None):
        self.base, self.cap = float(base), float(cap)
        self.n = 0
        self.rng = rng or random.Random()

    def next(self) -> float:
        d = min(self.cap, self.base * (2 ** min(self.n, 30)))
        if self.n < 30:
            self.n += 1
        return d * (0.5 + 0.5 * self.rng.random())

    def reset(self) -> None:
        self.n = 0


class ChannelAdapter:
    kind = "base"
    default_dm_policy = "open"        # open | pairing  (strangers in private chats)
    default_group_policy = "open"     # open | allowlist | disabled

    def __init__(self, kernel, name: str, cfg: dict):
        self.k = kernel
        self.name = name
        self.cfg = dict(cfg or {})
        self.caps = Capabilities()
        q = self.cfg.get("quotas") or {}
        self.quotas = Quotas(**{k2: v for k2, v in q.items() if k2 in Quotas.__dataclass_fields__})
        self.identity = SelfIdentity(display_names=list(self.cfg.get("names") or []))
        self.running = False
        self.connected = False
        self.stats = {"in": 0, "out": 0, "errors": 0, "reconnects": 0}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    # ------------------------------------------------------------------ outbound
    async def send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None, **kw) -> str:
        """Send one message; return the platform message id (str). Raise on failure."""
        raise NotImplementedError

    async def send_typing(self, chat_id: str) -> None:
        """Show a typing indicator if supported (no-op otherwise)."""
        return None

    async def react(self, chat_id: str, msg_id: str, emoji: str) -> bool:
        return False

    async def recall(self, chat_id: str, msg_id: str) -> bool:
        return False

    # ------------------------------------------------------------------ inbound
    async def deliver(self, msg: IncomingMessage) -> None:
        self.stats["in"] += 1
        social = self.k.social
        if social is None:
            return
        try:
            await social.submit_incoming(msg)
        except Exception:
            self.stats["errors"] += 1
            log.exception("%s: submit_incoming failed", self.name)

    def all_names(self) -> list:
        names = list(self.identity.display_names)
        if self.identity.username:
            names.append(self.identity.username)
        try:
            if self.k.selfm is not None:
                names.append(self.k.selfm.name())
        except Exception:
            pass
        return [n for n in dict.fromkeys(names) if n]

    def at_mentions_me(self, text: str) -> bool:
        """Plain-text '@name' detection for channels without structured mentions."""
        t = (text or "").lower()
        return any(("@" + n.lower()) in t for n in self.all_names())

    def chat_type_of(self, chat_id: str) -> str:
        """Best guess of a chat's type from its id (used when sending to a thread never seen inbound)."""
        return "private"

    @property
    def dm_policy(self) -> str:
        return str(self.cfg.get("dm_policy") or self.default_dm_policy)

    @property
    def group_policy(self) -> str:
        return str(self.cfg.get("group_policy") or self.default_group_policy)

    def status(self) -> dict:
        return {"kind": self.kind, "running": self.running, "connected": self.connected,
                "identity": asdict(self.identity), "caps": asdict(self.caps), **self.stats}


def split_text(text: str, limit: int) -> list:
    """Split over-long text at newlines/spaces so each part fits the platform limit."""
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts
