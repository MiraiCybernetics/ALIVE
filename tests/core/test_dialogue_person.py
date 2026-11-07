"""Dialogue state per PERSON, not per space (docs/22 §5, ADR-0005), move learning that does not treat silence as
failure (docs/19 §7), front stage from the audience (docs/20 §4), sampled choices instead of argmax (ADR-0007)."""
import json
import random
from collections import Counter

import pytest

from alive.core.types import ChatLine, ReplyTask, Trust
from alive.testing import make_test_kernel


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    yield kern
    await kern.stop()


def _task(k, space, pk, name, text, chat_type="private", history=(), mentioned=None):
    return ReplyTask(thread_id=space, channel="sim", chat_type=chat_type, chat_title="", participants=[pk],
                     history=list(history), new_lines=[ChatLine(sender=name, person_key=pk, text=text, ts=k.clock.now())],
                     trust=Trust.CONTACT, mentioned=(chat_type == "group") if mentioned is None else mentioned)


async def _move(k, mid, ts, pk, moves, genre="chitchat", need="company", sentiment=0.0, space=None):
    await k.db.execute("INSERT INTO core_moves (id,ts,thread_id,person_key,genre,frame,need,circle,moves,goal,sentiment) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (mid, ts, space or pk, pk, genre, "serious", need, "familiar", json.dumps(moves), "{}", sentiment))


# ------------------------------------------------------------------------------------------- per-person state
async def test_what_i_meant_to_do_next_with_him_follows_him_into_a_group(k):
    eng = k.mind.dialogue
    dm = _task(k, "sim:ming", "sim:ming", "阿明", "明天面试，有点慌")
    plan = await eng.prepare(dm, {"sentiment": -0.3, "intent": "emotional"}, None)
    await eng.after_reply(dm, plan, {"moves": ["reflect", "open_q"], "goal": {"type": "comfort", "aim": "让他安心一点"},
                                     "plan_next": "面试完问问他感觉怎么样", "reply": ["慌很正常"]}, "e1",
                          {"sentiment": -0.3})
    await k.clock.advance(120)
    grp = _task(k, "sim:g:g1", "sim:ming", "阿明", "@小屿 你们周末干嘛", chat_type="group")
    plan2 = await eng.prepare(grp, {"sentiment": 0.2, "intent": "chat"}, None)
    assert "面试完问问他感觉怎么样" in plan2.text and "让他安心一点" in plan2.text
    pd = await eng.person("sim:ming")
    assert pd.my_moves[-2:] == ["reflect", "open_q"]                # my moves toward HIM, from any space
    st = await eng.state("sim:g:g1")
    assert not hasattr(st, "goals") and "sim:ming" in st.speakers    # the space keeps facts about the space
    eng.people.clear()                                               # persisted per person
    assert (await eng.person("sim:ming")).goal["aim"] == "让他安心一点"


async def test_old_per_space_state_moves_to_the_person(k):
    now = k.clock.now()
    await k.db.kv_set("dialogue", "sim:g:old", {
        "thread_id": "sim:g:old", "phase": "closing", "my_moves": ["react"], "topic_owners": ["them"],
        "goals": {"sim:a": {"type": "connect", "aim": "陪他聊聊", "touched": now, "next": "问问他猫怎么样了"}}})
    eng = k.mind.dialogue
    await eng.state("sim:g:old")
    pd = await eng.person("sim:a")
    assert pd.goal["next"] == "问问他猫怎么样了" and pd.my_moves == ["react"] and pd.topic_owners == ["them"]
    stored = await k.db.kv_get("dialogue", "sim:g:old")
    assert "goals" not in stored and "phase" not in stored


def test_phase_is_gone_from_the_contract():
    from alive.core import prompts
    from alive.core.mind import REPLY_SCHEMA
    assert "phase" not in REPLY_SCHEMA and '"phase"' not in prompts.REPLY_CONTRACT


# ------------------------------------------------------------------------------------------- move learning
async def test_silence_is_not_failure_unless_i_asked_for_an_answer(k):
    eng = k.mind.dialogue
    now = k.clock.now()
    await _move(k, "mv_close", now - 2 * 86400, "sim:a", ["close"], genre="farewell", need="space")
    await _move(k, "mv_presence", now - 2 * 86400, "sim:a", ["presence", "react"])
    await _move(k, "mv_q_old", now - 2 * 86400, "sim:a", ["react", "closed_q"])
    await _move(k, "mv_q_3h", now - 3 * 3600, "sim:b", ["open_q"])
    await _move(k, "mv_recent", now - 600, "sim:a", ["closed_q"])            # still in flight
    assert await eng.sweep_ignored() == 2
    r = {row["id"]: row for row in await k.db.fetchall("SELECT * FROM core_moves")}
    assert r["mv_close"]["reward"] == -1 and r["mv_presence"]["reward"] == -1      # unevaluated, not failures
    assert r["mv_recent"]["reward"] is None
    assert r["mv_q_old"]["reward"] == pytest.approx(0.2)
    e_old = json.loads(r["mv_q_old"]["detail"])["evidence"]
    e_3h = json.loads(r["mv_q_3h"]["detail"])["evidence"]
    assert 0 < e_3h < e_old <= 1 and e_old > 0.9         # surer the longer it stays unanswered, as THEY usually reply
    stats = eng.policy.stats
    assert not any(key.endswith(("|close", "|presence", "|react")) for key in stats)   # nothing learned from those
    a, b, n = stats["p:sim:a|chitchat|company|closed_q"]
    assert b > a and n == 1


async def test_reward_is_how_it_landed_not_speed_or_length(k):
    eng = k.mind.dialogue

    async def score(text, delay, sent_after, moves=("closed_q",), mine="面试怎么样？"):
        await k.db.execute("DELETE FROM core_moves")
        now = k.clock.now()
        await _move(k, "mv", now - delay, "sim:a", list(moves))
        hist = [ChatLine(sender="小屿", person_key="", text=mine, ts=now - delay, mine=True)]
        return await eng._evaluate_previous(_task(k, "sim:a", "sim:a", "小A", text, history=hist),
                                            {"sentiment": sent_after}, "sim:a")
    short = await score("过了！", 60, 0.6)
    long = await score("过了！！面试官人超好，问的全是我准备过的，出来我腿都软了哈哈", 60, 0.6)
    assert abs(long - short) < 0.09                     # length is not the reward (picking up my words adds a bit)
    slow = await score("过了！", 6 * 3600, 0.6)
    assert 0 <= short - slow <= 0.05 + 1e-9             # latency relative to their own pace: a weak term at most
    assert await score("过了吧", 60, -0.4) < short - 0.2  # how they feel afterwards matters
    assert await score("别问了", 60, 0.0) < short - 0.3   # negative feedback matters
    ack_to_question = await score("嗯", 60, 0.0)
    ack_to_goodnight = await score("嗯", 60, 0.0, moves=("close",), mine="那先这样，晚安")
    assert ack_to_goodnight > ack_to_question            # an acknowledgement is the right answer to a closing


# ------------------------------------------------------------------------------------------- stage, choices
async def test_front_stage_comes_from_the_audience_not_the_chat_type(k):
    eng = k.mind.dialogue
    now = k.clock.now()
    close = {"sim:a": 0.8, "sim:bestie": 0.95}

    async def closeness(pk):
        return close.get(pk, 0.05)
    k.mind.understanding.closeness = closeness

    def grp(space, others):
        hist = [ChatLine(sender=f"P{i}", person_key=pk, text="哈哈哈", ts=now - 60 * (i + 1))
                for i, pk in enumerate(others)]
        return _task(k, space, "sim:a", "小A", "你们觉得呢", chat_type="group", history=hist)
    alone = await eng.prepare(grp("sim:g:1", []), {"sentiment": 0.1}, None)
    crowd = await eng.prepare(grp("sim:g:2", ["sim:x1", "sim:x2", "sim:x3"]), {"sentiment": 0.1}, None)
    friend = await eng.prepare(grp("sim:g:3", ["sim:bestie"]), {"sentiment": 0.1}, None)
    assert alone.situation["publicness"] < 0.05 and "只有我和他" in alone.text     # a quiet group: back stage
    assert crowd.situation["publicness"] > 0.8 and "前台" in crowd.text
    assert friend.situation["publicness"] < 0.3
    assert crowd.situation["face_risk"] > alone.situation["face_risk"]


async def test_candidate_choice_is_a_softmax_not_an_argmax(k):
    k.chance_rng = random.Random(5)
    picks = Counter()
    for _ in range(400):
        data = {"candidates": [{"reply": ["B"], "score": 0.7}, {"reply": ["A"], "score": 0.8},
                               {"reply": ["C"], "score": 0.1}]}
        k.mind._pick_candidate(data)
        picks[data["reply"][0]] += 1
    assert picks["A"] > picks["B"] > 50 and picks["C"] < 10


async def test_agency_wording_is_soft_at_the_old_band_edges(k):
    eng = k.mind.dialogue
    k.chance_rng = random.Random(1)
    strong = sum("不必顺着他" in eng._agency_line(0.61) for _ in range(300)) / 300
    assert 0.3 < strong < 0.8                           # near the old 0.6 edge it goes either way
    assert not any("不必顺着他" in eng._agency_line(0.1) for _ in range(200))
    assert sum(eng._agency_line(0.95).startswith("此刻我可以有自己的态度") for _ in range(50)) >= 48


async def test_sharing_what_i_read_is_a_draw_not_a_cutoff(k):
    inner = k.mind.inner
    p = inner.share_probability
    assert p(0.3, 0.5) < p(0.6, 0.5) < p(0.9, 0.5)              # grows with interest ...
    assert p(0.6, 0.9) > p(0.6, 0.1)                           # ... and with closeness
    assert 0.0 < p(0.45, 0.8) < 1.0                             # below the old 0.6 cut-off: sometimes
    inner.rng = random.Random(4)
    near = sorted(inner.share_delay(0.9) for _ in range(301))
    far = sorted(inner.share_delay(0.1) for _ in range(301))
    assert 300 < near[150] < far[150] < 3600                     # around the old 20 min, sooner for close people
    assert near[15] < 0.5 * near[150] and near[285] > 2 * near[150]     # right-skewed, not "always 1200 s"


async def test_follow_up_gap_is_drawn_around_the_schema_gap(k):
    import types
    k.chance_rng = random.Random(8)
    drafts = []

    async def add(draft, src=""):
        drafts.append(draft)

    async def none(*a, **kw):
        return []
    k.motivation.add_intention = add
    k.motivation.active_intentions = none
    cues = types.SimpleNamespace(schemas=[{"name": "面试", "follow_up": {"ask": "问问怎么样了", "gap_hours": 8}}])
    for i in range(201):
        await k.mind._schema_followups(cues, f"sim:p{i}", {}, "e1")
    gaps = sorted(d["due_in_s"] / 3600 for d in drafts)
    assert len(gaps) == 201 and 4 < gaps[100] < 14                # the schema's 8 h is the typical gap
    assert gaps[10] < 0.7 * gaps[100] and gaps[190] > 1.3 * gaps[100]
    assert all(d["trigger"]["type"] == "time" and d["reason_type"] == "care" for d in drafts)
