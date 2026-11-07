"""Hybrid retrieval: routes, RRF + scoring, budget packing, tiers, access/strength updates, passive recall."""
import pytest

from alive.core.types import Trust
from alive.memory.textutil import estimate_tokens, fts_terms
from tests.memory.memhelp import MONDAY, kernel

DAY = 86400.0


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


async def note(k, content, **kw):
    kw.setdefault("kind", "semantic")
    kw.setdefault("source", "experienced")
    return await k.memory.note(content=content, **kw)


def test_fts_terms_and_tokens():
    tri, short = fts_terms("我明天要去面试")
    assert "明天要" in tri or "天要去" in tri
    tri, short = fts_terms("面试")
    assert tri == [] and short == ["面试"]
    tri, short = fts_terms("我的猫")       # all trigrams are function-word heavy -> bigram fallback
    assert tri == [] and "的猫" in short
    assert estimate_tokens("你好世界") == 4          # 4/1.5 -> 2.67 + .5 -> 3, +1
    assert estimate_tokens("hello big world") == 4


async def test_relevance_ranking_and_short_cjk_fallback(k):
    a = await note(k, "阿杰下周三有一场面试")
    await note(k, "今天的晚饭是番茄炒蛋")
    await note(k, "小林在学吉他")
    hits = await k.memory.retrieve("面试")              # 2-char query: trigram FTS cannot match it
    assert hits and hits[0].id == a
    hits = await k.memory.retrieve("阿杰的面试准备得怎么样")
    assert hits[0].id == a


async def test_person_match_and_importance(k):
    a = await note(k, "很喜欢吃辣的火锅", person_keys=["tg:a"], told_by="tg:a", source="told_by", trust=Trust.CONTACT)
    b = await note(k, "很喜欢吃辣的火锅呀", person_keys=["tg:b"], told_by="tg:b", source="told_by", trust=Trust.CONTACT)
    hits = await k.memory.retrieve("火锅", person_key="tg:a")
    assert [h.id for h in hits[:2]] == [a, b]
    hits = await k.memory.retrieve("火锅", person_key="tg:b")
    assert hits[0].id == b
    c = await note(k, "下雨天想喝热汤", importance=9)
    d = await note(k, "下雨天想喝热汤啊", importance=1)
    hits = await k.memory.retrieve("下雨天想喝热汤")
    order = [h.id for h in hits]
    assert order.index(c) < order.index(d)


async def test_stale_and_conflict_penalty(k):
    a = await note(k, "小周在北京工作")
    b = await note(k, "小周在北京工作哦")
    await k.db.execute("UPDATE mem_records SET status='stale' WHERE id=?", (a,))
    hits = await k.memory.retrieve("小周在北京工作")
    assert [h.id for h in hits[:2]] == [b, a]
    assert "可能已经过时" in k.memory.format_hits(hits, k.clock.now())


async def test_budget_packing_and_limit(k):
    for i in range(30):
        await note(k, f"关于旅行计划的第{i}条记录：我们打算去海边看日出，还要带上相机和帐篷，顺便去吃海鲜")
    hits = await k.memory.retrieve("旅行 海边 日出", budget_tokens=150, limit=12)
    used = sum(estimate_tokens(h.content) + 8 for h in hits)
    assert 1 <= len(hits) and used <= 150
    hits = await k.memory.retrieve("旅行 海边 日出", budget_tokens=100000, limit=5)
    assert len(hits) == 5
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))


async def test_entity_expansion_two_hops(k):
    a = await note(k, "阿杰养了一只布偶猫叫小花", entities=["布偶猫", "小花"], person_keys=["tg:jie"])
    b = await note(k, "最近一直在打疫苗，胃口不太好", entities=["小花"])
    await note(k, "周末要去爬山", entities=["爬山"])
    std = [h.id for h in await k.memory.retrieve("那只布偶猫最近怎么样", limit=5)]
    assert a in std and b in std          # b shares no words with the cue: reached via 小花 (hop 2)
    fast = [h.id for h in await k.memory.retrieve("那只布偶猫最近怎么样", tier="fast", limit=5)]
    assert a in fast


async def test_time_route_and_filters(k):
    old = await note(k, "上周和阿杰去看了展览", kind="episodic", observed_at=MONDAY - 6 * DAY)
    await note(k, "今天整理了房间", kind="episodic")
    hits = await k.memory.retrieve("上周我们做了什么")
    assert hits and hits[0].id == old
    hits = await k.memory.retrieve("展览", since=MONDAY - DAY)
    assert old not in [h.id for h in hits]
    hits = await k.memory.retrieve("展览", kinds=["semantic"])
    assert old not in [h.id for h in hits]


async def test_access_updates_are_batched_and_strengthen(k):
    rid = await note(k, "阿杰最喜欢的电影是千与千寻", kind="episodic")
    before = await k.memory.get(rid)
    await k.clock.advance(5 * DAY)           # long gap -> big spacing bonus on recall
    hits = await k.memory.retrieve("千与千寻")
    assert hits[0].id == rid
    assert (await k.memory.get(rid)).access_count == 0      # not written synchronously
    assert await k.memory.flush_access() == 1
    after = await k.memory.get(rid)
    assert after.access_count == 1 and after.strength > before.strength and after.last_access == k.clock.now()
    # retrieve(touch=False) does not count
    await k.memory.retrieve("千与千寻", touch=False)
    assert await k.memory.flush_access() == 0


async def test_fast_tier_makes_no_llm_calls(k):
    for i in range(20):
        await note(k, f"第{i}次聊到猫和狗的话题")
    hits = await k.memory.retrieve("猫和狗", tier="fast", limit=20)
    assert hits and len(hits) <= 20
    assert not k.mock.calls


async def test_deep_tier_reformulates(k):
    target = await note(k, "去年夏天在青岛的海边吃了很多蛤蜊", kind="episodic")
    await note(k, "最近想去旅游")
    rounds = {"n": 0}

    def planner(req):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {"enough": False, "queries": ["青岛 海边"], "time_hint": ""}
        return {"enough": True, "queries": []}
    k.mock.on("mem.deep", planner)
    hits = await k.memory.retrieve("那次吃海鲜是在哪", tier="deep")
    assert target in [h.id for h in hits]
    assert 1 <= rounds["n"] <= 3
    assert k.mock.calls_for("mem.deep")[0].tier == "deliberate"

    # garbage planner output never breaks retrieval and stops after max rounds
    k.mock.on("mem.deep", lambda req: "not json at all")
    hits = await k.memory.retrieve("海边", tier="deep")
    assert hits


async def test_deep_tier_finds_compressed(k):
    rid = await note(k, "很久以前和朋友去过一次露营", kind="episodic")
    await k.db.execute("UPDATE mem_records SET status='compressed' WHERE id=?", (rid,))
    assert rid not in [h.id for h in await k.memory.retrieve("露营")]
    k.mock.on("mem.deep", {"enough": True, "queries": []})
    assert rid in [h.id for h in await k.memory.retrieve("露营", tier="deep")]


async def test_recall_cues_are_sampled_by_activation_and_respect_min_age(k):
    """docs/03 §5.3 (#26): what comes to mind on its own is a draw, not a threshold + top-k. A strongly cued memory
    comes up most of the time, an unrelated one rarely; something just written is never "recalled" (it is still
    in context). Seeded RNG: the statistics, not one outcome, are the contract."""
    import random
    rid = await note(k, "阿杰下周三有一场很重要的面试", kind="semantic", observed_at=MONDAY - DAY)
    recent = await note(k, "阿杰的面试是下午两点", kind="semantic")   # just written: excluded (still in context)
    rng = random.Random(7)
    n = 200
    related = unrelated = recent_hits = 0
    for _ in range(n):
        ids = [h.id for h in await k.memory.recall_cues(["说起来阿杰的面试准备得怎么样了"], rng=rng, limit=2)]
        related += rid in ids
        recent_hits += recent in ids
        unrelated += bool(await k.memory.recall_cues(["今天的天气真的很不错呢适合晒被子"], rng=rng))
    assert related / n > 0.7
    assert unrelated / n < 0.1
    assert recent_hits == 0
    assert not k.mock.calls


async def test_an_aroused_mind_recalls_at_least_as_readily(k):
    """Monotone in state (same seed, same cue): higher arousal never makes a memory come up less often."""
    import random
    rid = await note(k, "上个月和阿杰一起去爬了香山", kind="episodic", observed_at=MONDAY - 20 * DAY)

    async def rate(arousal):
        k.memory._recall_state = lambda: (0.0, arousal, 0.3)
        rng = random.Random(11)
        hits = [rid in [h.id for h in await k.memory.recall_cues(["阿杰最近在忙什么"], rng=rng)] for _ in range(150)]
        return sum(hits) / len(hits)
    assert await rate(0.95) >= await rate(0.05)


async def test_about_person_and_episodes_between(k):
    await note(k, "小林是我的大学同学", kind="relational", person_keys=["tg:lin"])
    await note(k, "小林喜欢吃甜食", person_keys=["tg:lin"])
    await note(k, "和小林打了一下午游戏", kind="episodic", person_keys=["tg:lin"], observed_at=MONDAY - 3600)
    await note(k, "和别人无关的事", kind="episodic")
    about = await k.memory.about_person("tg:lin")
    assert len(about) == 3 and about[0].kind == "relational"
    eps = await k.memory.episodes_between(MONDAY - 2 * 3600, MONDAY + 1)
    assert [e.content for e in eps] == ["和小林打了一下午游戏", "和别人无关的事"]
    # dict-style access for callers written against the fakes
    assert eps[0]["content"] == eps[0].content and eps[0]["ts"] == eps[0].observed_at
