"""OneBot v11 adapter: event parsing (segments and CQ strings) and the forward-WebSocket path (auth,
get_login_info, echo-matched actions, reply/at segments, reconnect) against a fake NapCat-like server."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from alive.runtime.clock import ManualClock
from alive.social.channels.onebot import OneBotAdapter, parse_cq

from tests.social.soc_helpers import decisions, make_kernel


class _K:
    clock = ManualClock()
    social = None
    selfm = None


def _ev(segs, *, mt="group", uid=222, gid=555, mid=1, sender=None):
    d = {"time": 1, "self_id": 10001, "post_type": "message", "message_type": mt, "sub_type": "normal",
         "message_id": mid, "user_id": uid, "message": segs, "sender": sender or {"user_id": uid, "nickname": "阿明",
                                                                                 "card": "明哥"}}
    if mt == "group":
        d["group_id"] = gid
    return d


def test_parse_cq_string():
    segs = parse_cq("[CQ:reply,id=12][CQ:at,qq=10001] 你好&#91;笑&#93;[CQ:image,file=a.jpg,url=http://x/y?a=1&amp;b=2]")
    assert [s["type"] for s in segs] == ["reply", "at", "text", "image"]
    assert segs[2]["data"]["text"] == " 你好[笑]" and segs[3]["data"]["url"] == "http://x/y?a=1&b=2"


def test_parse_segments():
    a = OneBotAdapter(_K(), "qq", {})
    a.identity.user_id = "10001"
    a._my_ids["777"] = None
    m = a.parse_event(_ev([{"type": "reply", "data": {"id": "777"}}, {"type": "at", "data": {"qq": "10001"}},
                           {"type": "text", "data": {"text": " 在吗"}}, {"type": "face", "data": {"id": "14"}}]))
    assert m.mentions_me and m.reply_to_me and m.reply_to_msg_id == "777" and m.text == "在吗"
    assert m.chat_id == "g555" and m.chat_type == "group" and m.sender_name == "明哥" and m.person_key == "qq:222"
    assert m.media == [{"type": "face", "id": "14"}]
    m = a.parse_event(_ev([{"type": "at", "data": {"qq": "333", "name": "小红"}}, {"type": "text", "data": {"text": "你来"}}]))
    assert not m.mentions_me and m.mentions_others and m.text.startswith("@小红")
    m = a.parse_event(_ev("[CQ:at,qq=10001]你好", mt="private", gid=None))
    assert m.chat_id == "p222" and m.chat_type == "private" and m.mentions_me
    m = a.parse_event(_ev([{"type": "image", "data": {"file": "x.gif", "sub_type": 1}}]))
    assert m.media_only and m.media[0]["type"] == "sticker"
    m = a.parse_event(_ev([{"type": "at", "data": {"qq": "all"}}, {"type": "text", "data": {"text": "开会"}}]))
    assert not m.mentions_me and m.raw["at_all"]
    assert a.parse_event({"post_type": "message", "message_type": "guild"}) is None
    assert a.chat_type_of("g1") == "group" and a._target("g555") == ("send_group_msg", {"group_id": 555})
    assert a._target("p222") == ("send_private_msg", {"user_id": 222})


class FakeOneBot:
    def __init__(self, token="secret"):
        self.token = token
        self.ws: list = []
        self.actions: list = []
        self.auth: list = []
        self.mid = 9000
        self.connected = asyncio.Event()

    async def handle(self, req):
        ok = req.headers.get("Authorization") == f"Bearer {self.token}"
        self.auth.append(ok)
        if not ok:
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws.append(ws)
        self.connected.set()
        async for m in ws:
            d = json.loads(m.data)
            self.actions.append(d)
            act = d.get("action")
            if act == "get_login_info":
                data = {"user_id": 10001, "nickname": "小屿"}
            elif act in ("send_group_msg", "send_private_msg"):
                self.mid += 1
                data = {"message_id": self.mid}
            elif act == "get_group_info":
                data = {"group_id": d["params"]["group_id"], "group_name": "测试群"}
            else:
                data = {}
            await ws.send_str(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": d.get("echo")}))
        return ws

    async def push(self, ev):
        await self.ws[-1].send_str(json.dumps(ev, ensure_ascii=False))

    def sends(self):
        return [a for a in self.actions if a["action"].startswith("send_")]


@pytest.fixture
async def napcat():
    fake = FakeOneBot()
    app = web.Application()
    app.router.add_get("/", fake.handle)
    srv = TestServer(app)
    await srv.start_server()
    fake.url = str(srv.make_url("/")).replace("http://", "ws://")
    yield fake
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


def _cfg(fake, **kw):
    return [{"kind": "onebot", "name": "qq", "url": fake.url, "access_token": "secret", "groups": ["555"], **kw}]


async def test_connect_login_and_group_reply_with_reply_segment(tmp_path, napcat):
    k = await make_kernel(tmp_path, channels=_cfg(napcat, at_on_reply=True))
    try:
        qq = k.social.adapter("qq")
        assert await until(k, lambda: qq.connected and qq.identity.user_id == "10001")
        assert napcat.auth == [True]
        await napcat.push(_ev([{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 今天几号"}}],
                              mid=321))
        assert await until(k, lambda: napcat.sends())
        s = napcat.sends()[0]
        assert s["action"] == "send_group_msg" and s["params"]["group_id"] == 555
        segs = s["params"]["message"]
        assert segs[0] == {"type": "reply", "data": {"id": "321"}}
        assert segs[1] == {"type": "at", "data": {"qq": "222"}} and segs[-1]["data"]["text"] == "好的"
        # the group title was fetched lazily and the reply to my message is recognised
        assert await until(k, lambda: any(a["action"] == "get_group_info" for a in napcat.actions))
        await napcat.push(_ev([{"type": "reply", "data": {"id": str(napcat.mid)}}, {"type": "text", "data": {"text": "谢谢"}}],
                              mid=322))
        assert await until(k, lambda: len(napcat.sends()) == 2)
        assert (await decisions(k, "qq:g555"))[-1]["mentioned"] is True
        assert k.social._threads["qq:g555"].title == "测试群"
    finally:
        await k.stop()


async def test_private_and_ignoring_my_own_messages(tmp_path, napcat):
    k = await make_kernel(tmp_path, channels=_cfg(napcat, dm_policy="open"))
    try:
        qq = k.social.adapter("qq")
        assert await until(k, lambda: qq.connected)
        own = _ev([{"type": "text", "data": {"text": "我自己发的"}}], mt="private", uid=10001, mid=5)
        own["post_type"] = "message_sent"
        await napcat.push(own)
        await napcat.push(_ev([{"type": "text", "data": {"text": "在吗"}}], mt="private", uid=222, mid=6))
        assert await until(k, lambda: napcat.sends())
        await until(k, lambda: False, timeout=0.5)
        sends = napcat.sends()
        assert len(sends) == 1 and sends[0]["action"] == "send_private_msg" and sends[0]["params"]["user_id"] == 222
    finally:
        await k.stop()


async def test_reconnects_after_server_drops(tmp_path, napcat):
    k = await make_kernel(tmp_path, channels=_cfg(napcat))
    try:
        qq = k.social.adapter("qq")
        assert await until(k, lambda: qq.connected)
        await napcat.ws[-1].close()
        assert await until(k, lambda: not qq.connected, timeout=5)
        assert await until(k, lambda: qq.connected and len(napcat.ws) == 2, timeout=15)
        assert qq.stats["reconnects"] >= 1
        assert sum(1 for a in napcat.actions if a["action"] == "get_login_info") == 2
        with pytest.raises(Exception):
            await OneBotAdapter(k, "x", {"url": napcat.url}).call("get_status")   # not connected -> fails fast
    finally:
        await k.stop()


async def test_wrong_token_is_rejected_and_retried(tmp_path, napcat):
    k = await make_kernel(tmp_path, channels=[{"kind": "onebot", "name": "qq", "url": napcat.url,
                                               "access_token": "nope"}])
    try:
        assert await until(k, lambda: len(napcat.auth) >= 2, timeout=15)
        assert not any(napcat.auth) and not k.social.adapter("qq").connected
    finally:
        await k.stop()
