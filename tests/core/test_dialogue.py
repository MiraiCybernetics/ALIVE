"""Dialogue strategy (docs/19-20), speech monitor (docs/18 §4.3) and fragment resolution (docs/18 §5)."""
import json
import random

import pytest

from alive.core.coherence import rule_check
from alive.core.dialogue import MovePolicy, circle_of, guess_genre
from alive.core.types import ChatLine, ReplyTask, Trust
from alive.core.understanding import is_fragment_opener, open_topics
from alive.testing import make_test_kernel


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    yield kern
    await kern.stop()


def test_genre_and_circle():
    assert guess_genre({}, "周末一起去看展吗", False) == "invite"
    assert guess_genre({}, "改天吧最近忙", False) == "decline"
    assert guess_genre({"intent": "complaint"}, "气死了老板又改需求", False) == "venting"
    assert guess_genre({"good_news": True}, "我过了！！", False) == "good_news"
    assert guess_genre({}, "哈哈哈哈你也太菜了", False) == "banter"
    assert guess_genre({}, "无了", True) == "fragment"
    assert circle_of(0.8)[0] == "intimate" and circle_of(0.4)[0] == "familiar" and circle_of(0.05)[0] == "stranger"


async def test_policy_priors_and_gating(k):
    p = MovePolicy(k, rng=random.Random(1))
    vent = {"genre": "venting", "need": "listen", "circle": "intimate", "frame": "venting", "stage": "back"}
    assert p.prior("suggest", vent) < 0.2 < p.prior("react", vent)
    play = {**vent, "frame": "play", "genre": "banter"}
    intimate = p.prior("tease", play)
    acquaintance = p.prior("tease", {**play, "circle": "acquaintance"})
    assert intimate > 0 and 0 < acquaintance < 0.3 * intimate         # soft circles: rare, not zeroed
    assert p.prior("tease", {**play, "sensitive": True}) == 0           # never at a sensitive point
    assert p.prior("tease", vent) == 0                                  # nor while he is venting (frame)
    # @-mentioned: silence is costly but not impossible (INV-28)
    assert 0 < p.prior("wait", {**vent, "mentioned": True}) < p.prior("wait", vent)
    # which moves come to mind is a draw (Plackett-Luce), concentrated on what the situation calls for
    draws = [[m for m, _ in p.suggest(vent, "sim:ming", [])] for _ in range(300)]
    assert all(len(d) == 3 and len(set(d)) == 3 for d in draws)
    react = sum("react" in d for d in draws) / len(draws)
    sugg = sum("suggest" in d for d in draws) / len(draws)
    assert react > 0.5 and sugg < 0.02
    assert len({tuple(d) for d in draws}) > 5                           # not one fixed top-3


async def test_policy_learns_per_person(k):
    p = MovePolicy(k, rng=random.Random(2))
    sit = {"genre": "chitchat", "need": "fun", "circle": "familiar", "frame": "serious", "stage": "back"}
    for _ in range(25):
        await p.update("sim:lin", sit, ["open_q"], 0.95)
        await p.update("sim:lin", sit, ["react"], 0.05)

    def share(pk, move, n=200):
        return sum(1 for _ in range(n) if move in [m for m, _ in p.suggest(sit, pk, [])]) / n
    assert share("sim:lin", "open_q") > 0.8
    assert share("sim:lin", "react") < share("sim:other", "react")    # learned for him (and a bit globally)
    first = sum(1 for _ in range(200) if p.suggest(sit, "sim:lin", [])[0][0] == "open_q") / 200
    assert first > 0.4
    await p.load()  # persisted
    assert p.mean("p:sim:lin|chitchat|fun|open_q") > 0.8


async def test_partial_evidence_updates_weakly(k):
    p = MovePolicy(k, rng=random.Random(3))
    sit = {"genre": "chitchat", "need": "fun"}
    await p.update("sim:a", sit, ["open_q"], 0.0, weight=0.25)
    a, b, n = p.stats["p:sim:a|chitchat|fun|open_q"]
    assert (a, b) == pytest.approx((1.0, 1.25)) and n == 1
    await p.update("sim:a", sit, ["open_q"], 0.0, weight=0.0)          # no evidence, no update
    assert p.stats["p:sim:a|chitchat|fun|open_q"][2] == 1


def test_circle_membership_is_soft_and_sums_to_one():
    from alive.core.dialogue.catalog import circle_membership
    prev = None
    for i in range(101):
        m = circle_membership(i / 100)
        assert sum(m.values()) == pytest.approx(1.0) and all(v >= 0 for v in m.values())
        inner = m["intimate"] + m["familiar"]
        if prev is not None:
            assert inner >= prev - 1e-12 and inner - prev < 0.15         # monotone, no jump at 0.3
        prev = inner


async def test_ethics_gate(k):
    eng = k.mind.dialogue
    assert eng.ethics_ok({"type": "elicit", "aim": "让他愿意说说发生了什么"})
    assert not eng.ethics_ok({"type": "connect", "aim": "让他多聊一会儿别走"})
    assert not eng.ethics_ok({"type": "reframe", "aim": "改变他的政治立场"})
    assert not eng.ethics_ok({"type": "manipulate", "aim": "x"})


def _ming_history(clock):
    now = clock.now()
    h = []
    for i in range(10):
        t = now - 2 * 86400 + i * 600
        h.append(ChatLine(sender="小屿", person_key="", text="哈哈然后呢", ts=t, mine=True, msg_id=f"me{i}"))
        h.append(ChatLine(sender="阿明", person_key="sim:ming", text=f"哈哈哈今天又被老板折腾了 {i}", ts=t + 40, msg_id=f"m{i}"))
    h.append(ChatLine(sender="阿明", person_key="sim:ming", text="我下午要去考科目二了，紧张死", ts=now - 5 * 3600, msg_id="ky"))
    h.append(ChatLine(sender="小屿", person_key="", text="稳住！", ts=now - 5 * 3600 + 60, mine=True, msg_id="ky2"))
    return h


async def test_fragment_resolution_cues(k):
    h = _ming_history(k.clock)
    new = [ChatLine(sender="阿明", person_key="sim:ming", text="无了", ts=k.clock.now(), msg_id="n1")]
    assert is_fragment_opener(new, h, "sim:ming")
    topics = open_topics(h, "sim:ming", k.clock.now(), k.clock)
    assert any("科目二" in t for t in topics)


async def test_turn_plan_logging_and_outcome(k):
    h = _ming_history(k.clock)

    async def history(thread_id, limit=30):
        return h
    k.social.history = history

    async def cands(now, limit=10):
        return [{"person_key": "sim:ming", "thread_id": "sim:p:ming", "name": "阿明", "closeness": 0.8}]
    k.social.outreach_candidates = cands
    k.social.names["sim:ming"] = "阿明"
    k.mock.on("s1.read", {"sentiment": -0.3, "intent": "other"})
    k.mock.on("s2.reply", {"reading": {"feeling": "失落", "genre": "fragment", "frame": "serious",
                                       "hypotheses": [{"h": "科目二没过", "p": 0.8}, {"h": "别的事", "p": 0.2}]},
                           "moves": ["react", "closed_q"], "goal": {"type": "elicit", "aim": "接住他，让他说说", "progress": 0.2},
                           "plan_next": "他说哪项挂了就接着问细节或者陪他吐槽", "reply": ["啊…哪项"]})
    task = ReplyTask(thread_id="sim:p:ming", channel="sim", chat_type="private", chat_title="", participants=["sim:ming"],
                     history=h, new_lines=[ChatLine(sender="阿明", person_key="sim:ming", text="无了", ts=k.clock.now())],
                     trust=Trust.CONTACT)
    await k.mind.quick_read(task)
    res = await k.mind.compose_reply(task)
    assert res.segments == ["啊…哪项"]
    content = k.mock.calls_for("s2.reply")[0].messages[0]["content"]
    assert "此刻怎么说" in content and "没头没尾" in content and "科目二" in content and "亲近圈" in content
    rows = await k.db.fetchall("SELECT * FROM core_moves")
    assert len(rows) == 1 and json.loads(rows[0]["moves"]) == ["react", "closed_q"] and rows[0]["reward"] is None
    pd = await k.mind.dialogue.person("sim:ming")
    g = pd.goal                              # docs/22 §5: what I'm after is kept per person, not per space
    assert g["next"].startswith("他说哪项") and g["type"] == "elicit"
    assert "phase" not in (await k.mind.dialogue.state("sim:p:ming")).__dict__
    # his next message arrives quickly, longer, mood a bit better -> the move gets rewarded
    await k.clock.advance(60)
    k.mock.on("s1.read", {"sentiment": 0.1, "intent": "share"})
    h2 = h + [ChatLine(sender="小屿", person_key="", text="啊…哪项", ts=k.clock.now() - 50, mine=True)]
    task2 = ReplyTask(thread_id="sim:p:ming", channel="sim", chat_type="private", chat_title="", participants=["sim:ming"],
                      history=h2, new_lines=[ChatLine(sender="阿明", person_key="sim:ming",
                                                      text="倒车入库压线了，教练说我太急了，气死", ts=k.clock.now())],
                      trust=Trust.CONTACT)
    await k.mind.quick_read(task2)
    await k.mind.compose_reply(task2)
    rows = await k.db.fetchall("SELECT reward, detail FROM core_moves ORDER BY ts")
    assert rows[0]["reward"] is not None and rows[0]["reward"] > 0.6
    assert k.mind.dialogue.policy.mean("p:sim:ming|fragment|company|closed_q") is not None
    # outcome-informed plan text: the previous plan_next is surfaced
    content2 = k.mock.calls_for("s2.reply")[-1].messages[0]["content"]
    assert "我之前打算接下来" in content2


def test_speech_monitor_flags_narration_and_ai_tells():
    r = rule_check(["在的在的，我在呢", "面试那事儿，你想说就说，不想说咱就聊点别的～"])
    assert not r.ok and any("打算说出口" in i for i in r.issues)
    r = rule_check(["听起来你今天真的很委屈"])
    assert not r.ok and any("宣告共情" in i for i in r.issues)
    r = rule_check(["希望这能对你有帮助"])
    assert not r.ok
    long_msg = "今天发生了很多事情我跟你说一下首先是早上起床的时候外面在下雨然后我出门忘了带伞到了公司又发现电脑没电了"
    assert len(long_msg) > 40
    r = rule_check([long_msg], partner_len=5)
    assert not r.ok and any("说得太多" in i for i in r.issues)
    r = rule_check(["怎么了？", "是面试吗？"])
    assert any("好几个问题" in i for i in r.issues)
    r = rule_check(["哈哈哈哈你也太惨了吧"], recent_mine=["哈哈哈哈你也太惨了吧"])
    assert any("几乎一样" in i for i in r.issues)
    assert rule_check(["还没呢 咋了"], partner_len=4).ok
    assert rule_check(["啊…哪项"], partner_len=2).ok
