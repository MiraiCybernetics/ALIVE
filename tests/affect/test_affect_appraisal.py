"""Appraisal (quick rules, OCC mapping, divergence, intensity modulation), need coupling, rumination,
reappraisal and regulation triggers (docs/04 §3.1, §3.4, §4, §5)."""
import pytest

from tests.affect import affhelp
from tests.affect.affhelp import emo
from alive.affect.appraisal import QUICK_CAP, occ_rules, quick_rules
from alive.affect.tables import normalize_type
from alive.core.types import EV

H = 3600.0


@pytest.fixture
async def k(tmp_path):
    k = await affhelp.kernel(tmp_path)
    yield k
    await k.stop()


# --------------------------------------------------------------------------- quick appraisal
async def test_quick_appraisal_rule_table_and_cap(k):
    a = k.affect
    created = a.appraise_quick({"praised": True, "thanked": True, "good_news": 1.0, "relationship_weight": 1.0},
                               "ev1", target="tg:1")
    types = {e["type"] for e in created}
    assert {"pride", "gratification", "joy"} <= types
    assert all(e["intensity"] <= QUICK_CAP + 1e-9 for e in created)
    assert all(e["target"] == "tg:1" and e["cause"] == "ev1" and e["source"] == "quick" for e in created)


async def test_quick_appraisal_relationship_branches():
    close = {e[0] for e in quick_rules({"ignored": True, "relationship_weight": 0.9})}
    far = {e[0] for e in quick_rules({"ignored": True, "relationship_weight": 0.1})}
    assert close == {"sadness"} and far == {"disappointment"}
    assert {e[0] for e in quick_rules({"insulted": True, "relationship_weight": 0.1})} == {"irritation"}
    assert {e[0] for e in quick_rules({"insulted": True, "relationship_weight": 0.9})} == {"anger", "distress"}
    assert quick_rules({"sentiment": 0.8})[0][0] == "joy"
    assert quick_rules({"sentiment": -0.8})[0][0] == "distress"
    assert quick_rules({"sentiment": 0.1}) == []
    assert {e[0] for e in quick_rules({"praised": True, "sentiment": -0.9})} == {"pride"}  # explicit signal wins


async def test_quick_appraisal_emits_delta_and_bumps_stress(k):
    a = k.affect
    s0 = a.snapshot().stress
    a.appraise_quick({"insulted": True, "target_name": "陌生人"}, "ev-insult", target="tg:9")
    assert a.snapshot().stress > s0
    await k.clock.settle()
    evs = await k.bus.query(types=[EV.AFFECT_DELTA])
    assert evs and evs[-1].causal_parent == "ev-insult" and evs[-1].payload["source"] == "quick"


# --------------------------------------------------------------------------- OCC rules
@pytest.mark.parametrize("appraisal,expected", [
    ({"desirability": 0.7}, "joy"),
    ({"desirability": -0.7}, "distress"),
    ({"desirability": 0.6, "likelihood": 0.5}, "hope"),
    ({"desirability": -0.6, "likelihood": "possible"}, "anxiety"),
    ({"desirability": -0.6, "likelihood": 1.0, "prospect": "feared"}, "fears_confirmed"),
    ({"desirability": 0.6, "prospect": "feared"}, "relief"),
    ({"desirability": 0.6, "prospect": "hoped"}, "satisfaction"),
    ({"desirability": -0.6, "prospect": "hoped"}, "disappointment"),
    ({"agency": "self", "praiseworthiness": 0.8}, "pride"),
    ({"agency": "self", "praiseworthiness": -0.8}, "shame"),
    ({"agency": "self", "praiseworthiness": 0.8, "desirability": 0.6}, "gratification"),
    ({"agency": "self", "praiseworthiness": -0.8, "desirability": -0.6}, "remorse"),
    ({"agency": "other", "praiseworthiness": 0.8}, "admiration"),
    ({"agency": "对方", "praiseworthiness": -0.8}, "reproach"),
    ({"agency": "other", "praiseworthiness": 0.8, "desirability": 0.7}, "gratitude"),
    ({"agency": "other", "praiseworthiness": -0.8, "desirability": -0.7}, "anger"),
    ({"agency": "third", "praiseworthiness": -0.6, "desirability": -0.5}, "anger"),
    ({"desirability_for_other": 0.8, "desirability": 0.5}, "happy_for"),
    ({"desirability_for_other": -0.8, "desirability": -0.5}, "pity"),
    ({"desirability_for_other": 0.8, "desirability": -0.5}, "resentment"),
    ({"desirability_for_other": -0.8, "desirability": 0.5}, "gloating"),
    ({"appealingness": 0.5}, "liking"),
    ({"appealingness": 0.9}, "love"),
    ({"appealingness": -0.9}, "hate"),
])
def test_occ_rule_mapping(appraisal, expected):
    out = occ_rules(appraisal)
    assert out and out[0][0] == expected, out


def test_occ_rules_neutral_relevance_and_coping():
    assert occ_rules({"desirability": 0.05}) == []
    hi = occ_rules({"desirability": 0.8, "relevance": 1.0})[0][1]
    lo = occ_rules({"desirability": 0.8, "relevance": 0.25})[0][1]
    assert lo == pytest.approx(hi / 4)
    helpless = occ_rules({"desirability": -0.6, "coping_potential": 0.0})[0][1]
    capable = occ_rules({"desirability": -0.6, "coping_potential": 1.0})[0][1]
    assert helpless > capable
    # agency=environment cannot produce agent-directed emotions
    assert occ_rules({"agency": "environment", "praiseworthiness": -0.9, "desirability": -0.5})[0][0] == "distress"


async def test_prospect_confirmation_uses_active_hope_or_fear(k):
    a = k.affect
    a.apply_deltas([{"desirability": 0.7, "likelihood": 0.5, "target": "tg:M", "cause": "M 的面试"}], "ev1")
    assert emo(a, "hope")
    out = a.apply_deltas([{"desirability": 0.8, "likelihood": "happened", "target": "tg:M", "cause": "M 的面试"}], "ev2")
    assert [e["type"] for e in out] == ["satisfaction"]
    assert emo(a, "hope")["intensity"] < 0.3   # the prospect resolved
    a.apply_deltas([{"desirability": -0.7, "likelihood": 0.4, "target": "tg:L", "cause": "体检结果"}], "ev3")
    assert emo(a, "anxiety")
    out = a.apply_deltas([{"desirability": 0.6, "target": "tg:L", "cause": "体检结果"}], "ev4")
    assert out[0]["type"] == "relief"


async def test_llm_type_vs_rule_divergence_is_recorded(k):
    a = k.affect
    out = a.apply_deltas([{"type": "joy", "intensity": 0.5, "desirability": -0.7, "cause": "考砸了"}], "ev")
    assert [e["type"] for e in out] == ["distress"]          # the deterministic rule wins
    assert a.divergences[-1]["llm"] == "joy" and a.divergences[-1]["rule"] == "distress"
    out = a.apply_deltas([{"type": "开心", "intensity": 0.5, "desirability": 0.6, "cause": "放假"}], "ev2")
    assert [e["type"] for e in out] == ["joy"]               # agreement (via alias): no divergence
    assert a.counters["divergences"] == 1
    out = a.apply_deltas([{"type": "weltschmerz", "intensity": 0.5}], "ev3")
    assert out == [] and a.divergences[-1].get("note") == "unknown_type"
    await k.clock.settle()
    evs = await k.bus.query(types=[EV.AFFECT_DELTA])
    assert any(e.payload["divergence"] for e in evs)


async def test_explicit_types_and_aliases(k):
    a = k.affect
    out = a.apply_deltas([{"emotion": "worried", "intensity": 0.4, "cause": "明天考试"},
                          {"type": "感激", "intensity": 0.4, "target": "tg:L", "target_name": "L"}], "ev")
    assert [e["type"] for e in out] == ["anxiety", "gratitude"]
    assert normalize_type("Happy") == "joy" and normalize_type("难过") == "sadness" and normalize_type("??") == ""


# --------------------------------------------------------------------------- intensity modulation
async def test_intensity_modulation(k):
    a = k.affect
    a.mood = (0.0, 0.0, 0.0)
    base = lambda t, rw=0.5: a._modulated(t, 0.5, rw, 1.0)  # noqa: E731
    # temperament sensitivity: distress 1.1, anger 0.8 (temperament.yaml)
    assert base("distress") == pytest.approx(0.5 * 1.1)
    assert base("irritation") == pytest.approx(0.5 * 0.8)   # family of anger
    # relationship weight: people I care about move me more
    assert base("joy", 1.0) > base("joy", 0.5) > base("joy", 0.0)
    # mood congruence: negative mood amplifies negative emotions, damps positive ones
    a.mood = (-0.6, 0.0, 0.0)
    assert a._modulated("sadness", 0.4, 0.5, 1.0) == pytest.approx(0.4 * 1.1 * 1.5)   # family distress
    assert a._modulated("joy", 0.4, 0.5, 1.0) == pytest.approx(0.4 * 0.5)
    # low energy -> more easily annoyed
    a.mood = (0.0, 0.0, 0.0)
    a.sp = 0.95
    assert a._modulated("irritation", 0.4, 0.5, 1.0) == pytest.approx(0.4 * 0.8 * 1.3)
    assert a._modulated("joy", 0.4, 0.5, 1.0) == pytest.approx(0.4)


async def test_same_type_target_and_cause_merge(k):
    a = k.affect
    a.appraise_quick({"praised": True}, "ev", target="tg:1")
    a.appraise_quick({"praised": True}, "ev", target="tg:1")
    prides = [e for e in a.snapshot().emotions if e["type"] == "pride"]
    assert len(prides) == 1 and prides[0]["intensity"] <= QUICK_CAP + 1e-9


# --------------------------------------------------------------------------- needs, rumination, reappraisal
async def test_need_events_couple_to_emotions(k):
    a = k.affect
    a.on_need_event("social", 0.3)
    a.on_need_event("competence", -0.2)
    a.on_need_event("safety", -0.3)
    a.on_need_event("curiosity", 0.01)   # too small to matter
    types = {e["type"] for e in a.snapshot().emotions}
    assert {"gratitude", "disappointment", "anger"} <= types and "interest" not in types


async def test_rumination_bump_capped_at_initial(k):
    a = k.affect
    a.apply_deltas([{"type": "sadness", "intensity": 0.6, "cause": "分别"}], "ev-sad")
    e = emo(a, "sadness")
    initial = e["intensity"]
    await k.clock.advance(6 * H)
    decayed = emo(a, "sadness")["intensity"]
    a.ruminate("ev-sad", 1.0)
    bumped = emo(a, "sadness")["intensity"]
    assert bumped == pytest.approx(decayed + 0.3 * initial, abs=2e-4)
    for _ in range(20):
        a.ruminate("ev-sad", 1.0)
    assert emo(a, "sadness")["intensity"] <= initial + 1e-9
    a.ruminate("other-cause", 1.0)  # unrelated cause -> no effect


async def test_reappraise_scales_and_removes(k):
    a = k.affect
    a.apply_deltas([{"type": "anger", "intensity": 0.8, "cause": "L 没回消息"}], "ev-a")
    i0 = emo(a, "anger")["intensity"]
    a.reappraise("ev-a", 0.5)
    assert emo(a, "anger")["intensity"] == pytest.approx(i0 / 2, abs=2e-4)
    a.ruminate("ev-a", 1.0)
    assert emo(a, "anger")["intensity"] <= i0 / 2 + 2e-4   # rumination cannot restore the old meaning
    a.reappraise("L 没回消息", 0.0)                         # match by cause text
    assert emo(a, "anger") is None


# --------------------------------------------------------------------------- regulation
async def test_regulation_trigger_strong_negative_with_cooldown(k):
    a = k.affect
    assert a.regulation_needed() is None
    a.apply_deltas([{"type": "distress", "intensity": 0.9, "cause": "被误解"}], "ev", target_weight=1.0)
    r = a.regulation_needed()
    assert r and r["reason"] == "strong_negative_emotion" and r["emotion"]["type"] == "distress"
    assert r["strategy"] == ["reappraise"]                 # temperament regulation_style
    assert a.regulation_needed()["key"] == r["key"]        # idempotent for check-then-act callers
    assert a.counters["regulations"] == 1
    await k.clock.advance(a.REG_GRACE_S + 1)
    assert a.regulation_needed() is None                   # quiet during the cooldown
    await k.clock.advance(a.regulation_cooldown_s)
    assert a.regulation_needed() is not None and a.counters["regulations"] == 2


async def test_regulation_check_then_act_flow_like_the_core(k):
    """mind: `if affect.regulation_needed(): inner.regulate()` where regulate() asks again, then calls
    reappraise(emotion["cause"], f). Emotions created without an event id must still be matched."""
    a = k.affect
    a.apply_deltas([{"type": "sadness", "intensity": 0.95, "cause": "L 没回消息"}], "", target_weight=1.0)
    assert a.regulation_needed()
    need = a.regulation_needed()
    assert need is not None
    cause = need["emotion"]["cause"]
    assert cause == need["emotion"]["id"]                  # fallback when there was no cause event
    i0 = need["emotion"]["intensity"]
    a.reappraise(cause, 0.8)
    s = [e for e in a.snapshot().emotions if e["type"] == "sadness"][0]
    assert s["intensity"] == pytest.approx(i0 * 0.8, abs=2e-4)
    assert a.regulation_needed() is None                   # handled: no immediate re-trigger


async def test_regulation_trigger_panic_like_state_and_high_negative_arousal(k):
    a = k.affect
    assert not a.high_negative_arousal()
    a.apply_deltas([{"type": "fear", "intensity": 0.9, "cause": "账号被盗"},
                    {"type": "anger", "intensity": 0.9, "cause": "被骗"}], "ev", target_weight=1.0)
    assert a.high_negative_arousal()
    r = a.regulation_needed()
    assert r["reason"] == "strong_negative_emotion"
    a.reappraise(r["emotion"]["cause"], 1.0)               # handled (no change in meaning)
    r2 = a.regulation_needed()
    assert r2 is not None and r2["reason"] == "high_negative_arousal" and "calm" in r2["strategy"]
    a.reappraise("ev", 0.2)
    a.mood = (0.2, 0.1, 0.1)
    assert not a.high_negative_arousal()


async def test_regulate_uses_llm_reappraisal(k):
    a = k.affect
    k.mock.on("aff.reappraise", {"reappraisal": "他可能只是太忙了，不是不理我", "factor": 0.5})
    a.apply_deltas([{"type": "sadness", "intensity": 0.95, "cause": "L 没回消息"}], "ev", target_weight=1.0)
    i0 = emo(a, "sadness")["intensity"]
    out = await a.regulate()
    assert out["factor"] == 0.5 and "太忙" in out["reappraisal"]
    assert emo(a, "sadness")["intensity"] == pytest.approx(i0 / 2, rel=1e-3)
    assert k.mock.calls_for("aff.reappraise")[0].tier == "fast"


async def test_regulate_falls_back_without_llm(k):
    a = k.affect
    k.mock.on("aff.reappraise", lambda req: (_ for _ in ()).throw(RuntimeError("down")))
    a.apply_deltas([{"type": "distress", "intensity": 0.95, "cause": "x"}], "ev", target_weight=1.0)
    i0 = emo(a, "distress")["intensity"]
    out = await a.regulate()
    assert out["factor"] == pytest.approx(0.85)
    assert emo(a, "distress")["intensity"] == pytest.approx(i0 * 0.85, rel=1e-3)
