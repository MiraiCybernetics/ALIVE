"""Understanding engine (docs/17): baselines, deviations, schemas, group context, deep read, learning."""
import pytest

from alive.core.types import ChatLine, ReplyTask, Trust
from alive.core.understanding import compute_baseline, deviations, group_context
from alive.testing import free_message_budget, make_test_kernel


def _hist(clock, n=20):
    t = clock.now() - 86400
    out = []
    for i in range(n):
        out.append(ChatLine(sender="小屿", person_key="", text="哈哈是吗", ts=t + i * 600, mine=True, msg_id=f"me{i}"))
        out.append(ChatLine(sender="阿明", person_key="sim:ming", text=f"哈哈哈今天又被产品经理折腾了一下午 {i}",
                            ts=t + i * 600 + 60, msg_id=f"m{i}"))
    return out


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    free_message_budget(kern)
    yield kern
    await kern.stop()


async def test_baseline_and_deviations(k):
    hist = _hist(k.clock)
    b = compute_baseline(hist, "sim:ming", k.clock)
    assert b.reliable and b.laugh_rate > 0.9 and b.latency_median == 60
    my_last = ChatLine(sender="小屿", person_key="", text="你今天面试怎么样", ts=k.clock.now() - 3 * 3600, mine=True)
    new = [ChatLine(sender="阿明", person_key="sim:ming", text="嗯。", ts=k.clock.now(), msg_id="n1"),
           ChatLine(sender="阿明", person_key="sim:ming", text="还行。", ts=k.clock.now(), msg_id="n2")]
    devs = deviations(b, new, hist + [my_last], k.clock, k.clock.now())
    joined = "；".join(devs)
    assert "短很多" in joined and "句号" in joined and "哈哈" in joined and "隔了很久" in joined


async def test_baseline_unreliable_is_marked(k):
    hist = _hist(k.clock, n=3)
    b = compute_baseline(hist, "sim:ming", k.clock)
    new = [ChatLine(sender="阿明", person_key="sim:ming", text="嗯", ts=k.clock.now())]
    devs = deviations(b, new, hist, k.clock, k.clock.now())
    assert not b.reliable
    assert all("仅供参考" in d or "很久" in d or "慢" in d for d in devs)


async def test_schema_matching(k):
    lib = k.mind.understanding.schemas
    ids = [s["id"] for s in lib.match("我明天上午面试，有点紧张")]
    assert "exam_interview" in ids
    ids = [s["id"] for s in lib.match("改天吧，最近有点忙")]
    assert "polite_refusal" in ids
    assert lib.match("今天中午吃了米饭") == [] or all(s["id"] != "loss_grief" for s in lib.match("今天中午吃了米饭"))


async def test_group_context_share(k):
    now = k.clock.now()
    hist = [ChatLine(sender="小屿", person_key="", text="哈哈", ts=now - 60 * i, mine=True) for i in range(6)]
    hist += [ChatLine(sender="阿明", person_key="sim:ming", text="嗯嗯", ts=now - 30 * i) for i in range(2)]
    new = [ChatLine(sender="小林", person_key="sim:lin", text="@阿明 你周末去吗", ts=now)]
    g = group_context(hist, new, now, "小屿")
    assert g["my_share"] > g["fair_share"] * 1.5
    assert g["addressed_to_others"] == ["阿明"]


async def test_deep_read_candidates_contagion_followup(k):
    hist = _hist(k.clock)

    async def history(thread_id, limit=30):
        return hist
    k.social.history = history

    async def cands(now, limit=10):
        return [{"person_key": "sim:ming", "thread_id": "sim:p:ming", "name": "阿明", "closeness": 0.8}]
    k.social.outreach_candidates = cands
    k.social.names["sim:ming"] = "阿明"
    k.mock.on("s1.read", {"sentiment": -0.7, "intent": "emotional", "bad_news": True})
    k.mock.on("core.deep_read", {"hypotheses": [{"h": "面试没过，很失落", "p": 0.7, "evidence": "回得很短"},
                                                {"h": "只是很累", "p": 0.3, "evidence": "深夜"}],
                                 "perspective": "我准备了很久还是没过，不想被追问", "feeling": "失落",
                                 "need": "陪着", "not_need": "分析原因", "approach": "先接住", "pitfalls": ["追问细节"]})
    k.mock.on("s2.reply", {"reading": {"feeling": "失落", "feeling_intensity": 0.8, "need": "company",
                                       "hypotheses": [{"h": "面试没过", "p": 0.7}, {"h": "累了", "p": 0.3}]},
                           "strategy": "先接住情绪",
                           "candidates": [{"reply": ["怎么了怎么了？面试没过吗"], "predicted_reaction": "被追问，更烦", "score": 0.2},
                                          {"reply": ["还没睡呢", "咋了"], "predicted_reaction": "被接住", "score": 0.85}],
                           "reply": ["怎么了怎么了？面试没过吗"]})
    k.mock.on("core.lookahead", lambda req: {"predicted_reply": "没过……", "goal_progress": 0.2, "risk": 0.8}
              if "面试没过吗" in req.messages[0]["content"] else
              {"predicted_reply": "面试没过", "goal_progress": 0.8, "risk": 0.1})
    now = k.clock.now()
    task = ReplyTask(thread_id="sim:p:ming", channel="sim", chat_type="private", chat_title="", participants=["sim:ming"],
                     history=hist, new_lines=[ChatLine(sender="阿明", person_key="sim:ming", text="面试结束了。", ts=now)],
                     trust=Trust.CONTACT)
    await k.mind.quick_read(task)
    res = await k.mind.compose_reply(task)
    assert res.segments == ["还没睡呢", "咋了"]                           # lookahead picked the better candidate
    assert len(k.mock.calls_for("core.lookahead")) == 2
    deep_req = k.mock.calls_for("core.deep_read")
    assert len(deep_req) == 1
    reply_req = k.mock.calls_for("s2.reply")[0]
    assert "读懂这些消息的线索" in reply_req.messages[0]["content"]
    assert "面试没过，很失落" in reply_req.messages[0]["content"]       # deep read fed into the reply
    applied = [c for c in k.affect.calls if c[0] == "apply_deltas"]
    assert any(d.get("type") == "pity" and d["intensity"] <= 0.35 for c in applied for d in c[1][0])
    its = list(k.motivation.intentions.values())
    assert any("关心一下阿明" in i.goal for i in its)                     # schema follow-up (exam_interview)
    rows = await k.db.fetchall("SELECT * FROM core_readings")
    assert rows and rows[0]["feeling"] == "失落" and rows[0]["deep"] == 1


async def test_review_readings_feeds_learning(k):
    k.social.names["sim:ming"] = "阿明"
    await k.mind.understanding.record("sim:ming", "sim:p:ming", "ev1",
                                      {"feeling": "生气", "need": "space", "hypotheses": [{"h": "他在生我的气", "p": 0.8}]}, True)
    await k.clock.advance(60)
    await k.bus.emit("percept.message", {"text": "哈哈我没生气啦，就是在忙", "person_key": "sim:ming"},
                     thread_id="sim:p:ming")
    k.mock.on("core.review_reading", {"verdict": "refuted", "lesson": "阿明回得短多半是在忙，不是生气"})
    rep = await k.mind.understanding.review(k.clock.now() - 3600, k.clock.now() + 1)
    assert rep["refuted"] == 1
    assert k.growth.evidence and k.growth.evidence[0]["polarity"] == "harmful"
    assert any("在忙" in r["content"] and r["kind"] == "opinion" for r in k.memory.records)
