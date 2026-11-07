"""Expression mapping (labels, style, modulators), router hook and persistence (docs/04 §6, docs/10 §4.2)."""
import pytest

from tests.affect import affhelp
from tests.affect.affhelp import ts
from alive.affect.expression import ExprState, emotion_phrase, energy_label, mood_label, style, time_of_day_zh
from alive.core.types import EV
from alive.runtime.clock import ManualClock
from alive.runtime.llm import LLMRequest

H = 3600.0


@pytest.fixture
async def k(tmp_path):
    k = await affhelp.kernel(tmp_path)
    yield k
    await k.stop()


def _st(**kw):
    base = dict(mood=(0.0, 0.0, 0.0), default=(0.0, 0.0, 0.0), emotions=[], energy=0.7, sleep_pressure=0.3,
                circadian=0.5, social_battery=0.8, stress=0.1, boredom=0.0, asleep=False, hour=15.0, now=1e9)
    base.update(kw)
    return ExprState(**base)


# --------------------------------------------------------------------------- labels
@pytest.mark.parametrize("pad,label", [
    ((0.05, 0.0, 0.05), "心情挺平静的"),
    ((0.25, -0.18, 0.12), "挺放松的"),                 # ALMA "slightly relaxed"
    ((0.6, -0.5, 0.6), "很放松，心里挺踏实"),           # moderately relaxed
    ((0.4, 0.4, 0.3), "心情不错，挺有精神的"),
    ((-0.3, 0.3, 0.3), "有点烦躁"),                    # slightly hostile
    ((-0.6, 0.6, -0.6), "挺焦虑的，心里不踏实"),        # moderately anxious
    ((-0.4, -0.3, -0.4), "有点低落，提不起劲"),
    ((-0.9, -0.9, -0.9), "很消沉，什么都不想干"),       # fully
])
def test_mood_label_octant_and_band(pad, label):
    assert mood_label(_st(mood=pad)) == label


def test_emotion_phrases_with_cause_and_bands():
    assert emotion_phrase({"type": "sadness", "intensity": 0.2, "initial": 0.6, "cause_text": "L 没回消息"}) \
        == "还有点因为L 没回消息而难过"
    assert emotion_phrase({"type": "joy", "intensity": 0.5, "initial": 0.5, "cause_text": "收到礼物"}) == "因为收到礼物挺开心的"
    assert emotion_phrase({"type": "anger", "intensity": 0.8, "initial": 0.8}) == "特别生气"
    assert emotion_phrase({"type": "happy_for", "intensity": 0.3, "initial": 0.3, "target_name": "M"}) == "有点替M高兴"


def test_energy_label_time_of_day_and_states():
    assert time_of_day_zh(3) == "凌晨" and time_of_day_zh(15) == "下午" and time_of_day_zh(21) == "晚上"
    assert energy_label(_st(energy=0.4, sleep_pressure=0.5, hour=14.5)) == "下午有点犯困"
    assert energy_label(_st(energy=0.4, sleep_pressure=0.2, circadian=0.8, hour=14.5)) == "下午有点累"
    assert energy_label(_st(energy=0.9, hour=10)) == "上午精神很好"
    assert energy_label(_st(asleep=True)).startswith("在睡觉")
    assert energy_label(_st(energy=0.6, social_battery=0.1, stress=0.8, hour=20)) == "晚上精神还行，不太想社交，压力有点大"
    assert energy_label(_st(now=1000.0, woke_at=500.0, hour=7.8)) == "早上刚醒，还有点迷糊"


async def test_labels_strongest_two_emotions(k):
    a = k.affect
    a.apply_deltas([{"type": "sadness", "intensity": 0.7, "cause": "L 没回消息"},
                    {"type": "interest", "intensity": 0.3, "cause": "一篇论文"},
                    {"type": "joy", "intensity": 0.2, "cause": "天气好"}], "ev")
    lab = a.labels()
    assert set(lab) == {"mood", "emotion", "energy"}
    parts = lab["emotion"].split("；")
    assert len(parts) == 2 and "难过" in parts[0] and "L 没回消息" in parts[0] and "好奇" in parts[1]
    assert lab["energy"].startswith("上午")
    assert "P" not in lab["mood"] and "0." not in lab["mood"]     # translated, not dumped


# --------------------------------------------------------------------------- style
def test_style_neutral_at_default_mood_and_quadrants():
    s0 = style(_st(mood=(0.2, 0.3, 0.1), default=(0.2, 0.3, 0.1)))
    assert s0["length"] == pytest.approx(1.0) and s0["emoji"] == pytest.approx(1.0) and s0["delay"] == pytest.approx(1.0)
    hh = style(_st(mood=(0.6, 0.6, 0.2)))
    hl = style(_st(mood=(0.6, -0.6, 0.2)))
    lh = style(_st(mood=(-0.6, 0.6, 0.3)))
    ll = style(_st(mood=(-0.6, -0.6, -0.3)))
    assert hh["length"] > 1.1 and hh["emoji"] > 1.3 and hh["delay"] < 0.9 and hh["tone"] == "活泼"
    assert hl["tone"] == "温和、慢条斯理" and 0.95 <= hl["length"] <= 1.1
    assert lh["length"] < 0.9 and lh["emoji"] < 0.7 and lh["delay_jitter"] > 0.4 and lh["tone"] == "生硬"
    assert ll["length"] < 0.9 and ll["delay"] > 1.2 and ll["tone"] == "平淡"
    tired = style(_st(energy=0.1))
    assert tired["length"] < 0.85 and tired["delay"] > 1.3 and tired["tone"] == "简略"


async def test_style_and_modulators_from_engine(k):
    a = k.affect
    st = a.style()
    assert set(st) >= {"length", "emoji", "delay", "tone"}
    assert st["length"] == pytest.approx(1.0, abs=0.05)
    m0 = a.modulators()
    assert set(m0) == {"willingness", "salience_affect", "patience"}   # no initiative threshold (a hazard now)
    a.battery = 0.05
    m1 = a.modulators()
    assert m1["willingness"] < m0["willingness"]
    a.battery = 0.8
    a.mood = (a.default[0] + 0.3, a.default[1] + 0.5, a.default[2])     # in a good, lively mood
    assert a.modulators()["willingness"] > m0["willingness"]
    a.apply_deltas([{"type": "anger", "intensity": 0.9}], "ev")
    m2 = a.modulators()
    assert m2["patience"] < m0["patience"] and m2["salience_affect"] > m0["salience_affect"]
    awake_w = a.modulators()["willingness"]
    a.set_asleep(True)
    assert a.modulators()["willingness"] < awake_w - 0.2


# --------------------------------------------------------------------------- router hook
async def test_energy_route_hook_downgrades_deep_when_tired(k):
    a = k.affect
    req = LLMRequest(purpose="core.reflect", tier="deep")
    assert a.route_hook(req, "deep") == "deep"
    a.sp = 0.95
    assert a.energy() < 0.3
    assert a.route_hook(req, "deep") == "deliberate"
    assert a.route_hook(req, "fast") == "fast"
    assert a.route_hook in k.llm.hooks
    resp = await k.llm.complete(LLMRequest(purpose="core.reflect", tier="deep"))
    assert resp.tier == "deliberate"
    a.set_asleep(True)                         # sleep consolidation keeps the deep tier
    assert a.route_hook(req, "deep") == "deep"


# --------------------------------------------------------------------------- persistence
async def test_snapshot_persisted_on_change_and_periodically(k):
    a = k.affect
    a.apply_deltas([{"type": "pride", "intensity": 0.5, "cause": "写完了"}], "ev")
    await k.clock.settle()
    st = await k.db.kv_get("affect", "state")
    assert st and any(e["type"] == "pride" for e in st["emotions"])
    t_saved = st["t"]
    await k.clock.advance(a.save_every_s + 1)
    st2 = await k.db.kv_get("affect", "state")
    assert st2["t"] > t_saved
    await a.save()
    evs = await k.bus.query(types=[EV.AFFECT_SNAPSHOT])
    assert evs and "labels" in evs[-1].payload and evs[-1].payload["P"] == pytest.approx(a.snapshot().P, abs=1e-3)


async def test_no_per_tick_events(k):
    a = k.affect
    for i in range(200):
        a.tick(30, k.clock.now() + 30 * (i + 1), {"workspace_load": 0.2})
    await k.clock.settle()
    evs = await k.bus.query(type_prefix="aff.")
    assert len(evs) <= 1


async def test_state_round_trip(tmp_path):
    clock = ManualClock(start=ts("2026-11-09 10:00"))
    k = await affhelp.kernel(tmp_path, clock=clock)
    a = k.affect
    a.apply_deltas([{"type": "gratitude", "intensity": 0.6, "target": "tg:L", "cause": "帮我改简历"}], "ev")
    a.battery = 0.4
    snap = a.snapshot()
    await k.stop()
    k2 = await affhelp.kernel(tmp_path, clock=ManualClock(start=clock.now()))
    try:
        s2 = k2.affect.snapshot()
        assert (s2.P, s2.A, s2.D) == pytest.approx((snap.P, snap.A, snap.D), abs=1e-3)
        assert s2.social_battery == pytest.approx(0.4, abs=1e-3)
        assert s2.emotions[0]["type"] == "gratitude" and s2.emotions[0]["cause_text"] == "帮我改简历"
    finally:
        await k2.stop()


async def test_budget_throttle_event_raises_stress(k):
    a = k.affect
    s0 = a.snapshot().stress
    await k.bus.emit(EV.BUDGET_THROTTLED, {"kind": "downgrade"})
    await k.bus.drain()
    assert a.snapshot().stress == pytest.approx(s0 + 0.05, abs=1e-3)
    await k.bus.emit(EV.BUDGET_THROTTLED, {"kind": "downgrade"})
    await k.bus.drain()
    assert a.snapshot().stress == pytest.approx(s0 + 0.05, abs=1e-3)   # rate-limited


async def test_is_sleep_time_and_window(k):
    a = k.affect
    assert a.is_sleep_time(ts("2026-11-10 02:00")) and not a.is_sleep_time(ts("2026-11-10 12:00"))
    s, e = a.sleep_window(ts("2026-11-09 12:00"))
    assert (s, e) == (ts("2026-11-09 23:40"), ts("2026-11-10 07:40"))


async def test_affect_feeds_reply_temperature_to_the_router(k):
    a = k.affect
    assert k.llm.state_source == a.llm_state
    st0 = a.llm_state()
    assert set(st0) == {"playfulness", "arousal"} and abs(st0["arousal"]) < 1e-6
    await k.llm.complete(LLMRequest(purpose="s2.reply", tier="deliberate"))
    t0 = k.mock.calls[-1].temperature
    a.mood = (a.default[0], a.default[1] + 0.5, a.default[2])          # stirred up
    await k.llm.complete(LLMRequest(purpose="s2.reply", tier="deliberate"))
    assert k.mock.calls[-1].temperature > t0
