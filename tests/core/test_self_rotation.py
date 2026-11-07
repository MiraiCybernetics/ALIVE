"""Self model day-to-day variation (ADR-0007): voice samples rotate once a day; the narrative-edit gate is a
readiness hazard instead of a fixed 7-day rule."""
import random

import pytest

from alive.testing import make_test_kernel


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["selfm"])
    yield kern
    await kern.stop()


async def test_voice_samples_rotate_daily_and_are_stable_within_a_day(k):
    s = k.selfm
    k.chance_rng = random.Random(9)
    for i in range(12):
        await s.add_voice_sample(f"学到的说法{i}号哈哈", 0.6)
    await s._load_voice()
    base = set(s.identity.voice_samples)
    days, learned_seen = [], set()
    for d in range(10):
        a = s.voice_samples(6)
        assert a == s.voice_samples(6) and len(a) == 6 and len(set(a)) == 6     # one rotation per day (cacheable)
        days.append(tuple(a))
        learned_seen |= {v for v in a if v not in base}
        await k.clock.advance(86400)
    assert len(set(days)) > 3                                # differs from day to day
    assert learned_seen                                       # learned lines rotate in (the old top-n never did)
    share = sum(1 for d in days for v in d if v in base) / (6 * len(days))
    assert 0.4 < share < 0.95                                 # persona lines still dominate


def test_narrative_readiness_is_a_smooth_hazard(tmp_path):
    from alive.self_model.model import SelfModel
    r = SelfModel.narrative_readiness
    vals = [r(None, d) for d in (0.5, 2, 4, 6, 7, 9, 14)]
    assert vals[0] == 0.0                                     # technical floor: not twice in one night
    assert all(x < y for x, y in zip(vals[1:], vals[2:]))   # grows with the days since the last edit
    assert 0.0 < vals[1] < 0.05 and 0.55 < vals[4] < 0.7 and vals[-1] > 0.99
