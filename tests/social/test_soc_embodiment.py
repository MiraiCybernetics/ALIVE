"""Embodiment: segment splitting, delay model, typing-after-decision, abort on correction, governance hooks,
in-flight steer / interrupt / queue, reactions, outcome summaries."""
import random

import pytest

from alive.core.types import EV, ComposeResult
from alive.interfaces import Decision
from alive.social.embodiment import DelayModel, DelayParams, normalize_segments, split_segments

from tests.social.soc_helpers import decisions, events, make_kernel

LONG = "今天我去了一趟图书馆，借了三本关于记忆的书。其中一本讲海马体怎么把短期记忆变成长期记忆，特别有意思！" \
       "你最近在看什么书吗？有推荐的话告诉我呀。"


# --------------------------------------------------------------------------- splitting
def test_split_sentences_max_three():
    segs = split_segments(LONG)
    assert 1 < len(segs) <= 3
    assert "".join(segs).replace(" ", "") == LONG.replace(" ", "")
    assert all(s.strip() for s in segs)


def test_split_short_text_untouched_and_tiny_pieces_merged():
    assert split_segments("好的呀") == ["好的呀"]
    segs = split_segments("嗯。我觉得这个想法很好，可以试试看。不过要注意时间安排。哈！")
    assert all(len(s) >= 4 for s in segs)
    assert segs[0].startswith("嗯。")


def test_split_never_inside_code_or_urls():
    t = ("看这个链接 https://example.com/a.b?c=1!d=2 里面有说明。然后运行下面的代码：\n```python\nprint('a. b!')\n"
         "x = 1. \n```\n应该就好了！有问题再找我。")
    segs = split_segments(t)
    joined = "\n".join(segs)
    assert "https://example.com/a.b?c=1!d=2" in joined
    assert sum(1 for s in segs if "```" in s) == 1          # the whole fence lives in one segment
    code_seg = next(s for s in segs if "```" in s)
    assert code_seg.count("```") == 2
    assert len(segs) <= 3


def test_split_newlines_and_english():
    segs = split_segments("First line here.\nSecond line is a bit longer. And it continues.\nThird.")
    assert len(segs) <= 3 and segs[0] == "First line here."
    assert normalize_segments(["a", "b", "c", "d", "e"], 3) == ["a", "b", "c\nd\ne"]
    assert normalize_segments([LONG], 3) == split_segments(LONG)


# --------------------------------------------------------------------------- delay model
def test_delay_model_typing_speed_and_style():
    p = DelayParams(jitter_sigma=0.0, type_cjk_cv=1e-6)          # today's speed == the mean (4 CJK chars/s)
    dm = DelayModel(p, random.Random(1))
    t = dm.typing_time("你" * 30, energy=1.0)
    assert t == pytest.approx(30 / 4.0, rel=0.01)
    assert dm.typing_time("hello there my friend", energy=1.0) == pytest.approx(4.0)        # ~1 word/s
    assert dm.typing_time("你" * 30, energy=0.2) > t                                      # tired -> slower
    a = DelayModel(p, random.Random(1)).next_delay("这是一句话", style_delay=1.0)
    b = DelayModel(p, random.Random(1)).next_delay("这是一句话", style_delay=2.0)
    assert b == pytest.approx(2 * a)
    # time already spent thinking is subtracted, but some typing always remains
    d = DelayModel(p, random.Random(2)).first_delay("好的好的", "在吗", already_elapsed=1000, energy=1.0)
    assert d == pytest.approx(p.min_typing_frac * 4 / 4.0, rel=0.01)
    # drowsy reacts slower on average; a closer person a little faster (priority follows the tie)
    fast, slow = DelayModel(rng=random.Random(3)), DelayModel(rng=random.Random(3))
    assert sum(slow.reaction_time("drowsy") for _ in range(300)) > sum(fast.reaction_time("active") for _ in range(300))
    near, far = DelayModel(rng=random.Random(4)), DelayModel(rng=random.Random(4))
    assert sum(near.reaction_time(closeness=0.9) for _ in range(300)) < sum(far.reaction_time(closeness=0.1)
                                                                            for _ in range(300))
    # no behavioural cap: a long message takes long to type; only a technical ceiling remains
    assert dm.first_delay("字" * 200, "", energy=1.0) > 30
    assert dm.first_delay("字" * 5000, "字" * 2000) <= p.physical_cap_s


def test_typing_speed_is_a_daily_draw():
    days: dict = {}
    day = ["d1"]

    def daily(key, sampler):
        k_ = (day[0], key)
        if k_ not in days:
            days[k_] = sampler(rng)
        return days[k_]
    rng = random.Random(7)
    dm = DelayModel(DelayParams(), random.Random(1), daily_fn=daily)
    s1 = {round(dm.typing_speed(1.0), 6) for _ in range(5)}
    assert len(s1) == 1                                      # stable within a day
    speeds = []
    for i in range(40):
        day[0] = f"d{i + 2}"
        speeds.append(dm.typing_speed(1.0))
    assert len({round(x, 4) for x in speeds}) > 30           # differs between days
    assert 3.4 < sum(speeds) / len(speeds) < 4.6             # around the persona mean


# --------------------------------------------------------------------------- typing only after deciding
async def test_typing_only_after_decision_and_never_for_silence(kern):
    k = kern
    sim = k.social.adapter("sim")
    k.mind.reply_text = "好呀，我们周末一起去吧"
    await sim.inject("周末一起去爬山吗？", sender="u1")
    await k.clock.advance(60)
    dec = [e for e in await events(k, EV.DECISION, "sim:u1")]
    assert dec[-1].payload["outcome"] == "respond"
    assert sim.typing and min(ts for _, ts in sim.typing) >= dec[-1].ts
    kinds = [x[0] for x in sim.timeline]
    assert kinds.index("typing") < kinds.index("send")
    # silent decision -> no typing at all
    sim.clear()

    async def silent(task):
        return ComposeResult(silent=True)
    k.mind.compose_reply = silent
    await sim.inject("嗯", sender="u2")
    await k.clock.advance(60)
    assert sim.typing == [] and sim.outputs == []


async def test_send_delays_are_humanlike(kern):
    k = kern
    sim = k.social.adapter("sim")
    k.mind.reply_text = LONG
    await sim.inject("你今天干嘛了", sender="u1")
    t_in = k.clock.now()
    await k.clock.advance(120)
    outs = sim.outputs
    assert 2 <= len(outs) <= 3
    first_gap = outs[0].ts - t_in
    assert 2.0 + 1.0 < first_gap < 2.0 + 30.0          # debounce + reaction/read/typing
    for a, b in zip(outs, outs[1:]):
        assert b.ts - a.ts >= 1.0                        # typing time between segments


# --------------------------------------------------------------------------- abort on correction / topic change
async def test_abort_remaining_segments_on_correction(kern):
    k = kern
    sim = k.social.adapter("sim")
    k.mind.reply_text = LONG
    await sim.inject("你今天干嘛了", sender="u1")
    for _ in range(600):
        await k.clock.advance(0.1)
        if sim.outputs:
            break
    assert len(sim.outputs) == 1
    k.mind.reply_text = "哦哦昨天啊"
    await sim.inject("不对不对，我是问你昨天", sender="u1")
    await k.clock.advance(120)
    ab = await events(k, EV.OUTCOME_ABORTED, "sim:u1")
    assert ab and ab[0].payload["reason"] == "correction" and ab[0].payload["sent"] == 1
    assert ab[0].payload["remaining"] == len(split_segments(LONG)) - 1
    assert sim.texts("u1") == [split_segments(LONG)[0], "哦哦昨天啊"]
    # the correction is then answered as a new batch
    assert any(c[0] == "compose_reply" and "昨天" in c[1][0].new_lines[-1].text for c in k.mind.calls)
    assert "没发完" in " ".join(k.selfm.outcomes)


async def test_unrelated_new_message_does_not_abort(kern):
    k = kern
    sim = k.social.adapter("sim")
    k.mind.reply_text = LONG
    await sim.inject("你今天干嘛了", sender="u1")
    for _ in range(600):
        await k.clock.advance(0.1)
        if sim.outputs:
            break
    await sim.inject("哈哈", sender="u1")
    await k.clock.advance(120)
    assert not await events(k, EV.OUTCOME_ABORTED, "sim:u1")
    assert sim.texts("u1")[: len(split_segments(LONG))] == split_segments(LONG)


async def test_send_api_abort_flag_and_hint(kern):
    k = kern
    sim = k.social.adapter("sim")
    await sim.inject("在吗", sender="u1")
    await k.clock.advance(30)
    sim.clear()

    async def inject_hint():
        await sim.inject("换个话题", sender="u1", raw={"alive_hint": "topic_change"})
    k.scheduler.call_later(0.5, inject_hint, "t.hint")
    res = await _drive(k, k.social.send("sim:u1", ["第一句话比较长一些对吧", "第二句", "第三句也在这里"],
                                        person_key="sim:u1"))
    assert res["aborted"] and res["sent"] == 0 and res["reason"] == "correction"
    res = await _drive(k, k.social.send("sim:u1", ["这次不检查", "直接发完"], person_key="sim:u1",
                                        abort_on_new=False))
    assert res == {"sent": 2, "aborted": False, "msg_ids": res["msg_ids"], "reason": ""}


async def _drive(k, coro, seconds=120):
    import asyncio
    t = asyncio.ensure_future(coro)
    for _ in range(int(seconds * 10)):
        if t.done():
            break
        await k.clock.advance(0.1)
    return await t


# --------------------------------------------------------------------------- governance hooks
async def test_pre_send_denial_and_modification(kern):
    k = kern
    sim = k.social.adapter("sim")

    async def deny(thread_id, person_key, text, ctx):
        k.gov.calls.append(("pre_send", (thread_id, text), {}))
        return Decision(False, "farewell_manipulation", rule="R7")
    k.gov.pre_send = deny
    await sim.inject("我先走啦，拜拜", sender="u1")
    await k.clock.advance(60)
    assert sim.outputs == [] and sim.typing == []       # denied before any typing indicator
    ab = await events(k, EV.OUTCOME_ABORTED, "sim:u1")
    assert ab and ab[-1].payload["reason"].startswith("gov:")

    async def modify(thread_id, person_key, text, ctx):
        assert ctx["farewell"] is True and ctx["chat_type"] == "private"
        return Decision(True, text=text + "（改）")
    k.gov.pre_send = modify
    await sim.inject("晚安啦", sender="u1")
    await k.clock.advance(60)
    assert sim.texts("u1") == ["好的（改）"]
    act = await events(k, EV.ACT_SEND, "sim:u1")
    sent = await events(k, EV.OUTCOME_SENT, "sim:u1")
    assert act and sent and sent[-1].causal_parent == act[-1].id and act[-1].ts <= sent[-1].ts


async def test_check_action_denial_keeps_silent(kern):
    k = kern
    sim = k.social.adapter("sim")
    k.gov.deny_kinds = {"send_message"}
    await sim.inject("你好", sender="u1")
    await k.clock.advance(60)
    assert sim.outputs == []
    assert (await decisions(k, "sim:u1"))[-1]["stage"] == "gov"


async def test_disclosure_reminder_appended_once(kern):
    k = kern
    sim = k.social.adapter("sim")
    state = {"due": True, "sent": 0}
    k.gov.disclosure_due = lambda tid, now: "（提醒一下：我是 AI 哦）" if state["due"] else None

    def sent(tid, now):
        state["due"] = False
        state["sent"] += 1
    k.gov.disclosure_sent = sent
    await sim.inject("陪我聊聊天", sender="u1")
    await k.clock.advance(60)
    assert sim.texts("u1") == ["好的", "（提醒一下：我是 AI 哦）"] and state["sent"] == 1
    await sim.inject("再聊会儿", sender="u1")
    await k.clock.advance(60)
    assert sim.texts("u1")[-1] == "好的"


# --------------------------------------------------------------------------- in-flight: steer / interrupt / queue
def _slow_mind(k, seconds=5.0):
    calls = []

    async def slow(task):
        calls.append(task)
        await k.clock.sleep(seconds)
        return ComposeResult(segments=[f"回复{len(calls)}"])
    k.mind.compose_reply = slow
    return calls


async def test_steer_merges_followup_into_reply_being_composed(kern):
    k = kern
    sim = k.social.adapter("sim")
    calls = _slow_mind(k)
    await sim.inject("推荐一本书吧", sender="u1")
    await k.clock.advance(3)                  # debounce done, composing (5 s)
    await sim.inject("最好是科普类的", sender="u1")
    await k.clock.advance(60)
    assert len(calls) == 2
    assert [l.text for l in calls[1].new_lines] == ["推荐一本书吧", "最好是科普类的"]
    assert sim.texts("u1") == ["回复2"]       # one reply that covers both messages
    assert k.social.counters.get("inflight.steer") == 1


async def test_interrupt_cancels_composition_on_correction(kern):
    k = kern
    sim = k.social.adapter("sim")
    calls = _slow_mind(k)
    await sim.inject("明天几点见面", sender="u1")
    await k.clock.advance(3)
    await sim.inject("说错了，是后天", sender="u1")
    await k.clock.advance(0.1)
    assert len(calls) == 2                    # first composition cancelled immediately, recomposing
    await k.clock.advance(60)
    assert sim.texts("u1") == ["回复2"]
    assert "后天" in calls[1].new_lines[-1].text
    assert k.social.counters.get("inflight.interrupt") == 1


async def test_other_speakers_are_queued_for_next_batch(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        calls = _slow_mind(k)
        await sim.inject("@小屿 帮我查个东西", sender="a", chat_id="g1", chat_type="group")
        await k.clock.advance(1.5)
        await sim.inject("今天好热", sender="b", chat_id="g1", chat_type="group")
        await k.clock.advance(60)
        assert [l.text for l in calls[0].new_lines] == ["帮我查个东西"] or len(calls[0].new_lines) == 1
        assert all("今天好热" not in l.text for l in calls[0].new_lines)
        dec = await decisions(k, "sim:g1")
        assert len(dec) == 2                  # b's message was decided separately afterwards
    finally:
        await k.stop()


# --------------------------------------------------------------------------- reactions & summaries
async def test_reaction_instead_of_text(kern):
    k = kern
    sim = k.social.adapter("sim")

    async def react(task):
        return ComposeResult(reaction="👍")
    k.mind.compose_reply = react
    m = await sim.inject("我今天考过了！", sender="u1")
    await k.clock.advance(60)
    assert sim.outputs == [] and sim.reactions[-1][1:3] == (m.msg_id, "👍")
    assert (await decisions(k, "sim:u1"))[-1]["outcome"] == "react"
    assert any(c[0] == "check_action" and c[1][0].kind == "react" for c in k.gov.calls)
    assert await events(k, EV.OUTCOME_REACT, "sim:u1")


async def test_group_outcome_summary_and_quote(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        m = await sim.inject("@小屿 量子纠缠是什么", sender="a", sender_name="阿明", chat_id="g1", chat_type="group",
                             title="摸鱼群")
        await k.clock.advance(60)
        assert sim.outputs[0].reply_to == m.msg_id          # mentions are answered with a quote
        # the day log keeps a short topic for shared spaces, never verbatim lines (INV-18)
        assert any(o.startswith("在群「摸鱼群」里和阿明聊了几句") and "量子纠缠" in o for o in k.selfm.outcomes)
    finally:
        await k.stop()


async def test_after_exchange_side_effects(kern):
    k = kern
    sim = k.social.adapter("sim")
    await sim.inject("谢谢你昨天陪我聊天", sender="u1")
    await k.clock.advance(60)
    await k.social.flush()
    assert any(c[0] == "observe_conversation" for c in k.memory.calls)
    assert any(c[0] == "observe_messages" for c in k.growth.calls)
    assert any(c[0] == "social_spend" for c in k.affect.calls)
    assert any(c[0] == "satisfy" and c[1][0] == "social" for c in k.motivation.calls)
    rel = await k.social.relationship("sim:u1")
    assert rel["interactions"] == 1 and rel["warmth"] > 0


async def test_reaction_falls_back_to_text_without_reaction_support(tmp_path):
    k = await make_kernel(tmp_path, channels=[{"kind": "sim", "name": "sim", "reactions": False}])
    try:
        sim = k.social.adapter("sim")

        async def react(task):
            return ComposeResult(reaction="👍")
        k.mind.compose_reply = react
        await sim.inject("我今天考过了！", sender="u1")
        await k.clock.advance(60)
        assert sim.texts("u1") == ["👍"] and sim.reactions == []
    finally:
        await k.stop()


async def test_governance_errors_fail_closed(kern):
    k = kern
    sim = k.social.adapter("sim")

    async def broken(act):
        raise RuntimeError("governance bug")
    k.gov.check_action = broken
    await sim.inject("你好", sender="u1")
    await k.clock.advance(60)
    assert sim.outputs == [] and (await decisions(k, "sim:u1"))[-1]["reason"] == "gov_error"
