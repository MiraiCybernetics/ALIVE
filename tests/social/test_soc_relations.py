"""Relationship model: dynamics, DAM aspect attitudes, opinions, pairing, context block, outreach candidates,
initiative tracking."""
import math

import pytest

from alive.core.types import EV, Event
from alive.interfaces import Intention
from alive.social.relations import PAIR_ALPHABET, entropy3

from tests.social.soc_helpers import decisions, events, make_kernel

DAY = 86400.0


async def _person(k, key="sim:u1", name="小明", text="你好"):
    sim = k.social.adapter("sim")
    await sim.inject(text, sender=key.split(":", 1)[1], sender_name=name)
    await k.clock.advance(30)


# --------------------------------------------------------------------------- dynamics
async def test_closeness_and_warmth_decay_half_lives(kern):
    k = kern
    rs = k.social.relations
    p, r = await rs.get("sim:x")
    now = k.clock.now()
    r.closeness, r.closeness_ts, r.warmth, r.warmth_ts = 0.65, now, 0.8, now
    f = rs.p.closeness_floor
    # the decay is exponential with the relationship's own half-life (exact at that half-life)
    hl = rs.closeness_half_life_days(r)
    assert rs.closeness_at(r, now + hl * DAY) == pytest.approx(f + (0.65 - f) / 2)
    whl = rs.warmth_half_life_days(r)
    assert rs.warmth_at(r, now + whl * DAY) == pytest.approx(0.4)
    assert 1.5 < whl < 6.0                                  # warmth: days
    assert rs.warmth_at(r, now + 30 * DAY) < 0.01           # fast variable fades within weeks
    # a new acquaintance fades in about a month; an old, rich relationship barely moves in a month
    assert 15 < hl < 70
    p.first_seen, r.interactions = now - 400 * DAY, 300
    old_hl = rs.closeness_half_life_days(r)
    assert old_hl > 3 * hl and 120 < old_hl < 900
    assert rs.closeness_at(r, now + 30 * DAY) > 0.55        # slow variable barely moves


async def test_closeness_half_life_grows_with_maturity_and_varies_per_person(kern):
    rs = kern.social.relations
    now = kern.clock.now()
    hls = []
    for i in range(40):
        p, r = await rs.get(f"sim:p{i}")
        p.first_seen, r.interactions, r.closeness_ts = now - 60 * DAY, 50, now
        hls.append(rs.closeness_half_life_days(r))
    assert len({round(h, 3) for h in hls}) > 30            # per person, not one value for everyone
    hls.sort()
    assert 40 < hls[20] < 90                                # a 2-month, 50-interaction relationship: ~60 d
    p, r = await rs.get("sim:p0")
    vals = []
    for age, n in ((0, 0), (30, 10), (120, 60), (365, 200), (1500, 1000)):
        p.first_seen, r.interactions = now - age * DAY, n
        vals.append(rs.closeness_half_life_days(r))
    assert all(a < b for a, b in zip(vals, vals[1:]))        # monotone in age and interaction count
    f = rs.person_factor("sim:p0")
    assert 0.9 * 30 * f < vals[0] < 1.1 * 30 * f and vals[-1] < 365 * f * 1.01


async def test_record_interaction_moves_closeness_slowly_and_warmth_fast(kern):
    k = kern
    await _person(k)
    r0 = await k.social.relationship("sim:u1")
    for _ in range(10):
        await k.social.record_interaction("sim:u1", quality=0.8, initiated_by="them")
    r1 = await k.social.relationship("sim:u1")
    assert r1["closeness"] > r0["closeness"]
    assert r1["closeness"] - r0["closeness"] < 0.25        # slow
    assert r1["warmth"] > 0.6                               # fast
    assert r1["reciprocity"] > 0.5                          # they initiate
    await k.social.record_interaction("sim:u1", quality=-1.0, initiated_by="them")
    r2 = await k.social.relationship("sim:u1")
    assert r2["warmth"] < r1["warmth"] - 0.3 and r2["closeness"] < r1["closeness"]


async def test_rhythm_histogram_and_reply_latency(kern):
    k = kern
    sim = k.social.adapter("sim")
    await sim.inject("早", sender="u1")
    await k.clock.advance(30)               # I reply
    await k.clock.advance(600)
    await sim.inject("刚看到", sender="u1")  # they reply ~10 min after my message
    await k.clock.advance(30)
    rel = await k.social.relationship("sim:u1")
    h = int(k.clock.hour_of_day())
    assert rel["hours"][h] >= 2
    assert 500 < rel["reply_latency"] < 700
    assert rel["last_initiated_by"] == "them"


# --------------------------------------------------------------------------- DAM aspects
async def test_dam_update_formula_entropy_stable_and_discard(kern):
    k = kern
    s = k.social
    r = await s.observe_aspect("sim:u1", "工作", (0.9, 0.05, 0.05), strength=1.0)
    assert r["accepted"] and r["pos"] == pytest.approx(0.9)          # W=0 -> C=P
    r = await s.observe_aspect("sim:u1", "工作", (0.6, 0.2, 0.2), strength=2.0)
    # C_new = (C·W + S·P)/(W+S) = (0.9·1 + 2·0.6)/3 = 0.7
    assert r["pos"] == pytest.approx(0.7) and r["weight"] == pytest.approx(3.0)
    assert r["entropy"] == pytest.approx(entropy3((0.7, 0.15, 0.15)), abs=1e-4)
    # an uninformative observation (H > 1.4 bits) is discarded
    r2 = await s.observe_aspect("sim:u1", "工作", (0.34, 0.33, 0.33))
    assert not r2["accepted"] and r2["obs_entropy"] > 1.4 and r2["pos"] == pytest.approx(0.7)
    # convergence: after ~12 consistent observations the attitude is stable (H < 0.8)
    for _ in range(12):
        r = await s.observe_aspect("sim:u1", "感情", (0.05, 0.9, 0.05), summary="聊感情时有点回避")
    assert r["stable"] and r["entropy"] < 0.8 and r["neg"] > 0.85
    # a consistently lukewarm attitude (H(P) ≈ 0.92 bits) never becomes "stable"
    for _ in range(12):
        r = await s.observe_aspect("sim:u1", "爱好", (0.1, 0.8, 0.1))
    assert r["accepted"] and not r["stable"]
    asp = {a["aspect"]: a for a in await s.relations.aspects("sim:u1")}
    assert asp["感情"]["stable"] and asp["工作"]["n"] == 2
    assert entropy3((1 / 3,) * 3) == pytest.approx(math.log2(3))


async def test_opinion_confidence_rules(kern):
    k = kern
    s = k.social
    oid = await s.add_opinion("sim:u1", "他做事很靠谱", confidence=0.5)
    assert (await s.update_opinion(oid, "support"))["confidence"] == pytest.approx(0.6)
    assert (await s.update_opinion(oid, "weaken"))["confidence"] == pytest.approx(0.5)
    assert (await s.update_opinion(oid, "contradict"))["confidence"] == pytest.approx(0.3)
    await s.update_opinion(oid, "contradict")
    res = await s.update_opinion(oid, "contradict")
    assert res["status"] == "retracted"
    assert await s.relations.opinions("sim:u1") == []
    with pytest.raises(ValueError):
        await s.update_opinion(oid, "maybe")


# --------------------------------------------------------------------------- pairing
async def test_pairing_codes_flow(tmp_path):
    k = await make_kernel(tmp_path, channels=[{"kind": "sim", "name": "sim", "dm_policy": "pairing"}])
    try:
        sim = k.social.adapter("sim")
        await sim.inject("你好，可以认识一下吗", sender="s1", sender_name="陌生人1")
        await k.clock.advance(60)
        ps = k.social.list_pairings()
        assert len(ps) == 1
        code = ps[0]["code"]
        assert len(code) == 8 and all(c in PAIR_ALPHABET for c in code)
        assert not set(code) & set("01OI")
        assert len(sim.texts("s1")) == 1 and code in sim.texts("s1")[0]      # one notice, no real reply
        assert not any(c[0] == "compose_reply" for c in k.mind.calls)
        assert (await decisions(k, "sim:s1"))[-1]["reason"] == "stranger"
        # same sender again: same code, no second notice
        await sim.inject("在吗", sender="s1")
        await k.clock.advance(60)
        assert len(k.social.list_pairings()) == 1 and len(sim.texts("s1")) == 1
        # at most 3 pending per channel
        for i in range(2, 6):
            await sim.inject("hi", sender=f"s{i}")
            await k.clock.advance(60)
        assert len(k.social.list_pairings("sim")) == 3
        # owner approves -> paired -> normal replies, proactive allowed
        d = await k.social.approve_pairing(code.lower())
        assert d and d["person_key"] == "sim:s1" and k.social.is_paired("sim:s1")
        assert await k.social.approve_pairing(code) is None              # single use
        await sim.inject("我是你主人的朋友", sender="s1")
        await k.clock.advance(60)
        assert sim.texts("s1")[-1] == "好的"
        assert k.social.consent_of("sim:s1")["proactive_ok"] is True
        assert any(e.payload["person_key"] == "sim:s1" for e in await events(k, "soc.paired"))
        # codes expire after 1 h
        await k.clock.advance(3700)
        assert k.social.list_pairings() == []
        # the owner is never a stranger
        await sim.inject("我是主人", sender="owner", chat_id="owner")
        await k.clock.advance(60)
    finally:
        await k.stop()


async def test_owner_key_from_config(tmp_path):
    k = await make_kernel(tmp_path, channels=[{"kind": "sim", "name": "sim", "dm_policy": "pairing"}],
                          cfg_overrides={"owner": {"person_keys": ["sim:boss"]}})
    try:
        sim = k.social.adapter("sim")
        await sim.inject("在吗", sender="boss")
        await k.clock.advance(60)
        assert sim.texts("boss") == ["好的"]
        rel = await k.social.relationship("sim:boss")
        assert rel["person"]["meta"].get("owner") and rel["trust"] >= 0.9
        ctx = await k.social.relationship_context("sim:boss")
        assert "主人" in ctx
    finally:
        await k.stop()


# --------------------------------------------------------------------------- context block & candidates
async def test_relationship_context_block(kern):
    k = kern
    await _person(k, "sim:u1", "小明")
    await k.memory.note(kind="semantic", content="小明在准备下周的面试", person_keys=["sim:u1"])
    k.motivation.intentions["i1"] = Intention(id="i1", level="now", goal="问问小明面试准备得怎么样",
                                              target_person="sim:u1", status="active")
    await k.social.update_person("sim:u1", preferred="明哥", mind_model={"sensitivities": ["不喜欢被催"],
                                                                        "recent_mood_guess": "有点紧张"})
    for _ in range(4):
        await k.social.observe_aspect("sim:u1", "工作", (0.9, 0.05, 0.05), summary="工作上很靠谱")
    oid = await k.social.add_opinion("sim:u1", "他嘴硬心软", confidence=0.7)
    assert oid
    ctx = await k.social.relationship_context("sim:u1")
    for part in ("明哥", "面试", "不喜欢被催", "有点紧张", "工作上很靠谱", "嘴硬心软", "上次联系"):
        assert part in ctx, part
    assert len(ctx) <= 700
    assert "第一次" in await k.social.relationship_context("sim:nobody")


async def test_outreach_candidates_shape(kern):
    k = kern
    await _person(k, "sim:u1", "小明")
    await _person(k, "sim:u2", "小红")
    await k.social.record_interaction("sim:u2", quality=0.9, initiated_by="them")
    await k.social.adapter("sim").inject("群里说话", sender="u3", chat_id="g1", chat_type="group")
    await k.clock.advance(30)
    cands = await k.social.outreach_candidates(k.clock.now())
    keys = [c["person_key"] for c in cands]
    assert keys[:2] == ["sim:u2", "sim:u1"]                # closer first
    assert "sim:u3" not in keys                            # no private thread -> not a candidate
    c = cands[0]
    for f in ("person_key", "thread_id", "name", "closeness", "warmth", "last_contact", "last_initiated_by",
              "reciprocity", "active_now_prob", "recent_initiatives", "proactive_ok", "contact_gap_s"):
        assert f in c
    assert c["thread_id"] == "sim:u2" and c["name"] == "小红" and 0 <= c["active_now_prob"] <= 1


# --------------------------------------------------------------------------- initiatives
async def test_initiative_reply_is_recorded(kern):
    k = kern
    sim = k.social.adapter("sim")
    await _person(k, "sim:u1", "小明")
    res = await _drive(k, k.social.send("sim:u1", ["在忙吗？想起你上次说的那本书"], kind="initiative",
                                        person_key="sim:u1"))
    assert res["sent"] == 1
    await k.bus.publish(Event(type=EV.OUTREACH_PLAN, payload={"plan": {"id": "op1", "person_key": "sim:u1",
                                                                       "thread_id": "sim:u1", "reason_type": "share"},
                                                              "status": "sent"}, thread_id="sim:u1"))
    await k.bus.drain()
    rel = await k.social.relationship("sim:u1")
    assert rel["last_initiated_by"] == "me" and len(rel["initiatives"]) == 1
    assert "主动找过" in await k.social.relationship_context("sim:u1")
    await k.clock.advance(1200)
    await sim.inject("哈哈对呀！那本书超好看，你也看了吗", sender="u1")
    await k.clock.advance(60)
    await k.social.flush()
    res = [c for c in k.motivation.calls if c[0] == "record_outreach_result"]
    assert res and res[-1][1][0] == "op1" and res[-1][1][1] in ("replied_warm", "replied_flat")
    # one outcome, recorded once (S5): a plan-backed initiative is reported to motivation, which records the
    # growth evidence with the plan's sources - social does not record it a second time under another key
    assert not [e for e in k.growth.evidence if e["about"].startswith("outreach")]
    assert any(o == "主动找小明说了话" for o in k.selfm.outcomes)      # no private content in the day log


async def test_initiative_without_a_plan_is_recorded_once_by_social(kern):
    k = kern
    sim = k.social.adapter("sim")
    await _person(k, "sim:u1", "小明")
    await _drive(k, k.social.send("sim:u1", ["今天看到一只超胖的猫"], kind="initiative", person_key="sim:u1"))
    await k.clock.advance(600)
    await sim.inject("哈哈哈哈给我看看！", sender="u1")
    await k.clock.advance(60)
    await k.social.flush()
    ev = [e for e in k.growth.evidence if e["about"].startswith("outreach")]
    assert len(ev) == 1 and ev[0]["about"] == "outreach.general" and ev[0]["subject"] == "sim:u1"


async def test_ignore_evidence_follows_their_own_latency_not_a_day(kern):
    """No "ignored after 24 h" for everyone: the evidence that an initiative is being passed over grows with the
    survival of THAT person's reply-latency distribution (docs/05 §5.4, timing note §2)."""
    k = kern
    await _person(k, "sim:u1", "小明")
    await _person(k, "sim:u2", "小红")
    rs = k.social.relations
    for pk, c in (("sim:u1", 0.9), ("sim:u2", 0.15)):
        p, r = await rs.get(pk)
        r.closeness, r.closeness_ts = c, k.clock.now()
        rs.save(None, r)
    b = k.social._baseline("sim:u1")
    for x in (30, 45, 60, 40, 50, 35, 55):                  # a quick replier
        b.observe_latency(x)
    t0 = k.clock.now()

    def e(pk, hours):
        return k.social.ignore_evidence(pk, t0, t0 + hours * 3600)
    assert e("sim:u1", 0.01) < 0.1                          # at first only the prior that a message slides
    assert e("sim:u1", 1) < e("sim:u1", 6) < e("sim:u1", 24) < 1   # grows smoothly
    assert e("sim:u1", 6) > e("sim:u2", 6)                   # a quick, close person's silence says more
    assert 0.3 < e("sim:u2", 25) < 0.95                       # a weak tie a day later: not "ignored" yet
    k.social.note_outreach("sim:u1", "op9", "care")
    await _drive(k, k.social.send("sim:u1", ["最近还好吗"], kind="initiative", person_key="sim:u1"))
    await k.clock.advance(DAY + 3600)
    await k.social.flush()
    assert not any(c[0] == "record_outreach_result" for c in k.motivation.calls)   # no fixed one-day verdict
    await k.clock.advance(3 * DAY)
    await k.social.flush()
    assert any(c[0] == "record_outreach_result" and c[1][:2] == ("op9", "ignored") for c in k.motivation.calls)
    assert "ignored_streak" not in await k.social.relationship("sim:u1")


async def test_platform_quota_blocks_initiatives(tmp_path):
    k = await make_kernel(tmp_path, channels=[{"kind": "sim", "name": "sim",
                                               "quotas": {"proactive_private_per_day": 1}}])
    try:
        await _person(k, "sim:u1", "小明")
        r1 = await _drive(k, k.social.send("sim:u1", ["早上好"], kind="initiative", person_key="sim:u1"))
        r2 = await _drive(k, k.social.send("sim:u1", ["在吗"], kind="initiative", person_key="sim:u1"))
        assert r1["sent"] == 1 and r2["sent"] == 0 and r2["reason"] == "quota_private_day"
        assert k.social.channel_quotas("sim")["proactive_private_per_day"] == 1
    finally:
        await k.stop()


async def _drive(k, coro, seconds=120):
    import asyncio
    t = asyncio.ensure_future(coro)
    for _ in range(int(seconds * 10)):
        if t.done():
            break
        await k.clock.advance(0.1)
    return await t


async def test_relationships_persist_across_restart(tmp_path):
    k = await make_kernel(tmp_path)
    await _person(k, "sim:u1", "小明")
    await k.social.record_interaction("sim:u1", quality=0.9, initiated_by="them")
    await k.social.update_person("sim:u1", preferred="明哥")
    before = await k.social.relationship("sim:u1")
    await k.stop()
    k2 = await make_kernel(tmp_path)
    try:
        after = await k2.social.relationship("sim:u1")
        assert after["closeness"] == pytest.approx(before["closeness"], abs=1e-3)
        assert after["person"]["names"]["preferred"] == "明哥"
        hist = await k2.social.history("sim:u1")
        assert [l.text for l in hist] == ["你好", "好的"]
        assert [t.thread_id for t in k2.social.threads()] == ["sim:u1"]
    finally:
        await k2.stop()
