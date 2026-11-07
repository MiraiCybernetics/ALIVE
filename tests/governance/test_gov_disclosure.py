"""AI-identity disclosure reminders (docs/09 §7.1): first contact, every 2 h (cn) / 3 h (ny) of continuous
use per thread, session gaps, pending de-duplication."""
from tests.governance import govhelp
from alive.core.types import EV

M = 60.0
H = 3600.0


async def chat(k, thread, minutes, every=10):
    """Simulate continuous use: an incoming message every ``every`` minutes."""
    for _ in range(int(minutes / every)):
        await k.clock.advance(every * M)
        await k.bus.emit(EV.MESSAGE_IN, {"channel": "tg", "chat_id": thread.split(":")[1], "sender_id": "L",
                                         "chat_type": "private", "text": "嗯嗯", "ts": k.clock.now()},
                         thread_id=thread)
    await k.bus.drain()


async def test_first_contact_then_every_two_hours(tmp_path):
    k = await govhelp.kernel(tmp_path)
    try:
        g = k.gov
        th = "tg:L"
        d = await g.pre_send(th, "tg:L", "你好呀", {})
        assert d.allow and d.rule == "disclosure_attached" and "我是 AI" in d.text and d.text.startswith("你好呀")
        assert g.disclosure_due(th, k.clock.now()) is None          # attached, pending confirmation
        g.disclosure_sent(th, k.clock.now())
        await chat(k, th, 110)
        assert g.disclosure_due(th, k.clock.now()) is None
        await chat(k, th, 20)                                        # > 2 h of continuous use
        rem = g.disclosure_due(th, k.clock.now())
        assert rem and "我是 AI" in rem
        d = await g.pre_send(th, "tg:L", "哈哈哈", {})
        assert d.text.endswith(rem)
        g.disclosure_sent(th, k.clock.now())
        assert g.disclosure_due(th, k.clock.now()) is None
        await k.clock.settle()
        row = await k.db.fetchone("SELECT count FROM gov_disclosure WHERE thread_id=?", (th,))
        assert row["count"] == 2
    finally:
        await k.stop()


async def test_session_gap_resets_continuous_use(tmp_path):
    k = await govhelp.kernel(tmp_path)
    try:
        g = k.gov
        th = "tg:L"
        g.disclosure_sent(th, k.clock.now())
        await chat(k, th, 100)
        await k.clock.advance(45 * M)                                # a break > 30 min
        await chat(k, th, 60)
        assert g.disclosure_due(th, k.clock.now()) is None           # only 1 h since the new session began
        await chat(k, th, 70)
        assert g.disclosure_due(th, k.clock.now())
        await k.clock.advance(40 * M)                                # idle: not continuous use any more
        assert g.disclosure_due(th, k.clock.now()) is None
    finally:
        await k.stop()


async def test_pending_reminder_reappears_if_not_confirmed(tmp_path):
    k = await govhelp.kernel(tmp_path)
    try:
        g = k.gov
        d = await g.pre_send("tg:L", "tg:L", "在吗", {})
        assert "我是 AI" in d.text
        d2 = await g.pre_send("tg:L", "tg:L", "我刚看到一个好玩的", {})
        assert d2.text == "我刚看到一个好玩的"                        # same burst: not repeated
        await k.clock.advance(6 * M)
        d3 = await g.pre_send("tg:L", "tg:L", "还在吗", {})
        assert "我是 AI" in d3.text                                   # the caller never confirmed sending
    finally:
        await k.stop()


async def test_jurisdictions(tmp_path):
    k = await govhelp.kernel(tmp_path / "ny", cfg={"governance": {"disclosure": {"jurisdiction": "ny"}}})
    try:
        g = k.gov
        g.disclosure_sent("tg:L", k.clock.now())
        await chat(k, "tg:L", 150)
        assert g.disclosure_due("tg:L", k.clock.now()) is None
        await chat(k, "tg:L", 40)
        assert g.disclosure_due("tg:L", k.clock.now())
    finally:
        await k.stop()
    k = await govhelp.kernel(tmp_path / "off", cfg={"governance": {"disclosure": {"jurisdiction": "off",
                                                                                   "on_first_contact": False}}})
    try:
        g = k.gov
        assert g.disclosure_due("tg:L", k.clock.now()) is None
        await chat(k, "tg:L", 300)
        assert g.disclosure_due("tg:L", k.clock.now()) is None
        d = await g.pre_send("tg:L", "tg:L", "嗨", {})
        assert d.text == "嗨"
    finally:
        await k.stop()


async def test_disclosure_state_persists(tmp_path):
    k = await govhelp.kernel(tmp_path)
    k.gov.disclosure_sent("tg:L", k.clock.now())
    await k.stop()
    k2 = await govhelp.kernel(tmp_path)
    try:
        assert k2.gov.disclosure_due("tg:L", k2.clock.now()) is None          # not a first contact any more
        assert k2.gov.disclosure_due("tg:new", k2.clock.now())
    finally:
        await k2.stop()


async def test_caller_managed_disclosure_is_not_duplicated(tmp_path):
    """The social send pipeline appends disclosure_due() as its own segment, marks it with ctx["disclosure"]
    and calls disclosure_sent(); pre_send must then not attach a second copy."""
    k = await govhelp.kernel(tmp_path)
    try:
        g = k.gov
        th = "tg:L"
        rem = g.disclosure_due(th, k.clock.now())
        assert rem
        d = await g.pre_send(th, "tg:L", "你好呀", {"disclosure": False, "segment_index": 0})
        assert d.allow and d.text == "你好呀"
        g.disclosure_sent(th, k.clock.now())
        assert g.disclosure_due(th, k.clock.now()) is None
        d = await g.pre_send(th, "tg:L", "嗯嗯", {"attach_disclosure": False})
        assert d.text == "嗯嗯"
    finally:
        await k.stop()
