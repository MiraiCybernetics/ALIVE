"""Telegram adapter: update parsing (UTF-16 entity offsets, mentions, replies, media, edits) and the
long-poll / send paths against a fake Bot API server."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from alive.core.types import ComposeResult
from alive.runtime.clock import ManualClock
from alive.social.channels.telegram import TelegramAdapter

from tests.social.soc_helpers import decisions, make_kernel


class _K:
    clock = ManualClock()
    social = None
    selfm = None
    cfg = None


def _adapter():
    a = TelegramAdapter(_K(), "tg", {"token": "T"})
    a.identity.user_id, a.identity.username = "999", "xiaoyu_bot"
    return a


def _upd(text, *, chat_type="supergroup", chat_id=-1001, frm=None, entities=None, reply=None, uid=1, **extra):
    m = {"message_id": 50 + uid, "date": 1, "chat": {"id": chat_id, "type": chat_type, "title": "摸鱼群"},
         "from": frm or {"id": 42, "is_bot": False, "first_name": "阿", "last_name": "明"}, "text": text}
    if entities:
        m["entities"] = entities
    if reply:
        m["reply_to_message"] = reply
    m.update(extra)
    return {"update_id": uid, "message": m}


def test_parse_mention_entity_with_utf16_offsets():
    a = _adapter()
    text = "😀 @xiaoyu_bot 你好 @someone"
    # 😀 is 2 UTF-16 units, so the bot mention starts at 3; "@someone" starts at 3+11+4 = 18
    u = _upd(text, entities=[{"type": "mention", "offset": 3, "length": 11},
                             {"type": "mention", "offset": 18, "length": 8}])
    m = a.parse_update(u)
    assert m.mentions_me and not m.mentions_others          # others flag only when I'm not addressed
    assert "@xiaoyu_bot" not in m.text and "你好" in m.text
    assert m.chat_type == "group" and m.chat_id == "-1001" and m.sender_name == "阿 明"
    assert m.raw["chat_title"] == "摸鱼群" and m.msg_id == "51"
    m2 = a.parse_update(_upd("@someone 你好", entities=[{"type": "mention", "offset": 0, "length": 8}]))
    assert not m2.mentions_me and m2.mentions_others


def test_parse_text_mention_command_reply_media_edits_and_channels():
    a = _adapter()
    m = a.parse_update(_upd("小屿 看这里", entities=[{"type": "text_mention", "offset": 0, "length": 2,
                                                   "user": {"id": 999}}]))
    assert m.mentions_me
    m = a.parse_update(_upd("/help@xiaoyu_bot", entities=[{"type": "bot_command", "offset": 0, "length": 16}]))
    assert m.mentions_me
    m = a.parse_update(_upd("对的", reply={"message_id": 7, "from": {"id": 999, "is_bot": True}}))
    assert m.reply_to_me and m.reply_to_msg_id == "7"
    m = a.parse_update(_upd("对的", reply={"message_id": 8, "from": {"id": 43}}))
    assert not m.reply_to_me and m.reply_to_msg_id == "8"
    u = _upd("", photo=[{"file_id": "small"}, {"file_id": "big"}])
    u["message"].pop("text")
    u["message"]["caption"] = "看图"
    m = a.parse_update(u)
    assert m.media == [{"type": "image", "file_id": "big"}] and m.text == "看图"
    u = _upd("", sticker={"file_id": "s1", "emoji": "😂"})
    u["message"].pop("text")
    m = a.parse_update(u)
    assert m.media_only and m.media[0]["type"] == "sticker"
    e = {"update_id": 9, "edited_message": {**_upd("改过了")["message"], "edit_date": 123}}
    m = a.parse_update(e)
    assert m.raw.get("edited") and m.msg_id.endswith("e123")
    assert a.parse_update(_upd("频道消息", chat_type="channel")) is None
    p = a.parse_update(_upd("私聊", chat_type="private", chat_id=42))
    assert p.chat_type == "private" and p.chat_id == "42" and p.person_key == "tg:42"
    b = a.parse_update(_upd("我是 bot", frm={"id": 77, "is_bot": True, "first_name": "Bot"}))
    assert b.is_bot
    assert a.chat_type_of("-100") == "group" and a.chat_type_of("42") == "private"


class FakeBotAPI:
    def __init__(self):
        self.updates: list = []
        self.calls: list = []
        self.new = asyncio.Event()
        self.fail_429 = 0
        self.mid = 1000

    def add(self, upd):
        self.updates.append(upd)
        self.new.set()

    async def handle(self, req):
        method = req.match_info["method"]
        token = req.match_info["token"]
        payload = await req.json() if req.can_read_body else {}
        self.calls.append((method, payload))
        if token != "TOKEN":
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        if method == "getMe":
            return web.json_response({"ok": True, "result": {"id": 999, "is_bot": True, "first_name": "小屿",
                                                             "username": "xiaoyu_bot"}})
        if method == "getUpdates":
            off = int(payload.get("offset", 0))
            ups = [u for u in self.updates if u["update_id"] >= off]
            if not ups:
                self.new.clear()
                try:
                    await asyncio.wait_for(self.new.wait(), min(1.0, float(payload.get("timeout", 0))))
                except asyncio.TimeoutError:
                    pass
                ups = [u for u in self.updates if u["update_id"] >= off]
            return web.json_response({"ok": True, "result": ups})
        if method == "sendMessage":
            if self.fail_429:
                self.fail_429 -= 1
                return web.json_response({"ok": False, "error_code": 429, "description": "Too Many Requests",
                                          "parameters": {"retry_after": 3}}, status=429)
            self.mid += 1
            return web.json_response({"ok": True, "result": {"message_id": self.mid, "text": payload.get("text")}})
        if method in ("sendChatAction", "setMessageReaction", "deleteMessage"):
            return web.json_response({"ok": True, "result": True})
        return web.json_response({"ok": False, "error_code": 404, "description": "Not Found"}, status=404)

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


@pytest.fixture
async def botapi():
    api = FakeBotAPI()
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", api.handle)
    srv = TestServer(app)
    await srv.start_server()
    api.base = str(srv.make_url("")).rstrip("/")
    yield api
    await srv.close()


async def until(k, cond, timeout=15.0, step=0.25):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await k.clock.advance(step)
        await asyncio.sleep(0.01)
    return cond()


def _tg_cfg(api, **kw):
    return [{"kind": "telegram", "name": "tg", "token": "TOKEN", "api_base": api.base, "poll_timeout": 1,
             "groups": {"-1001": {"talk_value": 1.0}}, **kw}]


async def test_long_poll_reply_with_quote_typing_and_offset(tmp_path, botapi):
    k = await make_kernel(tmp_path, channels=_tg_cfg(botapi))
    try:
        tg = k.social.adapter("tg")
        assert await until(k, lambda: tg.connected and tg.identity.username == "xiaoyu_bot")
        botapi.add(_upd("@xiaoyu_bot 今天吃什么", uid=1, entities=[{"type": "mention", "offset": 0, "length": 11}]))
        assert await until(k, lambda: botapi.sent())
        sent = botapi.sent()[0]
        assert sent["chat_id"] == -1001 and sent["text"] == "好的"
        assert sent["reply_parameters"] == {"message_id": 51, "allow_sending_without_reply": True}
        methods = [m for m, _ in botapi.calls]
        assert "sendChatAction" in methods and methods.index("sendChatAction") < methods.index("sendMessage")
        assert tg.offset == 2
        await k.social.flush()
        assert await k.db.kv_get("social", "tg_offset:tg") == 2
        # a reply to my message is detected as reply_to_me
        botapi.add(_upd("哈哈好", uid=2, reply={"message_id": botapi.mid, "from": {"id": 999, "is_bot": True}}))
        assert await until(k, lambda: len(botapi.sent()) == 2)
        d = await decisions(k, "tg:-1001")
        assert d[-1]["mentioned"] is True
    finally:
        await k.stop()


async def test_group_not_in_allowlist_is_ignored_and_private_stranger_gets_pairing(tmp_path, botapi):
    k = await make_kernel(tmp_path, channels=_tg_cfg(botapi))
    try:
        tg = k.social.adapter("tg")
        assert await until(k, lambda: tg.connected)
        botapi.add(_upd("@xiaoyu_bot hi", uid=1, chat_id=-2002, entities=[{"type": "mention", "offset": 0, "length": 11}]))
        botapi.add(_upd("你好", uid=2, chat_type="private", chat_id=42))
        assert await until(k, lambda: botapi.sent())
        await until(k, lambda: False, timeout=0.5)
        sent = botapi.sent()
        assert len(sent) == 1 and sent[0]["chat_id"] == 42 and "配对码" in sent[0]["text"]
        assert k.social.counters.get("in.group_not_allowed") == 1
        code = k.social.list_pairings("tg")[0]["code"]
        await k.social.approve_pairing(code)
        botapi.add(_upd("我是朋友", uid=3, chat_type="private", chat_id=42))
        assert await until(k, lambda: len(botapi.sent()) == 2)
        assert botapi.sent()[1]["text"] == "好的"
    finally:
        await k.stop()


async def test_429_retry_after_and_reactions(tmp_path, botapi):
    k = await make_kernel(tmp_path, channels=_tg_cfg(botapi, group_policy="open"))
    try:
        tg = k.social.adapter("tg")
        assert await until(k, lambda: tg.connected)
        botapi.fail_429 = 1
        botapi.add(_upd("@xiaoyu_bot 在吗", uid=1, entities=[{"type": "mention", "offset": 0, "length": 11}]))
        assert await until(k, lambda: botapi.sent() and any(m == "sendMessage" and p.get("text") for m, p in botapi.calls
                                                             if m == "sendMessage") and botapi.mid > 1000)
        assert sum(1 for m, _ in botapi.calls if m == "sendMessage") == 2      # retried after retry_after

        async def react(task):
            return ComposeResult(reaction="👍")
        k.mind.compose_reply = react
        botapi.add(_upd("@xiaoyu_bot 我考过了", uid=2, entities=[{"type": "mention", "offset": 0, "length": 11}]))
        assert await until(k, lambda: any(m == "setMessageReaction" for m, _ in botapi.calls))
        p = next(p for m, p in botapi.calls if m == "setMessageReaction")
        assert p["reaction"] == [{"type": "emoji", "emoji": "👍"}] and p["message_id"] == 52
    finally:
        await k.stop()


async def test_bad_token_backs_off_without_crashing(tmp_path, botapi):
    k = await make_kernel(tmp_path, channels=[{"kind": "telegram", "name": "tg", "token": "WRONG",
                                               "api_base": botapi.base, "poll_timeout": 1}])
    try:
        tg = k.social.adapter("tg")
        assert await until(k, lambda: tg.stats["errors"] >= 1, timeout=5)
        n = len(botapi.calls)
        await k.clock.advance(100)            # sleeping 5 min before retrying
        await asyncio.sleep(0.1)
        assert len(botapi.calls) == n and not tg.connected
    finally:
        await k.stop()
