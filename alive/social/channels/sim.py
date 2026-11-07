"""In-memory channel for tests and the life simulator.

    sim = kernel.social.adapter("sim")
    msg = await sim.inject("你好呀", sender="u1", sender_name="小明")              # private chat with u1
    msg = await sim.inject("@小屿 在吗", sender="u2", chat_id="g1", chat_type="group")
    sim.outputs   -> [SentRecord(chat_id, text, reply_to, msg_id, ts)]               (virtual timestamps)
    sim.typing    -> [(chat_id, ts)]
    sim.reactions -> [(chat_id, msg_id, emoji, ts)]
    sim.timeline  -> [("send"|"typing"|"react"|"recall", chat_id, payload, ts)] in order
    sim.on_send = callback(record) (sync or async)  — e.g. the simulator's virtual humans
    sim.fail_sends = n  -> the next n send_text calls raise (error-path tests)
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Callable, Optional

from alive.core.types import IncomingMessage
from alive.social.channels.base import ChannelAdapter, Quotas


@dataclass
class SentRecord:
    chat_id: str
    text: str
    reply_to: Optional[str]
    msg_id: str
    ts: float
    kw: Optional[dict] = None


class SimAdapter(ChannelAdapter):
    kind = "sim"

    def __init__(self, kernel, name: str, cfg: dict):
        super().__init__(kernel, name, cfg)
        self.caps.typing = bool(self.cfg.get("typing", True))
        self.caps.reactions = bool(self.cfg.get("reactions", True))
        self.caps.recall = bool(self.cfg.get("recall", True))
        self.caps.max_text = int(self.cfg.get("max_text", 4000))
        q = self.cfg.get("quotas") or {}
        self.quotas = Quotas(**{k: v for k, v in q.items() if k in Quotas.__dataclass_fields__})
        self.identity.user_id = str(self.cfg.get("self_id", "me"))
        self.identity.username = str(self.cfg.get("username", ""))
        self.outputs: list = []
        self.typing: list = []
        self.reactions: list = []
        self.recalled: list = []
        self.timeline: list = []
        self.on_send: Optional[Callable] = None
        self.fail_sends = 0
        self._ids = itertools.count(1)
        self._in_ids = itertools.count(1)
        self.auto_mention = bool(self.cfg.get("auto_mention", True))

    async def start(self) -> None:
        self.running = True
        self.connected = True

    async def stop(self) -> None:
        self.running = False
        self.connected = False

    # ------------------------------------------------------------------ inbound
    def make_message(self, text: str, *, sender: str = "u1", sender_name: Optional[str] = None,
                     chat_id: Optional[str] = None, chat_type: str = "private", mentions_me: Optional[bool] = None,
                     reply_to_me: bool = False, reply_to_msg_id: Optional[str] = None, mentions_others: bool = False,
                     media: Optional[list] = None, is_bot: bool = False, msg_id: Optional[str] = None,
                     ts: Optional[float] = None, raw: Optional[dict] = None, title: Optional[str] = None
                     ) -> IncomingMessage:
        if chat_id is None:
            chat_id = sender if chat_type == "private" else "g1"
        if mentions_me is None:
            mentions_me = self.auto_mention and self.at_mentions_me(text)
        r = dict(raw or {})
        if title:
            r["chat_title"] = title
        return IncomingMessage(channel=self.name, chat_id=str(chat_id), chat_type=chat_type, sender_id=str(sender),
                               sender_name=sender_name or str(sender), text=text or "",
                               msg_id=msg_id or f"in{next(self._in_ids)}",
                               ts=self.k.clock.now() if ts is None else ts, mentions_me=bool(mentions_me),
                               reply_to_me=reply_to_me, reply_to_msg_id=reply_to_msg_id,
                               mentions_others=mentions_others, media=list(media or []), is_bot=is_bot, raw=r or None)

    async def inject(self, text: str = "", **kw) -> IncomingMessage:
        """Build an IncomingMessage (see ``make_message``) and deliver it through submit_incoming."""
        msg = self.make_message(text, **kw)
        await self.deliver(msg)
        return msg

    async def inject_message(self, msg: IncomingMessage) -> None:
        await self.deliver(msg)

    # ------------------------------------------------------------------ outbound
    async def send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None, **kw) -> str:
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise RuntimeError("sim send failure")
        mid = f"out{next(self._ids)}"
        rec = SentRecord(str(chat_id), text, reply_to, mid, self.k.clock.now(), kw or None)
        self.outputs.append(rec)
        self.timeline.append(("send", str(chat_id), text, rec.ts))
        self.stats["out"] += 1
        cb = self.on_send
        if cb is not None:
            res = cb(rec)
            if asyncio.iscoroutine(res):
                await res
        return mid

    async def send_typing(self, chat_id: str) -> None:
        if not self.caps.typing:
            return
        ts = self.k.clock.now()
        self.typing.append((str(chat_id), ts))
        self.timeline.append(("typing", str(chat_id), None, ts))

    async def react(self, chat_id: str, msg_id: str, emoji: str) -> bool:
        if not self.caps.reactions:
            return False
        ts = self.k.clock.now()
        self.reactions.append((str(chat_id), str(msg_id), emoji, ts))
        self.timeline.append(("react", str(chat_id), (msg_id, emoji), ts))
        return True

    async def recall(self, chat_id: str, msg_id: str) -> bool:
        self.recalled.append((str(chat_id), str(msg_id)))
        self.timeline.append(("recall", str(chat_id), msg_id, self.k.clock.now()))
        return True

    def chat_type_of(self, chat_id: str) -> str:
        return "group" if str(chat_id).startswith("g") else "private"

    # ------------------------------------------------------------------ helpers for tests
    def texts(self, chat_id: Optional[str] = None) -> list:
        return [r.text for r in self.outputs if chat_id is None or r.chat_id == str(chat_id)]

    def clear(self) -> None:
        self.outputs.clear()
        self.typing.clear()
        self.reactions.clear()
        self.timeline.clear()
