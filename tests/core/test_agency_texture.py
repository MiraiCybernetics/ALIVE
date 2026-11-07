"""Life texture (docs/21 §4) and conversational agency (docs/21 §2)."""
import random
import statistics

import pytest

from alive.core.dialogue import MovePolicy
from alive.core.dialogue.engine import PersonDialogue
from alive.core.types import ChatLine, ReplyTask, Trust
from alive.runtime.texture import LifeTexture, means_from_big_five
from alive.testing import make_test_kernel


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    yield kern
    await kern.stop()


def _run(k, tex, days, step_h=1.0):
    t = k.clock.now()
    xs = []
    for _ in range(int(days * 24 / step_h)):
        t += step_h * 3600
        k.clock.set(t)
        tex.tick(t)
        xs.append(dict(tex.x))
    return xs


async def test_texture_is_deterministic_with_seed(k):
    a = LifeTexture(k, seed=42, means=means_from_big_five({"E": 0.6, "O": 0.8}))
    b = LifeTexture(k, seed=42, means=means_from_big_five({"E": 0.6, "O": 0.8}))
    a.last_t = b.last_t = k.clock.now()
    t0 = k.clock.now()
    xa = _run(k, a, 5)
    k.clock.set(t0)
    xb = _run(k, b, 5)
    assert xa == xb


async def test_texture_tendency_mean_reversion_and_inertia(k):
    tex = LifeTexture(k, seed=7, means={"chattiness": 0.7}, config={"jumps_per_day": 0.0})
    tex.last_t = k.clock.now()
    tex.x["chattiness"] = 0.1                      # far from the mean
    xs = [x["chattiness"] for x in _run(k, tex, 20)]
    assert statistics.mean(xs[-24 * 10:]) == pytest.approx(0.7, abs=0.12)   # reverts toward the persona mean
    # inertia: hour-to-hour changes are small compared with the overall spread (not white noise)
    diffs = [abs(b - a) for a, b in zip(xs, xs[1:])]
    assert statistics.mean(diffs) < statistics.pstdev(xs) * 0.5


async def test_texture_mutations_and_interest(k):
    tex = LifeTexture(k, seed=3, config={"jumps_per_day": 3.0, "interest_mutation_share": 1.0})
    tex.last_t = k.clock.now()
    tex.observe_topic("海洋生物")
    tex.observe_topic("咖啡")
    _run(k, tex, 3)
    assert tex.mutations_log, "Poisson mutations should have happened at 3/day"
    assert tex.top_interests(1)[0] in ("海洋生物", "咖啡")
    assert "感兴趣" in tex.describe() or tex.describe() == "" or tex.phase != "normal"


async def test_texture_phases_follow_real_mood(k):
    lows = 0
    for seed in range(30):
        tex = LifeTexture(k, seed=seed, config={"jumps_per_day": 0.0})
        tex.last_t = tex.phase_since = k.clock.now() - 3 * 86400
        for _ in range(5):
            tex.observe_mood(-0.8)
        t = k.clock.now()
        for d in range(12):
            tex.tick(t + d * 86400)
            if tex.phase == "low":
                lows += 1
                break
    assert lows >= 10   # a run of bad days makes the "low" phase likely, not certain


async def test_texture_state_roundtrip(k):
    tex = LifeTexture(k, seed=9)
    tex.last_t = k.clock.now()
    _run(k, tex, 2)
    st = tex.to_state()
    t2 = LifeTexture(k, seed=1)
    t2.load_state(st)
    assert t2.x == tex.x and t2.rng.random() == tex.rng.random()


async def test_kernel_texture_feeds_self_state(k):
    k.texture.phase = "lively"
    assert "话多" in k.selfm.state_text()


def test_agency_gating_in_policy():
    class K:  # minimal kernel stub for MovePolicy
        db = None
    p = MovePolicy(K(), rng=random.Random(0))
    base = {"genre": "discussion", "need": "info", "circle": "familiar", "frame": "serious", "stage": "back",
            "has_agenda": True, "has_opinion": True}
    lo, hi = p.prior("own_view", {**base, "agency": 0.1}), p.prior("own_view", {**base, "agency": 0.9})
    assert hi > 3 * lo
    assert p.prior("self_topic", {**base, "agency": 0.9, "listen_lock": True}) == 0.0          # INV-24
    assert p.prior("disagree_soft", {**base, "agency": 0.9, "has_opinion": False}) < \
        p.prior("disagree_soft", {**base, "agency": 0.9})
    familiar = p.prior("counter", {**base, "agency": 0.9, "circle": "familiar", "frame": "play"})
    intimate = p.prior("counter", {**base, "agency": 0.9, "circle": "intimate", "frame": "play"})
    assert 0 <= familiar < 0.25 * intimate                  # soft circles: an inner-circle move, not a bucket
    assert p.prior("counter", {**base, "agency": 0.9, "circle": "intimate", "frame": "play", "sensitive": True}) == 0
    assert p.prior("own_view", {**base, "agency": 0.7, "agree_streak": True}) > p.prior("own_view", {**base, "agency": 0.7})


async def test_agency_low_when_he_needs_listening_high_in_chitchat(k):
    eng = k.mind.dialogue
    st = PersonDialogue(person_key="sim:t")         # who brings up the topics between us: per person
    conf = {"genre": "confiding", "need": "listen"}
    a1, lock1, _ = eng.compute_agency(conf, 0.7, {"sentiment": -0.6}, st, 0.5)
    chat = {"genre": "discussion", "need": "info"}
    st.topic_owners = ["them"] * 6
    a2, lock2, imb = eng.compute_agency(chat, 0.7, {"sentiment": 0.2}, st, 0.75)
    assert lock1 and a1 <= 0.25
    assert not lock2 and a2 > a1 and imb == 1.0


async def test_plan_text_reflects_agency_and_stance(k):
    async def cands(now, limit=10):
        return [{"person_key": "sim:hao", "thread_id": "sim:p:hao", "name": "浩子", "closeness": 0.8}]
    k.social.outreach_candidates = cands
    eng = k.mind.dialogue
    eng.stances["sim:hao"] = ["agree"] * 12
    now = k.clock.now()
    task = ReplyTask(thread_id="sim:p:hao", channel="sim", chat_type="private", chat_title="", participants=["sim:hao"],
                     history=[], new_lines=[ChatLine(sender="浩子", person_key="sim:hao",
                                                     text="我觉得周末就该在家躺着", ts=now)], trust=Trust.CONTACT)
    plan = await eng.prepare(task, {"sentiment": 0.3, "intent": "chat"}, None)
    assert "附和" in plan.text
    task2 = ReplyTask(thread_id="sim:p:hao", channel="sim", chat_type="private", chat_title="", participants=["sim:hao"],
                      history=[], new_lines=[ChatLine(sender="浩子", person_key="sim:hao", text="我好难受", ts=now + 60)],
                      trust=Trust.CONTACT)
    plan2 = await eng.prepare(task2, {"sentiment": -0.7, "intent": "emotional"}, None)
    assert "以他为主" in plan2.text and plan2.situation["listen_lock"]


def test_stance_moves_open_under_listen_lock_only_for_a_claim():
    """docs/21 §2.5: the empathy lock holds topic + feeling; a claim he makes keeps my view open."""
    class K:
        db = None
    p = MovePolicy(K(), rng=random.Random(0))
    base = {"genre": "venting", "need": "listen", "circle": "familiar", "frame": "venting", "stage": "back",
            "agency": 0.25, "listen_lock": True, "has_opinion": False}
    locked = p.prior("own_view", base)
    opened = p.prior("own_view", {**base, "claim": "x", "stance_open": True, "stance_agency": 0.5})
    assert opened > 2 * locked
    assert p.prior("qualify", base) == 0.0                                       # nothing to qualify
    assert p.prior("qualify", {**base, "claim": "x", "stance_open": True, "stance_agency": 0.5}) > 0
    assert p.prior("self_topic", {**base, "claim": "x", "stance_open": True, "stance_agency": 0.9}) == 0.0  # INV-24


async def test_stance_room_closes_in_acute_pain_and_confiding(k):
    eng = k.mind.dialogue
    eng.cfg = {"separate_claim": True}            # optional mechanism (off by default, docs/21 §2.5)
    vent = {"genre": "venting", "need": "listen"}
    claim, open_, sa = eng.stance_room(vent, 0.6, {"sentiment": -0.6, "claim": "某个判断"})
    assert claim and open_ and 0 < sa < 1
    assert not eng.stance_room(vent, 0.6, {"sentiment": -0.95, "claim": "某个判断"})[1]
    assert not eng.stance_room({"genre": "confiding", "need": "listen"}, 0.6, {"sentiment": -0.5, "claim": "x"})[1]
    assert not eng.stance_room(vent, 0.6, {"sentiment": -0.5, "claim": "x", "crisis_hint": True})[1]
    assert eng.stance_room(vent, 0.6, {"sentiment": -0.5, "claim": ""}) == ("", False, 0.0)


async def test_plan_separates_feeling_from_claim(k):
    eng = k.mind.dialogue
    eng.cfg = {"separate_claim": True}
    now = k.clock.now()
    task = ReplyTask(thread_id="sim:p:hao", channel="sim", chat_type="private", chat_title="", participants=["sim:hao"],
                     history=[], new_lines=[ChatLine(sender="浩子", person_key="sim:hao", text="烦", ts=now)],
                     trust=Trust.CONTACT)
    plan = await eng.prepare(task, {"sentiment": -0.6, "intent": "complaint", "claim": "某个判断"}, None)
    assert plan.situation["listen_lock"] and plan.situation["stance_open"]
    assert "两回事" in plan.text and "某个判断" in plan.text
    plan2 = await eng.prepare(task, {"sentiment": -0.6, "intent": "complaint", "claim": ""}, None)
    assert "两回事" not in plan2.text



async def test_claim_separation_is_off_by_default(k):
    eng = k.mind.dialogue
    assert eng.stance_room({"genre": "venting", "need": "listen"}, 0.6, {"sentiment": -0.5, "claim": "x"}) == ("", False, 0.0)
