"""Initiative engine: reasons (INV-7), who comes to mind (Plackett-Luce, no cap), what I'd say (a softmax over my
reasons), the state-dependent initiative draw with no quotas or thresholds (docs/05 §5.6), reception from earlier
results, evidence recorded once (docs/05 §5, 07 §5). Probabilistic behaviour is tested by frequencies."""
import random
import types

import pytest

from tests.motivation import mothelp
from tests.motivation.mothelp import ts
from alive.core.types import EV
from alive.interfaces import Urge
from alive.motivation.engine import REASON_TYPES, confide_weight

H = 3600.0


def people(now):
    base = {"warmth": 0.5, "last_initiated_by": "them", "reciprocity": 0.2, "recent_initiatives": 0,
            "proactive_ok": True}
    return [
        {**base, "person_key": "tg:M", "thread_id": "tg:M", "name": "M", "closeness": 0.85,
         "last_contact": now - 2 * 86400, "active_now_prob": 0.8},
        {**base, "person_key": "tg:S", "thread_id": "tg:S", "name": "S", "closeness": 0.1,
         "last_contact": now - 5 * 86400, "active_now_prob": 0.5},
        {**base, "person_key": "tg:X", "thread_id": "tg:X", "name": "X", "closeness": 0.9, "proactive_ok": False,
         "last_contact": now - 5 * 86400, "active_now_prob": 0.9},
    ]


@pytest.fixture
async def k(tmp_path):
    k = await mothelp.kernel(tmp_path, real=("motivation",), start="2026-11-09 15:00",
                             cfg={"motivation": {"seed": 3}})

    async def cands(now, limit=10):
        return people(now)
    k.social.outreach_candidates = cands
    k.motivation.rng.random = lambda: 0.0          # deterministic: every eligible plan is drawn in
    yield k
    await k.stop()


def fresh(k):
    """A fresh decision state: no plans in flight, no greeting refractory, a new initiative draw per person."""
    from alive.core.initiative import get_initiative
    m = k.motivation
    m._plans.clear()
    m._recent_plan_at.clear()
    m._greeted.clear()
    get_initiative(k)._state = {}


async def freq(k, trials=150, urge_p=0.3, seed=11, before=None):
    """Run propose_outreach repeatedly from a fresh decision state; count plans by (person, reason)."""
    from collections import Counter
    m = k.motivation
    m.rng = random.Random(seed)
    c = Counter()
    t0 = k.clock.now()
    for _ in range(trials):
        fresh(k)
        if before:
            before()
        for pl in await m.propose_outreach(social_urge(k, urge_p), t0):
            c[(pl.person_key, pl.reason_type)] += 1
    return c


def by_person(c, pk):
    return sum(v for (p, _r), v in c.items() if p == pk)


def social_urge(k, p=0.15):
    k.motivation.model.needs["social"].level = 0.3
    return [Urge(need="social", pressure=p, created_at=k.clock.now())]


async def plan_events(k):
    await k.clock.settle()
    return [e.payload for e in await k.bus.query(types=[EV.OUTREACH_PLAN]) if e.payload["op"] == "proposed"]


def always_draw(k):
    """Let every reason through the initiative draw, to look at who / what comes to mind on its own."""
    from alive.core.initiative import get_initiative

    async def decide(entity, **kw):
        return True, 0.5, {"lambda_per_h": 1.0, "reason": kw.get("reason")}
    get_initiative(k).decide = decide


def only(*keys, **over):
    async def cands(now, limit=10):
        return [{**p, **over} for p in people(now) if p["person_key"] in keys]
    return cands


async def test_promise_plan_has_reason_sources_and_is_persisted(k):
    m = k.motivation
    it = await m.add_intention({"goal": "把那篇文章发给 M", "kind": "outreach", "target_person": "tg:M",
                                "is_commitment": True}, source_event_id="ev-promise")
    plans = await m.propose_outreach(social_urge(k), k.clock.now())
    assert all(x.person_key != "tg:X" for x in plans)                       # opted out: never
    p = next(x for x in plans if x.person_key == "tg:M")
    assert p.thread_id == "tg:M" and p.reason_type == "promise" and 0 < p.score <= 1
    assert p.reason_type in REASON_TYPES and "把那篇文章发给 M" in p.content_hint            # INV-7
    assert "ev-promise" in p.source_event_ids and len(p.source_event_ids) >= 2
    ev = next(e for e in await plan_events(k) if e["plan_id"] == p.id)
    assert set(ev["factors"]) == {"pressure", "content_value", "recency", "affect_modulation", "relationship_fit",
                                  "initiative"}
    assert ev["factors"]["initiative"]["lambda_per_h"] > 0 and "threshold" not in ev
    assert f"intention:{it.id}" in ev["refs"] and ev["alternatives"][0][0] == "promise"
    row = await k.db.fetchone("SELECT * FROM mot_outreach WHERE id=?", (p.id,))
    assert row["reason_type"] == "promise" and row["outcome"] is None
    # while that proposal is in flight, no duplicate for the same person (de-duplication, not pacing)
    assert all(x.person_key != "tg:M" for x in await m.propose_outreach(social_urge(k), k.clock.now()))


async def test_no_cap_per_call_and_who_comes_to_mind_first_is_sampled(k):
    m = k.motivation
    await m.add_intention({"goal": "问 S 一个问题", "kind": "outreach", "target_person": "tg:S",
                           "meta": {"reason_type": "continue"}})
    await m.add_intention({"goal": "问 M 一个问题", "kind": "outreach", "target_person": "tg:M",
                           "meta": {"reason_type": "continue"}})
    plans = await m.propose_outreach(social_urge(k, 0.3), k.clock.now())
    assert {p.person_key for p in plans} == {"tg:M", "tg:S"}                # each had its own draw; no max per call
    first = {"tg:M": 0, "tg:S": 0}
    m.rng = random.Random(7)
    always_draw(k)
    for _ in range(200):
        fresh(k)
        ps = await m.propose_outreach(social_urge(k, 0.3), k.clock.now())
        if ps:
            first[ps[0].person_key] += 1
    assert first["tg:M"] > first["tg:S"] > 0                                # a Plackett-Luce order, not a sort


async def test_opt_out_never_and_distant_people_rarely(k):
    m = k.motivation
    await m.add_intention({"goal": "问 S 一个问题", "kind": "outreach", "target_person": "tg:S",
                           "meta": {"reason_type": "continue"}})
    await m.add_intention({"goal": "问 X 一个问题", "kind": "outreach", "target_person": "tg:X"})
    await m.add_intention({"goal": "问 M 一个问题", "kind": "outreach", "target_person": "tg:M",
                           "meta": {"reason_type": "continue"}})
    c = await freq(k, urge_p=0.1)
    assert by_person(c, "tg:X") == 0                          # opted out: never (a safety rule, not pacing)
    assert by_person(c, "tg:S") < by_person(c, "tg:M")        # same kind of reason, distant person: rarer


async def test_sampling_is_a_state_dependent_draw(k):
    m = k.motivation
    await m.add_intention({"goal": "给 M 发昨天读到的东西", "kind": "outreach", "target_person": "tg:M",
                           "meta": {"reason_type": "share"}})
    k.social.outreach_candidates = only("tg:M")
    c = await freq(k, trials=200, urge_p=0.15, seed=4)
    assert 0 < by_person(c, "tg:M") < 200                     # a reason, but not every time
    assert c[("tg:M", "share")] > c[("tg:M", "care")]         # what I'd say is a draw over my reasons, the
    assert c[("tg:M", "care")] > 0                            #   stronger one usually - not an argmax
    m.rng.random = lambda: 0.0
    fresh(k)
    plans = await m.propose_outreach(social_urge(k), k.clock.now())
    meta = m._plans[plans[0].id]["meta"]
    assert 0 < meta["p_send"] < 1 and meta["factors"]["initiative"]["reason"] > 0


def test_confide_weight_is_smooth_without_a_knee():
    ws = [confide_weight(i / 100, 1.0, 0.8) for i in range(101)]
    assert all(b >= a for a, b in zip(ws, ws[1:]))                          # grows with closeness
    assert ws[60] > 0                                                      # no minimum closeness
    ratio = confide_weight(0.76, 1.0, 0.8) / confide_weight(0.74, 1.0, 0.8)
    assert ratio == pytest.approx((0.76 / 0.74) ** 2)                      # no steep step at the old 0.75
    assert confide_weight(0.9, 1.0, 0.0) == 0                              # not when I'm fine
    assert confide_weight(0.9, 1.3, 0.5) == pytest.approx(1.3 * confide_weight(0.9, 1.0, 0.5))   # sensitivity


async def test_confiding_grows_with_closeness(k):
    m = k.motivation
    k.affect.snap.P = -0.6
    m._p_base = 0.2
    k.affect.snap.emotions = [{"type": "sadness", "intensity": 0.7}]

    def cands_at(closeness):
        async def cands(now, limit=10):
            ps = people(now)
            ps[0]["closeness"] = closeness
            ps[0]["last_contact"] = now - 6 * H
            return ps[:1]
        return cands
    always_draw(k)                                            # what I'd want to say to them, whatever the moment
    k.social.outreach_candidates = cands_at(0.6)
    mid = (await freq(k))[("tg:M", "confide")]
    k.social.outreach_candidates = cands_at(0.95)
    close = (await freq(k))[("tg:M", "confide")]
    k.social.outreach_candidates = cands_at(0.3)
    far = (await freq(k))[("tg:M", "confide")]
    assert close > mid > far                                  # weighted by closeness, no hard cut-off
    assert mid > 0


async def test_ignored_initiatives_lower_reception_and_warm_replies_restore_it(k):
    from alive.core.initiative import get_initiative
    m = k.motivation
    await m.add_intention({"goal": "接着聊上次的话题", "kind": "outreach", "target_person": "tg:M"})
    k.social.outreach_candidates = only("tg:M")
    ini = get_initiative(k)

    def reception():
        return ini.factors("tg:M", shared=False, reason=0.3, now=k.clock.now(), ctx=m.reception("tg:M"))["reception"]
    base_f, base = reception(), by_person(await freq(k), "tg:M")
    m.rng.random = lambda: 0.0
    fresh(k)
    plans = await m.propose_outreach(social_urge(k, 0.3), k.clock.now())
    for _ in range(3):
        await m.record_outreach_result(plans[0].id, "ignored")
    assert reception() < base_f and by_person(await freq(k), "tg:M") < base      # smooth, learned - no backoff
    ignored_f = reception()
    for _ in range(4):
        await m.record_outreach_result(plans[0].id, "replied_warm", latency_s=120)
    assert reception() > ignored_f
    st = m._annoy["tg:M"]
    assert "ignored_streak" not in st and "backoff_until" not in st and not hasattr(m, "in_backoff")
    await k.clock.advance(1)
    assert await k.db.kv_get("motivation", "annoyance")


async def test_waiting_evidence_is_provisional_and_an_answer_withdraws_it(k):
    m = k.motivation
    await m.add_intention({"goal": "问问 M 搬家顺不顺利", "kind": "outreach", "target_person": "tg:M",
                           "meta": {"reason_type": "care"}})
    p = next(x for x in await m.propose_outreach(social_urge(k), k.clock.now()) if x.person_key == "tg:M")
    m.note_ignore_evidence("tg:M", "ini1", 0.3)
    assert m.reception("tg:M")["ignored"] == pytest.approx(0.3)
    m.note_ignore_evidence("tg:M", "ini1", 0.7)                   # grows smoothly: replaces, does not add up
    assert m.reception("tg:M")["ignored"] == pytest.approx(0.7)
    await m.record_outreach_result(p.id, "replied_warm", latency_s=3 * H, key="ini1")
    rec = m.reception("tg:M")
    assert rec["ignored"] == pytest.approx(0.0, abs=1e-6) and rec["warm"] == pytest.approx(1.0)


async def test_results_feed_needs_events_and_s3_evidence_once(k):
    m = k.motivation
    await m.add_intention({"goal": "问问 M 面试结果", "kind": "outreach", "target_person": "tg:M",
                           "meta": {"reason_type": "care"}})
    p = next(x for x in await m.propose_outreach(social_urge(k), k.clock.now()) if x.person_key == "tg:M")
    lvl = m.needs()["social"]["level"]
    await m.record_outreach_result(p.id, "replied_warm", latency_s=90)
    assert m.needs()["social"]["level"] == pytest.approx(lvl + 0.15, abs=1e-3)
    ev = k.growth.evidence[-1]
    assert ev["level"] == "S3" and ev["polarity"] == "helpful" and ev["about"] == "outreach.care"
    assert ev["subject"] == "tg:M" and p.id in ev["context_refs"]
    row = await k.db.fetchone("SELECT outcome, latency_s FROM mot_outreach WHERE id=?", (p.id,))
    assert row["outcome"] == "replied_warm" and row["latency_s"] == 90
    await k.clock.settle()
    res = [e.payload for e in await k.bus.query(types=[EV.OUTREACH_PLAN]) if e.payload["op"] == "result"]
    assert res[-1]["outcome"] == "replied_warm"
    # an unanswered share or greeting says less than an unanswered check-in
    n0 = len(k.growth.evidence)
    from alive.core.initiative import get_initiative
    get_initiative(k)._state = {}
    await m.add_intention({"goal": "分享一首歌", "kind": "outreach", "target_person": "tg:M",
                           "meta": {"reason_type": "share"}})
    pc = await m.propose_outreach(social_urge(k), k.clock.now())
    p_share = next(x for x in pc if x.person_key == "tg:M")
    m._plans[p_share.id]["reason_type"] = "share"
    await m.record_outreach_result(p_share.id, "ignored")
    m._plans[p.id]["outcome"] = None
    await m.record_outreach_result(p.id, "ignored")
    share_ev, care_ev = k.growth.evidence[n0:n0 + 2]
    assert len(k.growth.evidence) == n0 + 2                    # exactly one record per result
    assert share_ev["polarity"] == care_ev["polarity"] == "harmful"
    assert share_ev["confidence"] < care_ev["confidence"]
    # crisis mode / freeze pauses learning: no evidence
    k.gov = types.SimpleNamespace(learning_paused=True)
    n = len(k.growth.evidence)
    await m.record_outreach_result(p_share.id, "ignored")
    assert len(k.growth.evidence) == n
    with pytest.raises(ValueError):
        await m.record_outreach_result(p_share.id, "maybe")


async def test_no_outreach_while_asleep(k):
    m = k.motivation
    await m.add_intention({"goal": "答应 M 的事", "kind": "outreach", "target_person": "tg:M", "is_commitment": True})
    k.affect.is_sleep_time = lambda now: True
    assert await m.propose_outreach(social_urge(k), k.clock.now()) == []


async def test_missing_someone_grows_with_time_apart_on_our_own_rhythm(k):
    m = k.motivation

    async def score(days, **over):
        k.social.outreach_candidates = only("tg:M", last_contact=k.clock.now() - days * 86400, **over)
        fresh(k)
        plans = await m.propose_outreach(social_urge(k), k.clock.now())
        assert plans and plans[0].reason_type == "care"
        return plans[0].score
    s = [await score(d) for d in (0.5, 3, 10)]
    assert s[0] < s[1] < s[2]                                 # no "3 days" step: it grows smoothly
    # the same 3 days apart mean less with someone I usually hear from weekly than daily
    assert await score(3, contact_gap_s=7 * 86400) < await score(3, contact_gap_s=86400)


async def test_greetings_follow_my_day_with_a_refractory_not_a_daily_ban(k):
    m = k.motivation
    day = "2026-11-09 "
    g = {h: m._greet_strength("tg:M", ts(day + h), 0.85, 13 * H)[0] for h in ("08:30", "15:00", "22:00")}
    assert g["15:00"] < 0.01 * min(g["08:30"], g["22:00"])    # rituals peak around waking and bedtime
    assert m._greet_strength("tg:M", ts(day + "08:30"), 0.85, 13 * H)[1] == "早安问候"
    m.rhythm = {"wake": "09:40", "sleep": "01:30"}             # a later day moves the peaks with it
    assert m._greet_strength("tg:M", ts(day + "10:30"), 0.85, 13 * H)[0] > \
        m._greet_strength("tg:M", ts(day + "08:30"), 0.85, 13 * H)[0]
    m.rhythm = {}
    now = ts(day + "22:00")
    m._greeted["tg:M"] = now - 60
    just = m._greet_strength("tg:M", now, 0.85, 13 * H)[0]
    assert 0 < just < 0.05 * g["22:00"]                        # right after greeting: rare, not banned
    m._greeted["tg:M"] = ts(day + "08:30")                    # greeted this morning: the evening one is normal
    assert m._greet_strength("tg:M", now, 0.85, 13 * H)[0] > 0.8 * g["22:00"]
    # the peak itself moves a little from day to day
    k.chance_rng = random.Random(9)
    m._greeted.clear()
    peaks = {round(m._greet_strength("tg:M", ts(f"2026-11-{d} 08:30"), 0.85, 13 * H)[0], 3) for d in range(10, 16)}
    assert len(peaks) > 2


async def test_waiting_worry_rises_with_their_silence_not_at_a_fixed_hour(k):
    m = k.motivation

    def run(closeness, seed, hours=48, step=0.5):
        m.rng = random.Random(seed)
        k.affect.calls.clear()
        m._plans.clear()
        t0 = k.clock.now()
        pid = f"op-{closeness}-{seed}"
        m._plans[pid] = {"id": pid, "person_key": "tg:M", "thread_id": "tg:M", "reason_type": "care",
                         "content_hint": "问问", "score": 0.4, "source_event_ids": [], "created_at": t0,
                         "sent_at": None, "outcome": None, "latency_s": None, "resolved_at": None,
                         "meta": {"closeness": closeness, "name": "M"}}
        m.outreach_sent(pid, t0)
        m._wait_t = t0
        first = None
        for i in range(1, int(hours / step) + 1):
            m._check_waiting(t0 + i * step * H)
            if first is None and any(c[0] == "apply_deltas" for c in k.affect.calls):
                first = i * step
        deltas = [c[1][0][0] for c in k.affect.calls if c[0] == "apply_deltas"]
        return deltas, first
    close = [run(0.9, s) for s in range(40)]
    far = [run(0.2, s) for s in range(40)]
    assert sum(len(d) for d, _ in close) > sum(len(d) for d, _ in far)          # closer: more worry
    firsts = sorted(f for _, f in close if f is not None)
    assert len(firsts) > 30 and firsts[0] < firsts[-1]                       # no fixed hour: it varies
    assert all(d["type"] == "anxiety" and d["target"] == "tg:M" and 0 < d["intensity"] <= 0.35
               for ds, _ in close for d in ds)
    assert max(len(d) for d, _ in close) < 10                                # habituates


async def test_waiting_open_loop_fades_rather_than_expiring(k):
    m = k.motivation
    m.rng = random.Random(2)
    shown = []
    for i in range(60):
        pid = f"op{i}"
        m._plans = {pid: {"id": pid, "person_key": "tg:M", "thread_id": "tg:M", "reason_type": "care",
                          "content_hint": "问问", "score": 0.4, "source_event_ids": [], "created_at": k.clock.now(),
                          "sent_at": k.clock.now() - 30 * H, "outcome": None, "latency_s": None,
                          "resolved_at": None, "meta": {"closeness": 0.9}}}
        shown.append(any("在等" in x for x in await m.open_loops()))
    assert 0 < sum(shown) < 60                                # some waits still on my mind after 30 h, some not


async def test_negotiated_interruption_when_availability_is_uncertain(k):
    m = k.motivation

    def avail(a):
        async def cands(now, limit=10):
            ps = people(now)
            ps[0]["active_now_prob"] = a
            return ps[:1]
        return cands
    k.social.outreach_candidates = avail(0.3)
    await m.add_intention({"goal": "问问 M 搬家顺不顺利", "kind": "outreach", "target_person": "tg:M",
                           "is_commitment": True})
    plans = await m.propose_outreach(social_urge(k, 0.3), k.clock.now())
    assert plans and plans[0].reason_type == "promise"
    assert "先轻轻问一句" not in plans[0].content_hint          # promises are delivered as promised
    await m.update_intention((await m.active_intentions())[0].id, status="done")
    await m.add_intention({"goal": "接着聊她推荐的那本书", "kind": "outreach", "target_person": "tg:M"})
    low = by_person(await freq(k, urge_p=0.4), "tg:M")
    k.social.outreach_candidates = avail(0.95)
    high = by_person(await freq(k, urge_p=0.4), "tg:M")
    assert low < high                                         # less likely around -> less likely now ("宁晚勿早")
    k.social.outreach_candidates = avail(0.3)
    m.rng.random = lambda: 0.0
    fresh(k)
    plans = await m.propose_outreach(social_urge(k, 0.4), k.clock.now())
    assert plans and plans[0].content_hint.endswith("（不确定对方方不方便，先轻轻问一句）")
    ev = (await plan_events(k))[-1]
    assert ev["negotiate"] is True


async def test_adopts_plans_sent_by_the_core(k):
    """The core may build its own plan for an intention and announces sends as mot.outreach_plan
    status=sent: motivation starts the waiting loop and still learns from the result."""
    from alive.core.types import OutreachPlan
    m = k.motivation
    plan = OutreachPlan(id="op-core-1", person_key="tg:M", thread_id="tg:M", reason_type="promise",
                        content_hint="把链接发给 M", source_event_ids=["ev-x"], score=1.0, created_at=k.clock.now())
    await k.bus.emit(EV.OUTREACH_PLAN, {"plan": plan.__dict__, "status": "sent", "reason_type": "promise"},
                     source="mind", thread_id="tg:M")
    await k.bus.drain()
    assert m._plans["op-core-1"]["sent_at"] is not None
    assert any("在等" in x for x in await m.open_loops())
    await m.record_outreach_result("op-core-1", "ignored")
    await m.record_outreach_result("op-core-1", "ignored")
    assert m._annoy["tg:M"]["ignored"] > 1                    # feeds reception
