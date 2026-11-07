"""Affect dynamics: ALMA default mood, decay half-lives, second-order mood, physiology, sleep window,
numerical stability and catch-up after downtime (docs/04 §2-§3)."""
import math

import pytest

from tests.affect import affhelp
from tests.affect.affhelp import emo, ts
from alive.affect.dynamics import (MoodParams, SleepParams, SleepRhythm, alma_default_mood, circadian_value,
                                   mood_octant, mood_step, regression_half_life, sleep_pressure_step)
from alive.affect.tables import EMOTION_PAD, HALF_LIFE_H, OCC_PAD, is_negative
from alive.runtime.clock import ManualClock

H = 3600.0


@pytest.fixture
async def k(tmp_path):
    k = await affhelp.kernel(tmp_path)
    yield k
    await k.stop()


# --------------------------------------------------------------------------- ALMA mapping
def test_alma_paper_example_raw_convention():
    # Gebhard 2005: O=.4 C=.8 E=.6 A=.3 N=.4 -> P=.38 A=-.08 D=.50, "slightly relaxed"
    P, A, D = alma_default_mood({"O": .4, "C": .8, "E": .6, "A": .3, "N": .4}, scale="raw")
    assert (round(P, 2), round(A, 2), round(D, 2)) == (0.38, -0.08, 0.50)
    assert mood_octant((P, A, D))[:2] == ("relaxed", "slightly")
    # the paper's mood-intensity example
    assert mood_octant((0.25, -0.18, 0.12))[:2] == ("relaxed", "slightly")


def test_alma_centered_convention_for_persona():
    # docs/04 §2.1: inputs 0..1 are mapped to -1..1 first. xiaoyu: O.78 C.55 E.58 A.68 N.38
    P, A, D = alma_default_mood({"O": .78, "C": .55, "E": .58, "A": .68, "N": .38})
    assert P == pytest.approx(0.21 * 0.16 + 0.59 * 0.36 + 0.19 * -0.24, abs=1e-9)
    assert A == pytest.approx(0.15 * 0.56 + 0.30 * 0.36 - 0.57 * -0.24, abs=1e-9)
    assert D == pytest.approx(0.25 * 0.56 + 0.17 * 0.10 + 0.60 * 0.16 - 0.32 * 0.36, abs=1e-9)
    # a neutral personality maps to the origin
    assert alma_default_mood({k: 0.5 for k in "OCEAN"}) == pytest.approx((0.0, 0.0, 0.0))


async def test_engine_default_mood_from_persona_and_raw_config(tmp_path):
    k = await affhelp.kernel(tmp_path)
    try:
        assert k.affect.default == pytest.approx((0.2004, 0.3288, 0.1378), abs=1e-4)
        assert k.affect.mood_octant()[:2] == ("exuberant", "slightly")
        assert k.affect.mood_params.half_life_h == pytest.approx(12 + 36 * 0.38)
    finally:
        await k.stop()
    cfg = {"affect": {"big_five_scale": "raw",
                      "temperament": {"big_five": {"O": .4, "C": .8, "E": .6, "A": .3, "N": .4}}}}
    k2 = await affhelp.kernel(tmp_path / "raw", cfg=cfg)
    try:
        assert k2.affect.mood_octant()[:2] == ("relaxed", "slightly")
        assert k2.affect.labels()["mood"] == "挺放松的"
    finally:
        await k2.stop()


def test_octant_table_and_bands():
    assert mood_octant((0.05, 0.02, 0.0))[0] == "neutral"
    assert mood_octant((-0.5, 0.5, 0.5))[:2] == ("hostile", "moderately")
    assert mood_octant((-0.9, 0.8, -0.8))[:2] == ("anxious", "fully")
    assert mood_octant((-0.3, -0.3, -0.3))[:2] == ("bored", "slightly")


# --------------------------------------------------------------------------- emotions
def test_pad_table_is_alma_and_half_life_ordering():
    assert OCC_PAD["anger"] == (-0.51, 0.59, 0.25) and OCC_PAD["admiration"] == (0.5, 0.3, -0.2)
    assert len(OCC_PAD) == 24
    assert set(HALF_LIFE_H) == set(EMOTION_PAD)
    hl = HALF_LIFE_H
    assert hl["sadness"] == max(hl.values())
    for fast in ("surprise", "irritation", "relief", "fear"):
        assert hl[fast] <= 0.5
    assert hl["guilt"] > hl["shame"] and hl["anxiety"] > hl["fear"]
    pos = [hl[t] for t in EMOTION_PAD if not is_negative(t)]
    neg = [hl[t] for t in ("distress", "sadness", "guilt", "anger", "disappointment", "remorse")]
    assert min(neg) > sum(pos) / len(pos)  # negative emotions decay more slowly


async def test_exponential_decay_matches_half_life(k):
    a = k.affect
    a.apply_deltas([{"type": "joy", "intensity": 0.6, "cause": "收到礼物"}], "ev-joy")
    e0 = emo(a, "joy")
    hl = a.half_lives["joy"]
    await k.clock.advance(hl * H)
    assert emo(a, "joy")["intensity"] == pytest.approx(e0["intensity"] / 2, rel=1e-3)
    a.apply_deltas([{"type": "sadness", "intensity": 0.6, "cause": "朋友搬走了"}], "ev-sad")
    s0 = emo(a, "sadness")["intensity"]
    await k.clock.advance(a.half_lives["sadness"] * H)
    assert emo(a, "sadness")["intensity"] == pytest.approx(s0 / 2, rel=1e-3)
    assert a.half_lives["sadness"] > a.half_lives["joy"]


async def test_emotions_removed_below_threshold(k):
    a = k.affect
    a.apply_deltas([{"type": "surprise", "intensity": 0.5}], "ev")
    assert emo(a, "surprise")
    await k.clock.advance(4 * H)   # many half-lives of a 18-min emotion
    assert emo(a, "surprise") is None


# --------------------------------------------------------------------------- mood dynamics
async def test_mood_inertia_good_event_does_not_flip_bad_day(k):
    a = k.affect
    a.mood, a.vel = (-0.4, -0.2, -0.3), (0.0, 0.0, 0.0)
    a.apply_deltas([{"type": "joy", "intensity": 0.6, "cause": "被夸了"}], "ev")
    await k.clock.advance(600)
    assert a.snapshot().P < -0.38          # no jump after a single message
    await k.clock.advance(2 * H)
    p2 = a.snapshot().P
    assert -0.38 < p2 < -0.1               # noticeably better but still a bad mood


async def test_mood_lags_behind_emotion_hysteresis(k):
    a = k.affect
    a.mood, a.vel = a.default, (0.0, 0.0, 0.0)
    a.apply_deltas([{"type": "distress", "intensity": 0.9, "cause": "吵架了"}], "ev")
    ps = []
    for _ in range(16):
        await k.clock.advance(H / 2)
        ps.append(a.snapshot().P)
    # the emotion is strongest at t=0, but the mood keeps falling for hours afterwards (inertia/lag)
    i_min = ps.index(min(ps))
    assert i_min >= 4
    assert min(ps) < a.default[0] - 0.25
    # and after the emotion has faded the mood is still below baseline (slow regression)
    assert ps[-1] < a.default[0] - 0.1


def test_regression_half_life_and_neuroticism():
    assert regression_half_life(0.0) == 12 and regression_half_life(1.0) == 48
    def deviation_after(hours, N):
        p = MoodParams(half_life_h=regression_half_life(N))
        x, v = (0.6, 0.0, 0.0), (0.0, 0.0, 0.0)
        t = 0.0
        while t < hours - 1e-9:
            x, v, _ = mood_step(x, v, (0.0, 0.0, 0.0), None, 0.0, 1 / 12, p)
            t += 1 / 12
        return x[0]
    hl = regression_half_life(0.38)
    assert 0.2 < deviation_after(hl, 0.38) < 0.4      # ~half of the 0.6 deviation left
    assert abs(deviation_after(6 * hl, 0.38)) < 0.06
    assert deviation_after(24, 0.9) > deviation_after(24, 0.1)   # neurotic -> slower recovery


def test_push_phase_deepens_supported_mood():
    p = MoodParams()
    x, v, phase = mood_step((0.6, 0.4, 0.3), (0, 0, 0), (0.0, 0.0, 0.0), (0.4, 0.2, 0.1), 0.8, 0.05, p)
    assert phase == "push"
    _, _, phase2 = mood_step((0.1, 0.05, 0.0), (0, 0, 0), (0.0, 0.0, 0.0), (0.4, 0.2, 0.1), 0.8, 0.05, p)
    assert phase2 == "pull"


async def test_tick_large_dt_is_stable_and_consistent(tmp_path):
    k1 = await affhelp.kernel(tmp_path / "a")
    k2 = await affhelp.kernel(tmp_path / "b")
    try:
        for kk in (k1, k2):
            kk.affect.apply_deltas([{"type": "anger", "intensity": 0.8, "cause": "被骂了"}], "ev")
        t0 = k1.clock.now()
        k1.affect.tick(10 * H, t0 + 10 * H, {})          # one huge step
        for i in range(1, 601):                           # vs 600 small ticks
            k2.affect.tick(60, t0 + i * 60, {})
        s1, s2 = k1.affect.snapshot(), k2.affect.snapshot()
        for f in ("P", "A", "D", "sleep_pressure", "stress", "boredom", "social_battery"):
            assert getattr(s1, f) == pytest.approx(getattr(s2, f), abs=0.02), f
            assert math.isfinite(getattr(s1, f))
        k1.affect.tick(30 * 86400, t0 + 30 * 86400, {})   # a month in one call
        s = k1.affect.snapshot()
        assert all(-1 <= x <= 1 for x in (s.P, s.A, s.D)) and 0 <= s.energy <= 1
    finally:
        await k1.stop()
        await k2.stop()


async def test_tick_uses_dt_when_now_missing(k):
    a = k.affect
    a.apply_deltas([{"type": "joy", "intensity": 0.5}], "ev")
    i0 = emo(a, "joy")["intensity"]
    a.tick(a.half_lives["joy"] * H, 0, {})
    assert a.t == pytest.approx(k.clock.now() + a.half_lives["joy"] * H)
    assert emo(a, "joy")["intensity"] == pytest.approx(i0 / 2, rel=1e-3)


async def test_anti_lockin_after_48h_negative_mood(k):
    a = k.affect
    a.mood, a.vel = (-0.6, -0.3, -0.4), (0.0, 0.0, 0.0)
    a.default = (-0.5, -0.2, -0.3)   # a gloomy baseline keeps the mood negative for days
    a.neg_since = None
    await k.clock.advance(47 * H)
    assert not a.anti_lockin
    await k.clock.advance(2 * H)
    assert a.anti_lockin and a.counters["lockins"] == 1
    reg = a.regulation_needed()
    assert reg is not None and reg["reason"] == "negative_mood_48h" and "distract" in reg["strategy"]


# --------------------------------------------------------------------------- physiology
def test_two_process_model_pieces():
    p = SleepParams()
    sp = 0.08
    for _ in range(16):
        sp = sleep_pressure_step(sp, False, 1.0, p)
    assert 0.55 < sp < 0.65
    assert sleep_pressure_step(0.6, True, 8.0, p) == pytest.approx(0.6 * math.exp(-0.25 * 8))
    assert sleep_pressure_step(sp, False, 1e6, p) == pytest.approx(1.0)  # stable for any dt
    assert circadian_value(15.5, 15.5) == pytest.approx(1.0)
    assert circadian_value(3.5, 15.5) == pytest.approx(-1.0)


async def test_simulated_day_sleep_pressure_circadian_energy(tmp_path):
    k = await affhelp.kernel(tmp_path, start="2026-11-09 07:45")   # Monday, just after wake-up
    try:
        a = k.affect
        rows = {}
        for h in range(0, 25):
            s = a.snapshot()
            rows[h] = (s.sleep_pressure, s.circadian, s.energy, s.asleep)
            await k.clock.advance(H)
        awake = [rows[h] for h in range(0, 15)]           # 07:45 .. 21:45
        assert all(not r[3] for r in awake)
        assert all(b[0] > a_[0] for a_, b in zip(awake, awake[1:]))   # pressure rises while awake
        assert rows[0][2] >= 0.7                          # fresh in the morning (docs/01: ~0.8)
        assert rows[8][1] > 0.99                          # 15:45 ~ circadian peak 15:30
        assert rows[8][2] > rows[15][2]                   # afternoon energy > late evening
        assert rows[16][3] and rows[20][3]                # 23:45 and 03:45 asleep
        assert rows[20][0] < rows[16][0]                  # pressure falls while asleep
        assert rows[24][3] is False and rows[24][0] < 0.15   # next morning: rested
        assert a.labels()["energy"].startswith("上午")      # 08:45
    finally:
        await k.stop()


def test_habitual_window_is_the_prior_mean_with_weekend_shift():
    # SleepRhythm is the habitual schedule = the prior means of the sampled nights (every draw at its mean)
    r = SleepRhythm(affhelp.TZ, wake="07:40", sleep="23:40", weekend_shift_h=0.75, peak_h=15.5)
    assert r.is_sleep_time(ts("2026-11-09 23:50"))        # Monday night
    assert not r.is_sleep_time(ts("2026-11-13 23:50"))    # Friday night: sleeps 00:25
    assert r.is_sleep_time(ts("2026-11-14 00:30"))
    assert r.is_sleep_time(ts("2026-11-14 08:00"))        # Saturday morning: wakes 08:25
    assert not r.is_sleep_time(ts("2026-11-14 08:30"))
    assert not r.is_sleep_time(ts("2026-11-10 07:50"))    # Tuesday morning
    assert not r.is_sleep_time(ts("2026-11-09 15:00"))
    s, e = r.next_window(ts("2026-11-09 15:00"))
    assert s == ts("2026-11-09 23:40") and e == ts("2026-11-10 07:40")
    late = SleepRhythm(affhelp.TZ, wake="09:00", sleep="01:30")   # sleeps after midnight
    assert late.is_sleep_time(ts("2026-11-10 02:00")) and not late.is_sleep_time(ts("2026-11-10 00:30"))


# --------------------------------------------------------------------------- sampled nights (ADR-0007)
def _seeded(k, seed):
    """Real randomness for this kernel (tests of the randomness itself); forget the median draws made at start."""
    import random
    k.chance_rng = random.Random(seed)
    for key in [x for x in k.texture.daily if "|sleep." in x]:
        k.texture.daily.pop(key)


async def _live_nights(k, days):
    """Run the affect system for ``days`` of ordinary days (something noteworthy now and then, so I am not bored
    and listless all day) and collect every night it actually slept (committed nights)."""
    seen = {}
    for _ in range(int(days * 24)):
        await k.clock.advance(H)
        if not k.affect.snapshot().asleep:
            k.affect.note_salient(0.5)
        seen.update({key: dict(n) for key, n in k.affect.nights.items()})
    return seen


def _hours_after(ts_, d, hhmm):
    from datetime import datetime
    base = datetime.strptime(f"{d} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=affhelp.TZ).timestamp()
    return (ts_ - base) / H


async def test_bedtime_and_wake_vary_between_nights_around_the_habit(tmp_path):
    from datetime import date, timedelta
    k = await affhelp.kernel(tmp_path)
    try:
        _seeded(k, 11)
        nights = await _live_nights(k, 21)
        weekday, weekend, wakes = [], [], []
        for key, n in nights.items():
            d = date.fromisoformat(key)
            if key == "2026-11-08":
                continue
            off = _hours_after(n["start"], key, "23:40")
            (weekend if d.weekday() in (4, 5) else weekday).append(off)
            wakes.append(_hours_after(n["end"], (d + timedelta(days=1)).isoformat(), "07:40")
                         - (0.0 if d.weekday() not in (4, 5) else 0.75))
        assert len(weekday) >= 12 and len(weekend) >= 5
        mean = sum(weekday) / len(weekday)
        sd = (sum((x - mean) ** 2 for x in weekday) / (len(weekday) - 1)) ** 0.5
        assert abs(mean) < 0.5                       # centred on the habitual 23:40
        assert 0.25 < sd < 1.2                       # varies by tens of minutes (prior SD 35 min)
        assert sum(weekend) / len(weekend) > mean    # Fri/Sat nights later (mean shift 0.75 h)
        wm = sum(wakes) / len(wakes)
        assert abs(wm) < 0.75 and max(wakes) - min(wakes) > 0.3   # wake-up varies too, around 07:40
    finally:
        await k.stop()


async def test_arousal_delays_bedtime(tmp_path):
    starts = []
    for i, lift in enumerate((0.0, 0.5)):
        k = await affhelp.kernel(tmp_path / str(i), start="2026-11-10 21:30")     # Tuesday evening
        try:
            a = k.affect
            a.mood, a.vel = (a.default[0], a.default[1] + lift, a.default[2]), (0.0, 0.0, 0.0)   # excited (or not)
            for _ in range(10 * 6):
                await k.clock.advance(600)
                a.snapshot()
            starts.append(a.nights["2026-11-10"]["start"])
        finally:
            await k.stop()
    assert starts[1] > starts[0] + 15 * 60          # excited: to bed noticeably later


async def test_sleep_pressure_decides_bedtime(tmp_path):
    starts = []
    for i, nap in enumerate((False, True)):
        k = await affhelp.kernel(tmp_path / str(i), start="2026-11-10 16:00")
        try:
            a = k.affect
            if nap:
                a.set_asleep(True)                    # a 2-hour afternoon nap discharges sleep pressure
                await k.clock.advance(2 * H)
                a.set_asleep(False)
                a._external_sleep = False             # back to following my rhythm
            for _ in range(12 * 6):
                await k.clock.advance(600)
                a.snapshot()
            starts.append(a.nights["2026-11-10"]["start"])
        finally:
            await k.stop()
    assert starts[1] > starts[0] + 20 * 60          # after the nap I stay up longer


async def test_wake_probability_monotone_in_salience_and_depth(tmp_path):
    k = await affhelp.kernel(tmp_path, start="2026-11-10 22:00")
    try:
        a = k.affect
        assert a.wake_probability(0.2) == 1.0 and a.expected_wake() is None      # awake
        k.clock.set(ts("2026-11-11 00:45"))
        assert a.snapshot().asleep
        wake = a.expected_wake()
        assert wake is not None and wake == a.nights["2026-11-10"]["end"] and wake > k.clock.now()
        sal = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        early = [a.wake_probability(s) for s in sal]
        assert all(x < y for x, y in zip(early, early[1:]))                       # monotone in salience
        assert early[-1] > 0.3 and early[1] < 0.05                               # deep: only urgent+close wakes me
        k.clock.set(wake - 20 * 60)                                               # the last minutes of the night
        assert a.snapshot().asleep
        late = [a.wake_probability(s) for s in sal]
        assert all(l > e for l, e in zip(late, early))                            # lighter sleep toward morning
        assert a.sleep_depth() < 0.2
    finally:
        await k.stop()


def test_wake_probability_pure_function_monotone():
    from alive.affect.dynamics import NightParams, wake_probability
    p = NightParams()
    for d in (0.0, 0.3, 0.7, 1.0):
        ws = [wake_probability(s / 10, d, p) for s in range(11)]
        assert all(x < y for x, y in zip(ws, ws[1:]))
    for s in (0.1, 0.5, 0.9):
        ws = [wake_probability(s, d / 10, p) for d in range(11)]
        assert all(x > y for x, y in zip(ws, ws[1:]))


async def test_set_asleep_takes_external_control(k):
    a = k.affect
    assert not a.snapshot().asleep
    a.set_asleep(True)                      # e.g. the core decided to nap
    sp0 = a.snapshot().sleep_pressure
    await k.clock.advance(H)
    assert a.snapshot().asleep and a.snapshot().sleep_pressure < sp0
    a.tick(60, k.clock.now(), {"asleep": False})
    assert not a.snapshot().asleep
    assert "刚醒" in a.labels()["energy"]


async def test_boredom_grows_linearly_and_salient_events_relieve_it(k):
    a = k.affect
    b0 = a.snapshot().boredom
    await k.clock.advance(2 * H)
    b2 = a.snapshot().boredom
    await k.clock.advance(2 * H)
    b4 = a.snapshot().boredom
    assert b2 - b0 == pytest.approx(b4 - b2, rel=1e-6) and b4 > b0
    a.appraise_quick({"praised": True}, "ev")
    assert a.snapshot().boredom < b4 * 0.7
    a.tick(60, k.clock.now(), {"workspace_load": 0.8})
    b5 = a.snapshot().boredom
    await k.clock.advance(H)
    assert a.snapshot().boredom < b5      # busy -> not bored


async def test_social_battery_spend_recover_and_extraversion(tmp_path):
    k = await affhelp.kernel(tmp_path / "x")
    k2 = await affhelp.kernel(tmp_path / "i", cfg={"affect": {"temperament": {"big_five": {
        "O": .78, "C": .55, "E": 0.1, "A": .68, "N": .38}}}})
    try:
        for kk in (k, k2):
            kk.affect.battery = 0.9
            kk.affect.social_spend(0.2)
        assert k2.affect.snapshot().social_battery < k.affect.snapshot().social_battery   # introvert drains faster
        low = k.affect.snapshot().social_battery
        await k.clock.advance(2 * H)
        assert k.affect.snapshot().social_battery > low
    finally:
        await k.stop()
        await k2.stop()


async def test_stress_follows_load_and_sleep_washes_it_out(k):
    a = k.affect
    s0 = a.snapshot().stress
    for _ in range(12):
        a.tick(600, k.clock.now() + 600, {"workspace_load": 1.0, "open_loops": 6})
        await k.clock.advance(600)
    hi = a.snapshot().stress
    assert hi > s0 + 0.3
    assert a.snapshot().energy < 0.95
    a.set_asleep(True)
    await k.clock.advance(6 * H)
    assert a.snapshot().stress < hi / 3


# --------------------------------------------------------------------------- persistence / catch-up
async def test_catch_up_after_downtime_follows_rhythm(tmp_path):
    clock = ManualClock(start=ts("2026-11-09 21:00"))
    k = await affhelp.kernel(tmp_path, clock=clock)
    k.affect.apply_deltas([{"type": "sadness", "intensity": 0.8, "cause": "告别"}], "ev")
    i0 = emo(k.affect, "sadness")["intensity"]
    hl = k.affect.half_lives["sadness"]
    sp_evening = k.affect.snapshot().sleep_pressure
    await k.stop()
    # process down overnight; restart 12 h later
    k2 = await affhelp.kernel(tmp_path, clock=ManualClock(start=ts("2026-11-10 09:00")))
    try:
        a = k2.affect
        s = a.snapshot()
        assert not s.asleep
        assert s.sleep_pressure < sp_evening and s.sleep_pressure < 0.25   # slept during the downtime
        assert emo(a, "sadness")["intensity"] == pytest.approx(i0 * 2 ** (-12 / hl), rel=0.02)
        # the simulated night was a sampled one: woke at that night's wake time, near the habitual 07:40 (the
        # draws are medians here; the sad evening moved bed and wake a little)
        night = a.nights["2026-11-09"]
        assert a.woke_at == pytest.approx(night["end"], abs=400)
        assert abs(a.woke_at - ts("2026-11-10 07:40")) < 3600
    finally:
        await k2.stop()


async def test_catch_up_after_very_long_downtime_resets(tmp_path):
    k = await affhelp.kernel(tmp_path)
    k.affect.mood = (-0.7, 0.5, -0.5)
    k.affect.apply_deltas([{"type": "anger", "intensity": 0.9}], "ev")
    await k.stop()
    k2 = await affhelp.kernel(tmp_path, clock=ManualClock(start=ts("2026-11-25 10:00")))
    try:
        s = k2.affect.snapshot()
        assert not s.emotions
        assert (s.P, s.A, s.D) == pytest.approx(k2.affect.default, abs=0.05)
    finally:
        await k2.stop()
