"""OneBot v11 forward-WebSocket adapter (NapCat, Lagrange.OneBot, go-cqhttp compatible).

Config: {"kind": "onebot", "name": "qq", "url": "ws://127.0.0.1:3001", "access_token": "...",
         "names": ["小屿"], "bots": ["<qq of other bots>"], "at_on_reply": false, "napcat_input_status": false,
         "dm_policy": "pairing", "group_policy": "allowlist", "groups": {"123456": {"talk_value": 0.5}}}

- One WebSocket carries both events and API calls; calls are matched to responses by ``echo``.
- Reconnects with exponential backoff (kernel clock); pending calls fail fast on disconnect.
- Self identity from ``get_login_info``. Mention = ``at`` segment with my QQ (or a name mention, resolved by
  the social layer); replies to my messages are detected from the message ids I sent (LRU).
- chat ids are prefixed to keep namespaces apart: groups "g<group_id>", private chats "p<user_id>".
- Message formats: segment arrays (preferred) and CQ-code strings.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
from collections import OrderedDict
from typing import Optional

from alive.core.types import IncomingMessage
from alive.social.channels.base import Backoff, ChannelAdapter, split_text

log = logging.getLogger("alive.social")

_CQ = re.compile(r"\[CQ:([A-Za-z0-9_\-.]+)((?:,[^\]]*)?)\]")


def _cq_unescape(s: str, param: bool = False) -> str:
    s = s.replace("&#91;", "[").replace("&#93;", "]")
    if param:
        s = s.replace("&#44;", ",")
    return s.replace("&amp;", "&")


def parse_cq(s: str) -> list:
    """CQ-code string -> OneBot segment list."""
    segs, pos = [], 0
    for m in _CQ.finditer(s or ""):
        if m.start() > pos:
            segs.append({"type": "text", "data": {"text": _cq_unescape(s[pos:m.start()])}})
        data = {}
        for kv in (m.group(2) or "").lstrip(",").split(","):
            if "=" in kv:
                k, _, v = kv.partition("=")
                data[k] = _cq_unescape(v, True)
        segs.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(s or ""):
        segs.append({"type": "text", "data": {"text": _cq_unescape(s[pos:])}})
    return segs


class OneBotError(Exception):
    def __init__(self, retcode, message: str = ""):
        super().__init__(f"onebot retcode={retcode} {message}")
        self.retcode = retcode


class OneBotAdapter(ChannelAdapter):
    kind = "onebot"
    default_dm_policy = "pairing"
    default_group_policy = "allowlist"

    def __init__(self, kernel, name: str, cfg: dict):
        super().__init__(kernel, name, cfg)
        self.url = str(self.cfg.get("url") or "ws://127.0.0.1:3001")
        self.token = self.cfg.get("access_token") or ""
        self.action_timeout = float(self.cfg.get("action_timeout_s", 15.0))
        self.at_on_reply = bool(self.cfg.get("at_on_reply", False))
        self.input_status = bool(self.cfg.get("napcat_input_status", False))
        self.bots = {str(x) for x in (self.cfg.get("bots") or [])}
        self.caps.typing = self.input_status
        self.caps.reactions = False
        self.caps.recall = True
        self.caps.max_text = int(self.cfg.get("max_text", 3000))
        self.backoff = Backoff(float(self.cfg.get("backoff_base_s", 1.0)), float(self.cfg.get("backoff_cap_s", 60.0)))
        self._session = None
        self._ws = None
        self._task = None
        self._pending: dict = {}
        self._echo = itertools.count(1)
        self._my_ids: "OrderedDict[str, None]" = OrderedDict()
        self._group_names: dict = {}
        self._title_fetch: set = set()
        self._bg: set = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.running = True
        self._task = self.k.scheduler.spawn(self._run, f"soc.onebot:{self.name}", restart=True)

    async def stop(self) -> None:
        self.running = False
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task.task, return_exceptions=True)
        self._fail_pending(ConnectionError("stopped"))
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self.connected = False

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _run(self) -> None:
        import aiohttp
        while self.running:
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(trust_env=True)
                await self._session_once()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, OneBotError, ConnectionError) as e:
                self.stats["errors"] += 1
                log.info("onebot %s: connection error %r", self.name, e)
            if not self.running:
                return
            self.stats["reconnects"] += 1
            await self.k.clock.sleep(self.backoff.next())

    async def _session_once(self) -> None:
        import aiohttp
        async with self._session.ws_connect(self.url, headers=self._headers(), heartbeat=30.0,
                                            max_msg_size=16 * 1024 * 1024) as ws:
            self._ws = ws
            reader = asyncio.ensure_future(self._recv(ws))
            try:
                info = await self.call("get_login_info", {})
                self.identity.user_id = str(info.get("user_id") or self.identity.user_id)
                nick = info.get("nickname")
                if nick and nick not in self.identity.display_names:
                    self.identity.display_names.append(nick)
                self.connected = True
                self.backoff.reset()
                log.info("onebot %s connected as %s (%s)", self.name, self.identity.user_id, nick)
                await reader
            finally:
                if not reader.done():
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                self._ws = None
                self.connected = False
                self._fail_pending(ConnectionError("disconnected"))

    async def _recv(self, ws) -> None:
        import aiohttp
        async for m in ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(m.data)
                except Exception:
                    continue
                await self._on_data(data)
            elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _on_data(self, d: dict) -> None:
        echo = d.get("echo")
        if echo is not None and str(echo) in self._pending:
            fut = self._pending.pop(str(echo))
            if not fut.done():
                fut.set_result(d)
            return
        pt = d.get("post_type")
        if pt in ("message", "message_sent"):
            try:
                msg = self.parse_event(d)
            except Exception:
                log.exception("onebot: cannot parse event")
                return
            if msg is not None:
                await self.deliver(msg)
        elif pt == "meta_event" and d.get("meta_event_type") == "lifecycle" and d.get("self_id"):
            self.identity.user_id = self.identity.user_id or str(d.get("self_id"))

    async def call(self, action: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("onebot not connected")
        echo = f"{self.name}-{next(self._echo)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send_str(json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False))
            resp = await asyncio.wait_for(fut, timeout or self.action_timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") == "failed" or (resp.get("retcode") not in (0, None)):
            raise OneBotError(resp.get("retcode"), str(resp.get("wording") or resp.get("message") or resp.get("msg") or ""))
        return resp.get("data") or {}

    # ------------------------------------------------------------------ parsing
    def _remember_mine(self, mid: str) -> None:
        self._my_ids[mid] = None
        while len(self._my_ids) > 2000:
            self._my_ids.popitem(last=False)

    def parse_event(self, d: dict) -> Optional[IncomingMessage]:
        mt = d.get("message_type")
        if mt not in ("private", "group"):
            return None
        uid = str(d.get("user_id", ""))
        self_id = str(d.get("self_id") or self.identity.user_id or "")
        segs = d.get("message")
        if isinstance(segs, str):
            segs = parse_cq(segs)
        segs = segs or []
        parts: list = []
        media: list = []
        mentions_me = mentions_others = reply_to_me = False
        at_all = False
        reply_to = None
        for seg in segs:
            t = seg.get("type")
            data = seg.get("data") or {}
            if t == "text":
                parts.append(str(data.get("text", "")))
            elif t == "at":
                qq = str(data.get("qq", ""))
                if qq == "all":
                    at_all = True
                elif self_id and qq == self_id:
                    mentions_me = True
                else:
                    mentions_others = True
                    parts.append(f"@{data.get('name') or qq} ")
            elif t == "reply":
                reply_to = str(data.get("id", ""))
                reply_to_me = reply_to in self._my_ids
            elif t == "image":
                media.append({"type": "sticker" if str(data.get("sub_type", "0")) == "1" else "image",
                              "url": data.get("url") or data.get("file")})
            elif t == "face":
                media.append({"type": "face", "id": data.get("id")})
            elif t in ("mface", "marketface", "dice", "rps"):
                media.append({"type": "sticker", "id": data.get("id") or data.get("emoji_id")})
            elif t == "record":
                media.append({"type": "voice", "url": data.get("url") or data.get("file")})
            elif t in ("video", "file"):
                media.append({"type": t, "url": data.get("url") or data.get("file")})
            elif t == "forward":
                media.append({"type": "forward", "id": data.get("id")})
            elif t in ("json", "xml", "share", "music"):
                media.append({"type": "card"})
        text = "".join(parts).strip()
        sender = d.get("sender") or {}
        name = str(sender.get("card") or sender.get("nickname") or uid)
        if mt == "group":
            gid = str(d.get("group_id", ""))
            chat_id = f"g{gid}"
            raw = {"group_id": gid, "time": d.get("time"), "at_all": at_all}
            title = self._group_names.get(gid)
            if title:
                raw["chat_title"] = title
            else:
                self._fetch_title(gid)
        else:
            chat_id = f"p{uid}"
            raw = {"time": d.get("time")}
        return IncomingMessage(channel=self.name, chat_id=chat_id, chat_type=mt, sender_id=uid, sender_name=name,
                               text=text, msg_id=str(d.get("message_id", "")), ts=self.k.clock.now(),
                               mentions_me=mentions_me, reply_to_me=reply_to_me, reply_to_msg_id=reply_to,
                               mentions_others=mentions_others and not mentions_me, media=media,
                               is_bot=uid in self.bots, raw=raw)

    def _fetch_title(self, gid: str) -> None:
        if gid in self._title_fetch or self._ws is None:
            return
        self._title_fetch.add(gid)

        async def job():
            try:
                info = await self.call("get_group_info", {"group_id": int(gid)})
                if info.get("group_name"):
                    self._group_names[gid] = str(info["group_name"])
            except Exception:
                self._title_fetch.discard(gid)
        t = asyncio.get_running_loop().create_task(job())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def chat_type_of(self, chat_id: str) -> str:
        return "group" if str(chat_id).startswith("g") else "private"

    # ------------------------------------------------------------------ outbound
    @staticmethod
    def _target(chat_id: str) -> tuple:
        cid = str(chat_id)
        if cid.startswith("g"):
            return "send_group_msg", {"group_id": int(cid[1:])}
        if cid.startswith("p"):
            return "send_private_msg", {"user_id": int(cid[1:])}
        return "send_private_msg", {"user_id": int(cid)}

    async def send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None, at_user: Optional[str] = None,
                        **kw) -> str:
        action, params = self._target(chat_id)
        last = ""
        for i, part in enumerate(split_text(text, self.caps.max_text)):
            segs = []
            if reply_to and i == 0:
                segs.append({"type": "reply", "data": {"id": str(reply_to)}})
            if at_user and i == 0 and action == "send_group_msg":
                segs.append({"type": "at", "data": {"qq": str(at_user)}})
                segs.append({"type": "text", "data": {"text": " "}})
            segs.append({"type": "text", "data": {"text": part}})
            data = await self.call(action, {**params, "message": segs})
            last = str(data.get("message_id", ""))
            if last:
                self._remember_mine(last)
            self.stats["out"] += 1
        return last

    async def send_typing(self, chat_id: str) -> None:
        if not self.input_status or not str(chat_id).startswith("p"):
            return
        try:
            await self.call("set_input_status", {"user_id": int(str(chat_id)[1:]), "event_type": 1}, timeout=5)
        except Exception as e:
            log.debug("onebot input status failed: %r", e)

    async def recall(self, chat_id: str, msg_id: str) -> bool:
        try:
            await self.call("delete_msg", {"message_id": int(msg_id)})
            return True
        except Exception:
            return False
