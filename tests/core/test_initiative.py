"""Initiative propensity (alive/core/initiative.py, docs/05 §5.6): no quotas, behaviour from state + chance."""
import math
import random

import pytest

from alive.core.initiative import get_initiative, night_curve
from alive.testing import make_test_kernel


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    yield kern
    await kern.stop()


async def test_factors_are_smooth_functions_of_state(k):
    ini = get_initiative(k)
    now = k.clock.now()
    f0 = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"closeness": 0.7}, st={})
    f1 = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"closeness": 0.7}, st={"last": now - 600})
    f2 = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"closeness": 0.7}, st={"last": now - 6 * 3600})
    assert f1["refractory"] < f2["refractory"] < f0["refractory"] == 1.0     # just spoke -> low, recovers smoothly
    fa = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"my_unanswered": 0}, st={})
    fb = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"my_unanswered": 2}, st={})
    assert fb["unanswered"] < fa["unanswered"]                             # double-texting is inhibited, not banned
    good = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"warm": 5, "ignored": 0}, st={})
    bad = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"warm": 0, "ignored": 5}, st={})
    assert bad["reception"] < 0.5 < good["reception"]                      # ignored initiatives lower it smoothly
    assert night_curve(3.5) < 0.2 < night_curve(10) < night_curve(16)
    far = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"closeness": 0.1}, st={})
    near = ini.factors("p", shared=False, reason=0.6, now=now, ctx={"closeness": 0.95}, st={})
    assert near["bond"] > far["bond"]


async def test_probability_covers_elapsed_time_not_call_frequency(k):
    """Asking every minute or every hour gives the same rate: p = 1 - exp(-lambda * dt)."""
    ini = get_initiative(k)
    rng = random.Random(1)
    ini._rng = lambda: rng

    async def rate(step_s, hours=200):
        ini._state = {}
        n, t = 0, k.clock.now()
        end = t + hours * 3600
        while t < end:
            ok, p, f = await ini.decide("x", shared=False, reason=0.5, ctx={"closeness": 0.5, "active_now_prob": 0.6},
                                        now=t)
            n += ok
            t += step_s
        return n / hours
    fast, slow = await rate(120), await rate(1800)
    assert fast > 0 and slow > 0 and abs(fast - slow) / max(fast, slow) < 0.35


async def test_frequency_emerges_from_mood_and_varies_no_fixed_count(k):
    """Over simulated days, how often I reach out depends on state and varies day to day; nothing caps it.
    Mood (research note §6, 建模建议 e): a good mood -> more initiating overall; a LOW mood turns me toward close
    ties and away from weak ones."""
    ini = get_initiative(k)
    rng = random.Random(7)
    ini._rng = lambda: rng

    async def _nosave():                                   # speed: persistence isn't under test here
        return None
    ini._save = _nosave

    class Snap:
        def __init__(self, P):
            self.P, self.A, self.energy, self.social_battery = P, 0.5, 0.8, 0.8
    now = k.clock.now()

    def mood_factor(P, closeness):
        k.affect.snapshot = lambda: Snap(P)
        return ini.factors("p", shared=False, reason=0.6, now=now, ctx={"closeness": closeness}, st={})["mood"]
    assert mood_factor(0.6, 0.5) > mood_factor(0.0, 0.5) > mood_factor(-0.6, 0.2)
    assert mood_factor(-0.6, 0.95) > mood_factor(0.0, 0.95)        # low: toward the closest people
    assert mood_factor(-0.6, 0.1) < mood_factor(0.0, 0.1)          # ...and away from weak ties
    counts = {}
    for P in (-0.6, 0.6):
        k.affect.snapshot = lambda P=P: Snap(P)
        days = []
        for d in range(150):
            ini._state = {}
            n, t = 0, k.clock.now() + d * 86400
            for step in range(96):                         # every 15 min across a day
                ok, _p, _f = await ini.decide("ming", shared=False, reason=0.8,
                                              ctx={"closeness": 0.4, "active_now_prob": 0.6}, now=t)
                if ok:
                    n += 1                         # (no refractory here: compare the urge itself, not its pacing)
                t += 900
            days.append(n)
        counts[P] = days
    low, high = counts[-0.6], counts[0.6]
    assert sum(high) > 1.2 * sum(low)                      # a good mood -> reaching out more often (weak-ish tie)
    assert len(set(high)) > 1                              # days differ; there is no fixed number
    assert max(high) >= 1 and min(low) >= 0


async def test_floor_yield_is_sampled_and_smooth(k):
    ini = get_initiative(k)
    rng = random.Random(3)
    ini._rng = lambda: rng

    def p(ratio, lively=12):
        return sum(ini.yield_floor(my_share=ratio * 0.25, fair_share=0.25, msgs_30min=lively, mentioned=False)
                   for _ in range(2000)) / 2000
    assert p(0.8) < p(1.6) < p(3.0)
    assert p(3.0, lively=1) < p(3.0, lively=20)
    assert not ini.yield_floor(my_share=0.9, fair_share=0.25, msgs_30min=20, mentioned=True)
