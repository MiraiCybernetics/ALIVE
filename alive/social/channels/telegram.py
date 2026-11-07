"""Telegram Bot API adapter (long polling with aiohttp).

Config: {"kind": "telegram", "name": "tg", "token": "..." | "token_env": "TELEGRAM_BOT_TOKEN",
         "api_base": "https://api.telegram.org", "poll_timeout": 30, "proxy": null, "edits": true,
         "dm_policy": "pairing", "group_policy": "allowlist", "groups": {"-100123": {"talk_value": 0.5}}}

- getUpdates long polling with a persisted offset (kv "social", "tg_offset:<name>"), exponential backoff on
  network errors, ``retry_after`` on 429, long sleep on 401/404 (bad token), backoff on 409 (another poller).
- Mentions: "@botusername" mention entities (offsets are UTF-16 code units), text_mention of the bot,
  "/cmd@botusername" commands, and replies to the bot's messages. Other mentions -> mentions_others.
- sendMessage with reply_parameters (allow_sending_without_reply), sendChatAction typing, setMessageReaction,
  deleteMessage. HTTP goes through aiohttp with trust_env=True (HTTP(S)_PROXY respected) or an explicit proxy.
- Group bots only see all messages if privacy mode is disabled in @BotFather; otherwise only mentions/replies.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from alive.core.types import IncomingMessage
from alive.social.channels.base import Backoff, ChannelAdapter, split_text

log = logging.getLogger("alive.social")


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: Optional[float] = None):
        super().__init__(f"telegram {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


def _utf16_slice(text: str, offset: int, length: int) -> str:
    b = text.encode("utf-16-le")
    return b[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


class TelegramAdapter(ChannelAdapter):
    kind = "telegram"
    default_dm_policy = "pairing"
    default_group_policy = "allowlist"

    def __init__(self, kernel, name: str, cfg: dict):
        super().__init__(kernel, name, cfg)
        self.token = self.cfg.get("token") or os.environ.get(self.cfg.get("token_env") or "TELEGRAM_BOT_TOKEN") or ""
        self.api_base = str(self.cfg.get("api_base") or "https://api.telegram.org").rstrip("/")
        self.poll_timeout = int(self.cfg.get("poll_timeout", 30))
        self.proxy = self.cfg.get("proxy")
        self.accept_edits = bool(self.cfg.get("edits", True))
        self.caps.typing = True
        self.caps.reactions = True
        self.caps.recall = True
        self.caps.max_text = 4096
        self.offset = 0
        self._session = None
        self._task = None
        self.backoff = Backoff(float(self.cfg.get("backoff_base_s", 1.0)), float(self.cfg.get("backoff_cap_s", 60.0)))

    # ------------------------------------------------------------------ HTTP
    async def _sess(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    async def call(self, method: str, payload: Optional[dict] = None, timeout: float = 30.0):
        """POST a Bot API method; returns ``result`` or raises TelegramError / aiohttp errors."""
        import aiohttp
        sess = await self._sess()
        url = f"{self.api_base}/bot{self.token}/{method}"
        async with sess.post(url, json=payload or {}, timeout=aiohttp.ClientTimeout(total=timeout),
                             proxy=self.proxy) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                raise TelegramError(r.status, (await r.text())[:200])
        if not isinstance(data, dict) or not data.get("ok"):
            d = data if isinstance(data, dict) else {}
            params = d.get("parameters") or {}
            raise TelegramError(int(d.get("error_code") or r.status), str(d.get("description") or "error"),
                                params.get("retry_after"))
        return data.get("result")

    async def call_retry(self, method: str, payload: dict, attempts: int = 3):
        import aiohttp
        last: Optional[Exception] = None
        for i in range(attempts):
            try:
                return await self.call(method, payload)
            except TelegramError as e:
                last = e
                if e.code == 429:
                    await self.k.clock.sleep(float(e.retry_after or 1.0))
                    continue
                if e.code >= 500:
                    await self.k.clock.sleep(min(10.0, 0.5 * 2 ** i))
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                last = e
                await self.k.clock.sleep(min(10.0, 0.5 * 2 ** i))
        raise last if last else RuntimeError("telegram call failed")

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self.token:
            log.error("telegram channel %s has no token (token / token_env)", self.name)
            return
        self.running = True
        try:
            self.offset = int(await self.k.db.kv_get("social", f"tg_offset:{self.name}", 0) or 0)
        except Exception:
            self.offset = 0
        self._task = self.k.scheduler.spawn(self._poll_loop, f"soc.telegram:{self.name}", restart=True)

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.gather(self._task.task, return_exceptions=True)
            except Exception:
                pass
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self.connected = False

    async def _identify(self) -> None:
        me = await self.call("getMe", {}, timeout=20)
        self.identity.user_id = str(me.get("id"))
        self.identity.username = me.get("username") or ""
        first = me.get("first_name") or ""
        if first and first not in self.identity.display_names:
            self.identity.display_names.append(first)

    async def _poll_loop(self) -> None:
        import aiohttp
        while self.running:
            try:
                if not self.identity.user_id:
                    await self._identify()
                updates = await self.call("getUpdates", {"offset": self.offset, "timeout": self.poll_timeout,
                                                          "allowed_updates": ["message", "edited_message"]},
                                          timeout=self.poll_timeout + 15)
                self.connected = True
                self.backoff.reset()
                last = self.offset
                for u in updates or []:
                    self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
                    try:
                        msg = self.parse_update(u)
                    except Exception:
                        log.exception("telegram: cannot parse update %s", u.get("update_id"))
                        continue
                    if msg is not None:
                        await self.deliver(msg)
                if self.offset != last:
                    await self.k.db.kv_set("social", f"tg_offset:{self.name}", self.offset)
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                self.connected = False
                self.stats["errors"] += 1
                if e.code in (401, 404):
                    log.error("telegram %s: bad token (%s); retrying in 5 min", self.name, e.description)
                    await self.k.clock.sleep(300.0)
                elif e.code == 429:
                    await self.k.clock.sleep(float(e.retry_after or 5.0))
                else:
                    log.warning("telegram %s: %s", self.name, e)
                    await self.k.clock.sleep(self.backoff.next())
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.connected = False
                self.stats["errors"] += 1
                self.stats["reconnects"] += 1
                d = self.backoff.next()
                log.info("telegram %s: network error %r; retry in %.1fs", self.name, e, d)
                await self.k.clock.sleep(d)

    # ------------------------------------------------------------------ parsing
    def parse_update(self, u: dict) -> Optional[IncomingMessage]:
        edited = "edited_message" in u
        m = u.get("message") or (u.get("edited_message") if self.accept_edits else None)
        if not m:
            return None
        chat = m.get("chat") or {}
        ctype = chat.get("type")
        if ctype == "private":
            chat_type = "private"
        elif ctype in ("group", "supergroup"):
            chat_type = "group"
        else:
            return None  # channels are not conversations
        frm = m.get("from") or {}
        sender_id = str(frm.get("id", ""))
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or frm.get("username") or sender_id
        text = m.get("text") or m.get("caption") or ""
        ents = m.get("entities") or m.get("caption_entities") or []
        me_user = (self.identity.username or "").lower()
        me_id = self.identity.user_id
        mentions_me = mentions_others = False
        strip_spans = []
        for e in ents:
            et = e.get("type")
            if et == "mention":
                seg = _utf16_slice(text, int(e.get("offset", 0)), int(e.get("length", 0)))
                if me_user and seg.lower() == "@" + me_user:
                    mentions_me = True
                    strip_spans.append(seg)
                else:
                    mentions_others = True
            elif et == "text_mention":
                uid = str((e.get("user") or {}).get("id", ""))
                if me_id and uid == me_id:
                    mentions_me = True
                else:
                    mentions_others = True
            elif et == "bot_command":
                seg = _utf16_slice(text, int(e.get("offset", 0)), int(e.get("length", 0)))
                if "@" in seg:
                    if me_user and seg.lower().endswith("@" + me_user):
                        mentions_me = True
                elif chat_type == "group":
                    pass  # bare /cmd in a group: addressed to all bots; not a mention of me
        for seg in strip_spans:
            text = text.replace(seg, " ").strip()
        reply = m.get("reply_to_message") or None
        reply_to_me = False
        reply_to_msg_id = None
        if reply:
            reply_to_msg_id = str(reply.get("message_id"))
            rfrom = str((reply.get("from") or {}).get("id", ""))
            reply_to_me = bool(me_id) and rfrom == me_id
        media = []
        if m.get("photo"):
            media.append({"type": "image", "file_id": (m["photo"][-1] or {}).get("file_id")})
        for key, typ in (("sticker", "sticker"), ("animation", "gif"), ("video", "video"), ("voice", "voice"),
                         ("audio", "audio"), ("document", "file"), ("video_note", "video")):
            if m.get(key) and not (key == "document" and m.get("animation")):
                media.append({"type": typ, "file_id": (m[key] or {}).get("file_id"),
                              **({"emoji": m[key].get("emoji")} if key == "sticker" else {})})
        mid = str(m.get("message_id"))
        raw = {"update_id": u.get("update_id"), "date": m.get("date")}
        if chat.get("title"):
            raw["chat_title"] = chat.get("title")
        if edited:
            raw["edited"] = True
            mid = f"{mid}e{m.get('edit_date', '')}"
        return IncomingMessage(channel=self.name, chat_id=str(chat.get("id")), chat_type=chat_type, sender_id=sender_id,
                               sender_name=name, text=text, msg_id=mid, ts=self.k.clock.now(), mentions_me=mentions_me,
                               reply_to_me=reply_to_me, reply_to_msg_id=reply_to_msg_id,
                               mentions_others=mentions_others and not mentions_me, media=media,
                               is_bot=bool(frm.get("is_bot")), raw=raw)

    def chat_type_of(self, chat_id: str) -> str:
        return "group" if str(chat_id).startswith("-") else "private"

    @staticmethod
    def _cid(chat_id: str):
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return chat_id

    # ------------------------------------------------------------------ outbound
    async def send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None, **kw) -> str:
        last_id = ""
        for i, part in enumerate(split_text(text, self.caps.max_text)):
            payload = {"chat_id": self._cid(chat_id), "text": part}
            if reply_to and i == 0:
                try:
                    payload["reply_parameters"] = {"message_id": int(str(reply_to).split("e")[0]),
                                                   "allow_sending_without_reply": True}
                except ValueError:
                    pass
            res = await self.call_retry("sendMessage", payload)
            last_id = str((res or {}).get("message_id", ""))
            self.stats["out"] += 1
        return last_id

    async def send_typing(self, chat_id: str) -> None:
        try:
            await self.call("sendChatAction", {"chat_id": self._cid(chat_id), "action": "typing"}, timeout=10)
        except Exception as e:
            log.debug("telegram typing failed: %r", e)

    async def react(self, chat_id: str, msg_id: str, emoji: str) -> bool:
        try:
            await self.call("setMessageReaction", {"chat_id": self._cid(chat_id),
                                                   "message_id": int(str(msg_id).split("e")[0]),
                                                   "reaction": [{"type": "emoji", "emoji": emoji}]}, timeout=10)
            return True
        except Exception as e:
            log.info("telegram reaction %s failed: %r", emoji, e)
            return False

    async def recall(self, chat_id: str, msg_id: str) -> bool:
        try:
            await self.call("deleteMessage", {"chat_id": self._cid(chat_id), "message_id": int(msg_id)}, timeout=10)
            return True
        except Exception:
            return False
