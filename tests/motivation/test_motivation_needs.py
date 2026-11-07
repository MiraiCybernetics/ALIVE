"""Needs dynamics, coupling, pressure, urges and concretization (docs/05 §2-§3)."""
import pytest

from tests.motivation import mothelp
from tests.motivation.mothelp import ts
from alive.core.types import EV
from alive.interfaces import AffectSnapshot, Urge
from alive.runtime.clock import ManualClock
from alive.runtime.llm import BudgetDenied

H = 3600.0


@pytest.fixture
async def k(tmp_path):
    k = await mothelp.kernel(tmp_path, real=("motivation",))
    yield k
    await k.stop()


def snap(**kw):
    return AffectSnapshot(**kw)


async def run(k, hours, step=0.5, **affect):
    urges = []
    for _ in range(int(hours / step)):
        await k.clock.advance(step * H)
        urges = k.motivation.tick(step * H, k.clock.now(), snap(**affect))
    return urges


def test_pressure_formula_and_coupling_pure():
    from alive.motivation.needs import NeedModel
    m = NeedModel.from_temperament({})
    m.needs["social"].level = m.needs["social"].setpoint - 0.2
    p = m.pressures()
    assert p["social"] == pytest.approx(1.0 * 0.2 ** 1.5)
    m.needs["rest"].level = 0.2                       # tired: (0.6-0.2)/0.6 -> x(1 + 0.5*0.667)
    assert m.pressures()["social"] == pytest.approx(0.2 ** 1.5 * (1 + 0.5 * (0.4 / 0.6)))
    m.needs["rest"].level = 0.6
    m.needs["curiosity"].level = 0.45
    base = m.pressures()["curiosity"]
    m.needs["safety"].level = 0.4                     # threatened: curiosity suppressed
    assert m.pressures()["curiosity"] == pytest.approx(base * (1 - 0.8 * 0.5))
    m.needs["safety"].level = 0.8
    assert m.pressures(boredom=1.0)["curiosity"] == pytest.approx(base * 1.5)
    assert m.pressures(mood_dev=(0.8, 0.0))["social"] > m.pressures(mood_dev=(-0.8, 0.0))["social"]


async def test_needs_from_temperament_and_decay(k):
    n = k.motivation.needs()
    assert set(n) == {"social", "curiosity", "rest", "competence", "safety", "autonomy", "coherence"}
    assert n["social"]["setpoint"] == 0.65 and n["curiosity"]["weight"] == 1.0
    await run(k, 5)
    n = k.motivation.needs()
    assert n["social"]["level"] == pytest.approx(0.65 - 5 * 0.04, abs=1e-6)
    assert n["curiosity"]["level"] == pytest.approx(0.75 - 5 * 0.05, abs=1e-6)
    assert n["social"]["pressure"] == pytest.approx(0.2 ** 1.5, rel=1e-3)
    assert n["rest"]["level"] == pytest.approx(0.8, abs=0.01)          # follows energy
    k.motivation.deplete("safety", 0.3)
    await run(k, 5)
    assert k.motivation.needs()["safety"]["level"] == pytest.approx(0.5 + 0.1, abs=1e-6)   # +0.02/h regression


async def test_asleep_slows_decay_and_suppresses_urges(k):
    k.motivation.tick(0, k.clock.now(), snap(asleep=True))   # the next interval is integrated as asleep
    urges = await run(k, 8, asleep=True)
    assert urges == []
    assert k.motivation.needs()["social"]["level"] == pytest.approx(0.65 - 8 * 0.04 * 0.25, abs=1e-6)


async def test_urges_threshold_event_and_hysteresis(k):
    urges = await run(k, 3)
    assert urges == []                                  # deficits still small
    urges = await run(k, 2.5)
    needs = [u.need for u in urges]
    assert "social" in needs and "curiosity" in needs and "competence" not in needs
    assert all(u.pressure >= k.motivation.model.needs[u.need].threshold for u in urges)
    assert urges == sorted(urges, key=lambda u: -u.pressure)
    created = {u.need: u.created_at for u in urges}
    urges2 = await run(k, 0.5)
    assert {u.need: u.created_at for u in urges2 if u.need in created} == created   # same urge persists
    await k.clock.settle()
    evs = await k.bus.query(types=[EV.URGE])
    assert sorted(e.payload["need"] for e in evs) == sorted(set(e.payload["need"] for e in evs))   # once per crossing
    k.motivation.satisfy("social", 0.4)
    urges3 = k.motivation.tick(1, k.clock.now() + 1, snap())
    assert "social" not in [u.need for u in urges3]


async def test_satisfy_notifies_affect_and_meaningful_events(k):
    k.motivation.satisfy("social", 0.2, cause="ev-chat")
    k.motivation.satisfy("social", 0.01)
    k.motivation.deplete("competence", 0.1, cause="ev-fail")
    calls = [c for c in k.affect.calls if c[0] == "on_need_event"]
    assert calls[0][1] == ("social", 0.2) and calls[-1][1] == ("competence", -0.1)
    await k.clock.settle()
    evs = await k.bus.query(types=[EV.NEED_EVENT])
    ops = [(e.payload["op"], e.payload["need"]) for e in evs]
    assert ("satisfy", "social") in ops and ("deplete", "competence") in ops
    assert sum(1 for o in ops if o == ("satisfy", "social")) == 1   # the 0.01 change is not an event
    k.motivation.satisfy("nonexistent", 0.5)                         # ignored, no crash


async def test_mood_offsets_and_weekend_weights(tmp_path):
    k = await mothelp.kernel(tmp_path, start="2026-11-14 10:00", real=("motivation",))   # Saturday
    try:
        m = k.motivation
        m.model.needs["curiosity"].level = 0.5
        m.tick(1, k.clock.now() + 1, snap())
        sat = m.needs()["curiosity"]["pressure"]
        assert sat == pytest.approx(1.15 * 0.25 ** 1.5, rel=1e-3)
        m.model.needs["social"].level = 0.5
        m.tick(1, k.clock.now() + 2, snap(P=0.8))
        happy = m.needs()["social"]["pressure"]
        m.tick(1, k.clock.now() + 3, snap(P=-0.8))
        sad = m.needs()["social"]["pressure"]
        assert happy > sad
    finally:
        await k.stop()


async def test_needs_persist_and_catch_up(tmp_path):
    clock = ManualClock(start=ts("2026-11-09 10:00"))
    k = await mothelp.kernel(tmp_path, real=("motivation",), clock=clock)
    k.motivation.satisfy("social", 0.2)
    lvl = k.motivation.needs()["social"]["level"]
    await k.stop()
    k2 = await mothelp.kernel(tmp_path, real=("motivation",), clock=ManualClock(start=ts("2026-11-09 14:00")))
    try:
        assert k2.motivation.needs()["social"]["level"] == pytest.approx(lvl - 4 * 0.04, abs=1e-6)
    finally:
        await k2.stop()


# --------------------------------------------------------------------------- concretize
def _people(now):
    return [{"person_key": "tg:M", "thread_id": "tg:M", "name": "M", "closeness": 0.8, "warmth": 0.5,
             "last_contact": now - 3 * 86400, "last_initiated_by": "them", "reciprocity": 0.2,
             "active_now_prob": 0.7, "recent_initiatives": 0, "ignored_streak": 0, "proactive_ok": True}]


async def test_concretize_uses_memory_people_and_is_rate_limited(k):
    async def cands(now, limit=10):
        return _people(now)
    k.social.outreach_candidates = cands
    await k.memory.note(kind="episodic", content="M 说她下周三面试，有点紧张")
    k.mock.on("mot.concretize", {"suggestion": "想问问 M 面试准备得怎么样", "target_person": "tg:M",
                                 "kind": "outreach", "reason": "她上次说紧张"})
    u = await k.motivation.concretize(Urge(need="social", pressure=0.12, created_at=k.clock.now()))
    assert u.suggestion == "想问问 M 面试准备得怎么样" and u.target_person == "tg:M"
    req = k.mock.calls_for("mot.concretize")[0]
    assert req.tier == "fast" and "tg:M" in req.messages[0]["content"]
    assert any(c[0] == "retrieve" for c in k.memory.calls)
    # rate limit: <= 1 LLM call per need per 30 virtual minutes; cached suggestion reused (also by tick)
    await k.motivation.concretize(Urge(need="social", pressure=0.13))
    assert len(k.mock.calls_for("mot.concretize")) == 1
    k.motivation.model.needs["social"].level = 0.3
    urges = k.motivation.tick(1, k.clock.now() + 1, snap())
    su = [x for x in urges if x.need == "social"][0]
    assert su.suggestion and su.target_person == "tg:M"
    await k.clock.advance(31 * 60)
    await k.motivation.concretize(Urge(need="social", pressure=0.13))
    assert len(k.mock.calls_for("mot.concretize")) == 2


async def test_concretize_validates_target_and_falls_back(k):
    k.mock.on("mot.concretize", {"suggestion": "找 Z 聊聊", "target_person": "tg:Z", "kind": "weird"})
    u = await k.motivation.concretize(Urge(need="social", pressure=0.1))
    assert u.target_person is None                      # not among the candidates -> dropped
    assert k.motivation._concrete["social"]["kind"] == "outreach"
    k.mock.on("mot.concretize.", {})
    k.mock.handlers.pop("mot.concretize")
    u2 = await k.motivation.concretize(Urge(need="curiosity", pressure=0.1))
    assert u2.suggestion == "想看看有什么新鲜的东西"     # empty LLM output -> template

    def deny(req):
        raise BudgetDenied("over budget")
    k.mock.on("mot.concretize", deny)
    u3 = await k.motivation.concretize(Urge(need="coherence", pressure=0.1))
    assert u3.suggestion == "想整理一下这几天的想法"


async def test_integration_with_real_affect(tmp_path):
    k = await mothelp.kernel(tmp_path, real=("affect", "motivation"))
    try:
        m, a = k.motivation, k.affect
        m.satisfy("social", 0.3, cause="ev-warm-chat")
        assert any(e["type"] == "gratitude" for e in a.snapshot().emotions)     # docs/04 §3.4 coupling
        m.deplete("safety", 0.3, cause="ev-attack")
        assert any(e["type"] == "anger" for e in a.snapshot().emotions)
        for _ in range(12):
            await k.clock.advance(H / 2)
            urges = m.tick(H / 2, k.clock.now(), a.snapshot())
        assert m.needs()["rest"]["level"] == pytest.approx(a.snapshot().energy, abs=0.05)
        assert all(isinstance(u, Urge) for u in urges)
        a.set_asleep(True)
        m.tick(1, k.clock.now() + 1, a.snapshot())
        assert m.tick(1, k.clock.now() + 2, a.snapshot()) == []
    finally:
        await k.stop()


async def test_urge_onset_is_logistic_and_needs_vary_by_day(tmp_path):
    """docs/05 §2-3: no fixed 0.08 onset (each urge episode draws its own logistic threshold) and no day like the
    last (setpoint and decay are drawn per day around the temperament, shifted by today's texture)."""
    import random
    from alive.motivation.needs import logistic_threshold
    r = random.Random(1)
    ths = [logistic_threshold(r, 0.08) for _ in range(4000)]

    def cdf(p):
        return sum(t <= p for t in ths) / len(ths)
    assert abs(cdf(0.08) - 0.5) < 0.03 and 0.18 < cdf(0.065) < 0.38 and 0.62 < cdf(0.095) < 0.82
    assert cdf(0.03) < 0.06                                     # smooth, but still about the same need
    k = await mothelp.kernel(tmp_path, real=("motivation",))
    try:
        k.chance_rng = random.Random(2)
        m = k.motivation
        days = []
        for d in range(6):
            m._daily_needs(ts(f"2026-11-{10 + d} 10:00"))
            n = m.model.needs["social"]
            days.append((n.setpoint, n.decay))
        assert len({sp for sp, _ in days}) == 6 and all(abs(sp - 0.65) < 0.15 for sp, _ in days)
        assert len({d for _, d in days}) == 6 and all(d < 0 for _, d in days)
        m._daily_needs(ts("2026-11-15 18:00"))                  # stable within a day
        assert (m.model.needs["social"].setpoint, m.model.needs["social"].decay) == days[-1]
    finally:
        await k.stop()
