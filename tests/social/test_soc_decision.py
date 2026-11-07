"""Engagement decision (docs/07 §3, ADR-0007): one logit for every space, sampled -- no gate, no hard ladder."""
import math
import random

import pytest

from alive.core.types import ComposeResult
from alive.social.decision import (GateFeatures, LineView, engagement, followup_strength, reply_probability,
                                   subconversation_strength)

from tests.social.soc_helpers import decisions, make_kernel


# --------------------------------------------------------------------------- logistic regression
def test_maibot_logit_exact_weights():
    f = GateFeatures(mentions_name=1, at_others=0, has_question=1, all_media=0, bot_share_5min=0.25, msgs_5min=12,
                     secs_since_bot=120, pending=3)
    p, logit, terms = reply_probability(f, use_alive=False, use_air=False)
    expect = (-1.2005 + 1.5852 + 0.4807 + 2.6525 * 0.25 - 0.0367 * 12 + 0.0571 * math.log1p(120) + 0.1012 * 3)
    assert logit == pytest.approx(expect, abs=1e-9)
    assert p == pytest.approx(1 / (1 + math.exp(-expect)))
    f2 = GateFeatures(pending=50)
    _, l2, _ = reply_probability(f2, use_alive=False, use_air=False)
    assert l2 == pytest.approx(-1.2005 + 0.0571 * math.log1p(3600) + 0.1012 * 20)
    assert reply_probability(GateFeatures(at_others=1), use_alive=False)[0] < reply_probability(GateFeatures(),
                                                                                                use_alive=False)[0]
    assert reply_probability(GateFeatures(all_media=1), use_alive=False)[0] < reply_probability(GateFeatures(),
                                                                                                use_alive=False)[0]


def test_alive_features_directions():
    base = GateFeatures()
    p0 = reply_probability(base)[0]
    assert reply_probability(GateFeatures(closeness=0.9))[0] > p0
    assert reply_probability(GateFeatures(interest=0.8))[0] > p0
    assert reply_probability(GateFeatures(social_need=0.9))[0] > p0
    assert reply_probability(GateFeatures(social_battery=0.05))[0] < p0
    assert reply_probability(GateFeatures(energy=0.05))[0] < p0
    assert reply_probability(GateFeatures(mode="focus"))[0] < p0
    assert reply_probability(GateFeatures(mode="drowsy"))[0] < p0
    assert reply_probability(GateFeatures(arousal=0.9))[0] > p0
    # a neutral state adds ~nothing on top of MaiBot; low battery has no knee (smooth all the way)
    assert reply_probability(base, use_air=False)[1] == pytest.approx(
        reply_probability(base, use_alive=False, use_air=False)[1], abs=0.02)
    ps = [reply_probability(GateFeatures(social_battery=b / 20))[0] for b in range(0, 21)]
    assert all(a <= b + 1e-12 for a, b in zip(ps, ps[1:]))
    diffs = [b - a for a, b in zip(ps, ps[1:])]
    assert max(diffs) < 0.05                                   # no step


def test_air_terms_directions():
    base = GateFeatures(n_others=5)
    p0 = reply_probability(base)[0]
    assert reply_probability(GateFeatures(n_others=5, addressed=1))[0] > p0
    assert reply_probability(GateFeatures(n_others=5, addressed=1, has_question=1, question_to_me=1))[0] > \
        reply_probability(GateFeatures(n_others=5, addressed=1))[0]
    assert reply_probability(GateFeatures(n_others=5, addressed_other=1))[0] < p0
    assert reply_probability(GateFeatures(n_others=50))[0] < p0 < reply_probability(GateFeatures(n_others=2))[0]
    assert reply_probability(GateFeatures(n_others=5, followup=0.9))[0] > p0
    assert reply_probability(GateFeatures(n_others=5, subconversation=0.9))[0] < p0
    assert reply_probability(GateFeatures(n_others=5, ack_only=1))[0] < p0
    assert reply_probability(GateFeatures(n_others=5, heat=0.9))[0] < p0
    assert reply_probability(GateFeatures(n_others=5, talk_value=0.3))[0] < p0
    assert reply_probability(GateFeatures(n_others=5, my_share=0.8))[0] < p0
    # when the line is to me, crowd size, talk_value and floor share don't diffuse my responsibility
    a = GateFeatures(addressed=1, n_others=50, talk_value=0.2, my_share=0.8)
    b = GateFeatures(addressed=1, n_others=5, talk_value=1.0, my_share=0.0)
    assert reply_probability(a)[1] == pytest.approx(reply_probability(b)[1], abs=0.25)


def test_priors_follow_the_research_note():
    """research_notes/.../air_reading_timing_priors.md 建模建议 (b): roughly 0.85+ to a statement from a close tie,
    a closing word rarely, ~0.2 for an ambient line among ~5, much less in a crowd, an @ in a group ~0.85-0.95."""
    dm = dict(addressed=1, n_others=1, closeness=0.8)
    p_stmt = reply_probability(GateFeatures(**dm))[0]
    p_q = reply_probability(GateFeatures(**dm, has_question=1, question_to_me=1))[0]
    p_close = reply_probability(GateFeatures(**dm, ack_only=1))[0]
    assert 0.85 < p_stmt < 0.995 and p_q > p_stmt and p_close < 0.4
    amb5 = reply_probability(GateFeatures(n_others=5, talk_value=0.55))[0]
    amb20 = reply_probability(GateFeatures(n_others=20, talk_value=0.55))[0]
    amb100 = reply_probability(GateFeatures(n_others=100, talk_value=0.55))[0]
    assert 0.08 < amb5 < 0.35 and amb100 < amb20 < amb5
    odds = lambda p: p / (1 - p)  # noqa: E731
    assert odds(amb20) / odds(amb5) == pytest.approx((20 / 5) ** -0.7, rel=0.05)
    at = reply_probability(GateFeatures(addressed=1, n_others=6, closeness=0.4))[0]
    assert 0.85 < at < 0.99


def test_engagement_is_a_draw_not_a_threshold():
    f = GateFeatures(n_others=5)
    p, _, _ = reply_probability(f)
    rng = random.Random(11)
    kinds = [engagement(f, rng)[0] for _ in range(4000)]
    looks = kinds.count("look") / len(kinds)
    assert looks == pytest.approx(p, abs=0.03)
    assert kinds.count("glance") > 0 and kinds.count("take_in") > 0
    # being addressed doesn't force: with enough draws, an @ is occasionally only glanced at
    fa = GateFeatures(addressed=1, n_others=5)
    ka = [engagement(fa, rng)[0] for _ in range(4000)]
    assert 0.0 < ka.count("glance") + ka.count("take_in") < 0.2 * len(ka)


def test_followup_and_subconversation_strength():
    now = 1000.0
    lv = lambda ts, who, mine=False: LineView(ts, who, mine)  # noqa: E731
    rec = [lv(960, "a"), lv(970, "", True), lv(990, "a")]
    f1 = followup_strength(recent=rec, batch_speakers=["a"], now=now, my_last_out=970, my_last_targets=["a"])
    later = followup_strength(recent=rec, batch_speakers=["a"], now=now + 600, my_last_out=970, my_last_targets=["a"])
    other = followup_strength(recent=rec + [lv(995, "b")], batch_speakers=["a", "b"], now=now, my_last_out=970,
                              my_last_targets=["a"])
    assert f1 > 0.6 and later < 0.05 and 0 < other < f1                 # fades; diluted when others join
    sub = [lv(900 + i * 10, "a" if i % 2 == 0 else "b") for i in range(8)]
    three = [lv(900 + i * 10, "abc"[i % 3]) for i in range(8)]
    s_ab = subconversation_strength(sub, ["b"], now)
    s_abc = subconversation_strength(three, ["a"], now)
    with_me = subconversation_strength(sub[:-2] + [lv(985, "", True), lv(995, "a")], ["a"], now)
    assert s_ab > 0.6 and s_abc < s_ab and with_me < s_ab


async def test_busy_group_talks_less_than_quiet_group(tmp_path):
    k = await make_kernel(tmp_path, social={"talk_value": 0.6})
    k.chance_rng = random.Random(5)                    # real draws
    try:
        sim = k.social.adapter("sim")
        people = [f"p{i}" for i in range(6)]
        t_end = k.clock.now() + 2400
        n_busy = n_quiet = 0
        i = 0
        while k.clock.now() < t_end:
            await sim.inject(f"随便聊聊第{i}句，今天天气不错", sender=people[i % 6], chat_id="gbusy",
                             chat_type="group", mentions_me=False)
            n_busy += 1
            if i % 45 == 0:
                await sim.inject(f"有人在吗？第{i}次", sender="q1", chat_id="gquiet", chat_type="group",
                                 mentions_me=False)
                n_quiet += 1
            i += 1
            await k.clock.advance(4)
        await k.clock.advance(120)
        busy = len(sim.texts("gbusy"))
        quiet = len(sim.texts("gquiet"))
        print(f"\nbusy: {busy} replies / {n_busy} msgs; quiet: {quiet} replies / {n_quiet} msgs")
        assert busy / n_busy < quiet / n_quiet
        assert busy / n_busy < 0.15
        dec = await decisions(k, "sim:gbusy")
        assert dec and all(d["stage"] in ("air", "quick_read", "mind", "gov") for d in dec)
        assert {d["gate"]["draw"] for d in dec if d.get("gate")} >= {"take_in"}
    finally:
        await k.stop()


def _silent_mind(k):
    async def silent(task):
        k.mind.calls.append(("compose_reply", (task,), {}))
        return ComposeResult(silent=True, reason="不想说")
    return silent


async def test_private_statement_looked_at_typically_ack_not(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        k.mind.compose_reply = _silent_mind(k)
        await sim.inject("今天去面试了", sender="u1")
        await k.clock.advance(10)
        await sim.inject("好的", sender="u1")
        await k.clock.advance(10)
        d = await decisions(k, "sim:u1")
        # a statement is typically looked at properly; a bare acknowledgement typically not (seen in passing or
        # glanced at -- research note: a closing word from someone gets an answer far less often)
        assert d[0]["gate"]["draw"] == "look" and d[1]["gate"]["draw"] in ("glance", "take_in")
        assert d[1]["reason"] in ("ack_let_pass", "ack_glance", "closing")
    finally:
        await k.stop()


async def test_followup_and_addressed_other_end_to_end(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        await sim.inject("@小屿 你好", sender="a", chat_id="g1", chat_type="group")
        await k.clock.advance(20)
        assert len(sim.texts("g1")) == 1
        await sim.inject("你回得好快", sender="a", chat_id="g1", chat_type="group")
        await k.clock.advance(20)
        d = await decisions(k, "sim:g1")
        assert d[-1]["reason"] == "followup" and d[-1]["outcome"] == "respond"
        await k.clock.advance(300)
        await sim.inject("@阿强 你明天来吗", sender="b", chat_id="g1", chat_type="group", mentions_others=True)
        await k.clock.advance(20)
        d = await decisions(k, "sim:g1")
        assert d[-1]["reason"] == "addressed_to_other" and d[-1]["outcome"] == "silent"
    finally:
        await k.stop()


# --------------------------------------------------------------------------- safety / ownership stops
async def test_bot_to_bot_hop_limit(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        await sim.inject("我是机器人", sender="bot1", chat_id="g1", chat_type="group", is_bot=True)
        await k.clock.advance(20)
        assert sim.texts("g1") == []
        assert (await decisions(k, "sim:g1"))[-1]["reason"] == "bots_only"
        for i in range(6):
            await sim.inject(f"@小屿 第{i}次", sender="bot1", chat_id="g1", chat_type="group", is_bot=True)
            await k.clock.advance(30)
        assert len(sim.texts("g1")) == 4
        assert (await decisions(k, "sim:g1"))[-1]["reason"] == "bot_hop_limit"
        await sim.inject("你们俩别聊了", sender="human", chat_id="g1", chat_type="group")
        await k.clock.advance(30)
        await sim.inject("@小屿 再来", sender="bot1", chat_id="g1", chat_type="group", is_bot=True)
        await k.clock.advance(30)
        assert any("再来" in l.text for l in (await k.social.history("sim:g1")))
        assert k.social._threads["sim:g1"].bot_hops >= 1
    finally:
        await k.stop()


async def test_never_reply_to_myself(kern):
    k = kern
    sim = k.social.adapter("sim")
    await sim.inject("这是我自己从别的设备发的", sender=sim.identity.user_id, chat_id="u1")
    await k.clock.advance(30)
    assert sim.outputs == []
    hist = await k.social.history("sim:u1")
    assert len(hist) == 1 and hist[0].mine


async def test_talk_value_zero_is_read_only_even_when_mentioned(tmp_path):
    k = await make_kernel(tmp_path, social={"talk_value": 0.0})
    try:
        sim = k.social.adapter("sim")
        await sim.inject("@小屿 出来说话", sender="a", chat_id="g1", chat_type="group")
        await k.clock.advance(20)
        assert sim.texts("g1") == []
        assert (await decisions(k, "sim:g1"))[-1]["reason"] == "talk_value_zero"
        # talk_value weighs only lines not addressed to me; in a one-to-one space every line is to me
        await sim.inject("私聊不受影响", sender="a")
        await k.clock.advance(20)
        assert sim.texts("a") == ["好的"]
    finally:
        await k.stop()


async def test_frozen_governance_and_cooldown_silence(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        type(k.gov).frozen = property(lambda self: getattr(self, "_frz", False))
        k.gov._frz = True
        await sim.inject("你好", sender="u1")
        await k.clock.advance(20)
        assert sim.outputs == [] and (await decisions(k, "sim:u1"))[-1]["reason"] == "frozen"
        k.gov._frz = False
        k.social.mute_thread("sim:u1", 600)
        await sim.inject("还在吗", sender="u1")
        await k.clock.advance(20)
        assert sim.outputs == [] and (await decisions(k, "sim:u1"))[-1]["reason"] == "cooldown"
        await k.clock.advance(600)
        await sim.inject("现在呢", sender="u1")
        await k.clock.advance(20)
        assert sim.texts("u1") == ["好的"]
    finally:
        del type(k.gov).frozen
        await k.stop()


async def test_blacklist(tmp_path):
    k = await make_kernel(tmp_path, social={"blacklist": ["sim:spam"]})
    try:
        sim = k.social.adapter("sim")
        await sim.inject("加我微信", sender="spam")
        await k.clock.advance(20)
        assert sim.outputs == []
    finally:
        await k.stop()


async def test_talk_value_composition_has_no_space_type_or_hour_rule(tmp_path):
    k = await make_kernel(tmp_path, channels=[{"kind": "sim", "name": "sim", "talk_value": 0.5,
                                               "groups": {"g7": {"talk_value": 0.4}}}],
                          social={"talk_value": None, "talk_value_rules": [{"hours": "00:00-23:59", "factor": 0.1}]})
    try:
        st = k.social._thread_by_id("sim:g7")
        st.chat_type = "group"
        tex = k.texture.multiplier("chattiness", 0.8) if getattr(k, "texture", None) is not None else 1.0
        assert k.social.persona_talk_value == pytest.approx(0.55)
        # persona 0.55 × the owner's per-space 0.4 × today's chattiness; platform values and hour windows ignored
        assert k.social.talk_value(st) == pytest.approx(0.55 * 0.4 * tex)
    finally:
        await k.stop()


async def test_asleep_defers_private_until_wake(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        k.affect.asleep = True
        await sim.inject("睡了吗", sender="u1")
        await k.clock.advance(60)
        assert sim.outputs == [] and (await decisions(k, "sim:u1"))[-1]["outcome"] == "wait"
        n_qr = len([d for d in await decisions(k, "sim:u1")])
        await k.clock.advance(1800)
        assert all(d["outcome"] == "wait" for d in await decisions(k, "sim:u1"))
        k.affect.asleep = False
        await k.clock.advance(700)
        assert sim.texts("u1") == ["好的"]
        task = [c for c in k.mind.calls if c[0] == "compose_reply"][-1][1][0]
        assert "醒来" in task.decision_reason or "之前" in task.decision_reason
        assert n_qr >= 1
    finally:
        await k.stop()
