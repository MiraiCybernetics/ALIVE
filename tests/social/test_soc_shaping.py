"""Inbound shaping: dedupe, debounce timings (MaiBot natural profile), @ fast path, inbox bounds."""
import pytest

from alive.core.types import EV, IncomingMessage
from alive.social.shaping import DebounceParams, DebounceTimer, Dedupe, classify_inflight, INTERRUPT, STEER, QUEUE, IGNORE

from tests.social.soc_helpers import decisions, events, make_kernel


def _m(text="hi", sender="a", ts=100.0, media=None, chat_type="group"):
    return IncomingMessage(channel="sim", chat_id="g1", chat_type=chat_type, sender_id=sender, sender_name=sender,
                           text=text, msg_id=f"{sender}{ts}", ts=ts, media=media or [])


# --------------------------------------------------------------------------- dedupe
def test_dedupe_ttl_and_cap():
    d = Dedupe(ttl=1200, max_size=3)
    assert not d.seen(("sim", "1"), 0)
    assert d.seen(("sim", "1"), 10)
    assert d.seen(("sim", "1"), 1199)
    assert not d.seen(("sim", "1"), 1201)          # expired after 20 min -> processed again
    for i in range(2, 6):
        d.seen(("sim", str(i)), 1300)
    assert len(d) == 3                               # size cap evicts the oldest
    assert not d.seen(("sim", "2"), 1300)            # evicted -> new again


async def test_duplicate_platform_delivery_processed_once(kern):
    k = kern
    sim = k.social.adapter("sim")
    await sim.inject("你好", sender="u1", msg_id="m1")
    await sim.inject("你好", sender="u1", msg_id="m1")   # platform re-delivery
    await k.clock.advance(60)
    assert len(await events(k, EV.MESSAGE_IN, "sim:u1")) == 1
    assert len(sim.texts("u1")) == 1
    assert k.social.counters.get("in.duplicate") == 1


# --------------------------------------------------------------------------- debounce timer (pure)
def test_debounce_quiet_and_active_start():
    p = DebounceParams()
    t = DebounceTimer(p, _m(ts=100), 100, prev_ts=None)
    assert t.deadline == pytest.approx(102.0) and t.kind == "quiet"
    t = DebounceTimer(p, _m(ts=100), 100, prev_ts=50)          # previous message 50 s ago -> quiet
    assert t.deadline == pytest.approx(102.0)
    t = DebounceTimer(p, _m(ts=100), 100, prev_ts=90)          # 10 s ago -> active start 5 s
    assert t.deadline == pytest.approx(105.0) and t.kind == "active"


def test_debounce_continuation_speaker_change_media_and_cap():
    p = DebounceParams()
    t = DebounceTimer(p, _m(sender="a", ts=100), 100, prev_ts=None)
    t.add(_m(sender="a", ts=101), 101)                          # same sender -> reset to 2 s
    assert t.deadline == pytest.approx(103.0)
    t.add(_m(sender="b", ts=102), 102)                          # speaker change -> 0.5 s
    assert t.deadline == pytest.approx(102.5)
    # media-only start waits >= 4 s
    t = DebounceTimer(p, _m(text="", media=[{"type": "image"}], ts=100), 100, prev_ts=None)
    assert t.deadline == pytest.approx(104.0)
    # a sticker inside a batch may only shorten the wait
    t = DebounceTimer(p, _m(sender="a", ts=100), 100, prev_ts=90)   # active: 105
    t.add(_m(text="", sender="a", media=[{"type": "sticker"}], ts=104.5), 104.5)
    assert t.deadline == pytest.approx(105.0)                   # min(105, 106.5)
    # whole batch capped at 8 s
    t = DebounceTimer(p, _m(sender="a", ts=100), 100, prev_ts=None)
    for s in (101.5, 103, 104.5, 106, 107.5):
        t.add(_m(sender="a", ts=s), s)
    assert t.deadline == pytest.approx(108.0)


def test_debounce_mention_cap_and_patience():
    p = DebounceParams()
    t = DebounceTimer(p, _m(ts=100), 100, prev_ts=90, addressed=True)
    assert t.deadline == pytest.approx(101.0)                   # @ -> at most 1 s
    t.add(_m(sender="a", ts=100.5), 100.5)                      # continuation cannot extend past the cap
    assert t.deadline == pytest.approx(101.0)
    t = DebounceTimer(p, _m(ts=100), 100, prev_ts=None, patience=1.5)
    assert t.deadline == pytest.approx(103.0)                   # 2 s × patience 1.5
    t = DebounceTimer(p, _m(ts=100), 100, prev_ts=None, patience=9)
    assert t.deadline == pytest.approx(104.0)                   # patience clipped to 2.0


def test_inflight_classification():
    parts = {"sim:a"}
    a = lambda text: IncomingMessage("sim", "g1", "group", "a", "a", text, "x", 0)  # noqa: E731
    b = IncomingMessage("sim", "g1", "group", "b", "b", "今天吃啥", "y", 0)
    assert classify_inflight(a("不对，我是说明天"), parts, False, "今天去看电影吗", "composing") == INTERRUPT
    assert classify_inflight(a("算了不用了"), parts, False, "", "composing") == INTERRUPT
    assert classify_inflight(a("嗯嗯"), parts, False, "", "composing") == IGNORE
    assert classify_inflight(a("还有我想吃火锅"), parts, False, "", "composing") == STEER
    assert classify_inflight(b, parts, False, "", "composing") == QUEUE


# --------------------------------------------------------------------------- debounce end-to-end (virtual time)
async def _decision_times(k, tid):
    return [(e.ts, e.payload) for e in await events(k, EV.DECISION, tid)]


async def test_private_quiet_start_waits_two_seconds(kern):
    k = kern
    sim = k.social.adapter("sim")
    t0 = k.clock.now()
    await sim.inject("在吗", sender="u1")
    await k.clock.advance(1.9)
    assert await _decision_times(k, "sim:u1") == []
    await k.clock.advance(0.2)
    d = await _decision_times(k, "sim:u1")
    assert len(d) == 1 and d[0][0] == pytest.approx(t0 + 2.0)


async def test_same_sender_continuation_extends_and_merges(kern):
    k = kern
    sim = k.social.adapter("sim")
    t0 = k.clock.now()
    await sim.inject("我跟你说", sender="u1")
    await k.clock.advance(1.5)
    await sim.inject("今天发生了一件事", sender="u1")
    await k.clock.advance(1.5)
    await sim.inject("超级好笑", sender="u1")
    await k.clock.advance(1.9)
    assert await _decision_times(k, "sim:u1") == []
    await k.clock.advance(0.2)
    d = await _decision_times(k, "sim:u1")
    assert len(d) == 1 and d[0][0] == pytest.approx(t0 + 5.0) and d[0][1]["n"] == 3
    task = [c for c in k.mind.calls if c[0] == "compose_reply"][-1][1][0]
    assert [l.text for l in task.new_lines] == ["我跟你说", "今天发生了一件事", "超级好笑"]


async def test_batch_capped_at_eight_seconds(kern):
    k = kern
    sim = k.social.adapter("sim")
    t0 = k.clock.now()
    for i in range(7):
        await sim.inject(f"第{i}句话", sender="u1")
        await k.clock.advance(1.5)
    d = await _decision_times(k, "sim:u1")
    assert d and d[0][0] == pytest.approx(t0 + 8.0)


async def test_group_mention_fast_path_within_one_second(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        await sim.inject("大家好", sender="a", chat_id="g1", chat_type="group")
        await k.clock.advance(10)
        t1 = k.clock.now()
        await sim.inject("@小屿 你觉得呢", sender="b", chat_id="g1", chat_type="group")
        await k.clock.advance(1.05)
        d = [x for x in await decisions(k, "sim:g1") if x["mentioned"]]
        assert len(d) == 1 and d[0]["outcome"] == "respond" and d[0]["reason"] == "addressed_to_me"
        evs = [e for e in await events(k, EV.DECISION, "sim:g1") if e.payload["mentioned"]]
        assert evs[0].ts <= t1 + 1.0 + 1e-6
        # a vocative name mention without '@' is also addressed
        await k.clock.advance(60)
        await sim.inject("小屿，帮我看看这个", sender="c", chat_id="g1", chat_type="group", mentions_me=False)
        await k.clock.advance(1.05)
        d = [x for x in await decisions(k, "sim:g1") if x["mentioned"]]
        assert len(d) == 2
    finally:
        await k.stop()


async def test_one_thread_failure_does_not_affect_others(kern):
    k = kern
    sim = k.social.adapter("sim")
    orig = k.mind.compose_reply

    async def flaky(task):
        if task.thread_id == "sim:bad":
            raise RuntimeError("boom")
        return await orig(task)
    k.mind.compose_reply = flaky
    await sim.inject("hi", sender="bad")
    await sim.inject("你好", sender="good")
    await k.clock.advance(30)
    assert sim.texts("good") == ["好的"] and sim.texts("bad") == []
    # the failed thread's actor survives and handles the next batch
    k.mind.compose_reply = orig
    await sim.inject("还在吗", sender="bad")
    await k.clock.advance(30)
    assert sim.texts("bad") == ["好的"]


async def test_idle_actor_is_garbage_collected_and_recreated(kern):
    k = kern
    sim = k.social.adapter("sim")
    await sim.inject("hi", sender="u1")
    await k.clock.advance(30)
    assert "sim:u1" in k.social._actors
    await k.clock.advance(k.social.idle_gc_s + 10)
    assert "sim:u1" not in k.social._actors
    await sim.inject("又来了", sender="u1")
    await k.clock.advance(30)
    hist = await k.social.history("sim:u1")
    assert [l.text for l in hist] == ["hi", "好的", "又来了", "好的"]   # history reloaded from the database


def test_unfinished_line_keeps_speaker_open_across_cut_in_and_mention():
    """docs/22 §4: completion is per speaker; another speaker or a mention doesn't close half a sentence."""
    from alive.social.shaping import looks_unfinished
    assert looks_unfinished("我跟你说，") and looks_unfinished("然后") and looks_unfinished("@小屿")
    assert not looks_unfinished("好的～") and not looks_unfinished("十二年啊…") and not looks_unfinished("嗯")
    p = DebounceParams()
    t = DebounceTimer(p, _m(sender="a", ts=100, text="我今天去面试，"), 100, prev_ts=90)
    t.add(_m(sender="b", ts=101, text="哈哈哈"), 101)
    assert t.deadline >= 100 + p.unfinished_wait_s - 1e-6        # b cutting in does not close a's utterance
    t.add(_m(sender="a", ts=104, text="结果他们根本没看我简历"), 104)
    assert t.deadline < 104 + p.unfinished_wait_s               # a finished: normal same-sender wait applies
    m = DebounceTimer(p, _m(sender="a", ts=200, text="@小屿"), 200, prev_ts=None, addressed=True)
    assert m.mention_deadline is None and m.deadline >= 200 + p.unfinished_wait_s - 1e-6
    t2 = DebounceTimer(p, _m(sender="a", ts=300, text="就是，"), 300, prev_ts=None)
    for i in range(1, 30):
        t2.add(_m(sender="a", ts=300 + i * 3, text="还有，"), 300 + i * 3)
    assert t2.deadline <= 300 + p.batch_abs_max_s * t2.k + 1e-6  # bounded
