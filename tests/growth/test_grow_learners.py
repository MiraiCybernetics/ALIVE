"""Expression & jargon learners (MaiBot style): batched per thread, learn from USER lines only, deterministic
validation, candidates first, AI review + evidence before activation."""
import pytest

from tests.growth.growhelp import chat, kernel

G = "tg:g1"


def group_lines(k, n_user=25, mine_text="对对对我也这么觉得"):
    specs = []
    for i in range(n_user):
        pk, name = (("tg:a", "阿杰"), ("tg:b", "小林"), ("tg:c", "老王"))[i % 3]
        specs.append((pk, name, ["xswl 这也太离谱了", "对对对你说得都对", "绝绝子", "今天又加班了", "笑死"][i % 5]))
        if i == 3:
            specs.append(("", "小屿", mine_text))
    return chat(k, specs, thread=G)


STYLE_REPLY = {"rules": [
    {"situation": "表示讽刺的赞同", "style": "使用“对对对”", "example": "对对对你说得都对"},     # valid
    {"situation": "表示附和", "style": "说“我也这么觉得”", "example": "对对对我也这么觉得"},      # my own line
    {"situation": "和阿杰开玩笑", "style": "叫他杰哥", "example": "xswl 这也太离谱了"},           # person name
    {"situation": "觉得好笑", "style": "这是一个非常非常非常非常非常长的说法描述超过了限制", "example": "笑死"},
    {"situation": "吐槽加班", "style": "说“又加班了”", "example": "这句话根本不在聊天里"},       # not a real line
    "garbage",
]}
JARGON_REPLY = {"terms": [
    {"term": "xswl", "meaning": "笑死我了", "example": "xswl 这也太离谱了", "confidence": 0.9},
    {"term": "小屿", "meaning": "群里的 AI", "example": "", "confidence": 0.9},              # a name
    {"term": "zzz", "meaning": "睡觉", "example": "", "confidence": 0.5},                   # never said
]}


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path, cfg={"growth": {"learning": {"blacklist": {"persons": ["tg:banned"]}}}})
    kern.mock.on("grow.learn_style", STYLE_REPLY)
    kern.mock.on("grow.jargon", JARGON_REPLY)
    yield kern
    await kern.stop()


async def test_learns_from_user_lines_only(k):
    await k.growth.observe_messages(G, "group", group_lines(k))
    await k.clock.advance(1)
    await k.growth.drain()
    calls = k.mock.calls_for("grow.learn_style")
    assert len(calls) == 1 and calls[0].tier == "fast"
    prompt = calls[0].messages[0]["content"]
    assert "我也这么觉得" not in prompt and "小屿" not in prompt      # my lines never reach the learner
    assert "阿杰" not in prompt                                       # speakers are anonymised (U1, U2...)
    cands = await k.growth.artifacts.list(type_="expression")
    style = [a for a in cands if a.kind == "style"]
    jargon = [a for a in cands if a.kind == "jargon"]
    assert [(a.situation, a.text) for a in style] == [("表示讽刺的赞同", "使用“对对对”")]
    assert [a.jargon["term"] for a in jargon] == ["xswl"] and jargon[0].text == "笑死我了"
    assert all(a.status == "candidate" and a.scope == f"channel:{G}" for a in cands)
    assert style[0].examples == ["对对对你说得都对"] and style[0].meta["example_by"] == ["tg:b"]
    # candidates are not used in prompts
    assert await k.growth.expressions_for("对对对", [f"channel:{G}"]) == []


async def test_batching_thresholds_and_idle(k):
    await k.growth.observe_messages(G, "group", group_lines(k, n_user=10))
    await k.clock.advance(60)
    assert not k.mock.calls_for("grow.learn_style")              # below the group threshold
    await k.clock.advance(1200)                                  # idle -> learn with what we have (>= min_lines)
    assert len(k.mock.calls_for("grow.learn_style")) == 1
    await k.growth.observe_messages(G, "group", chat(k, [("tg:a", "阿杰", "嗯")], thread=G))
    await k.clock.advance(5000)
    assert len(k.mock.calls_for("grow.learn_style")) == 1        # 1 line < min_lines: kept for later
    # private chats use a smaller threshold and person scope
    priv = chat(k, [("tg:a", "阿杰", f"好耶好耶{i}") for i in range(12)], thread="tg:a")
    await k.growth.observe_messages("tg:a", "private", priv)
    await k.clock.advance(1)
    await k.growth.drain()
    assert len(k.mock.calls_for("grow.learn_style")) == 2


async def test_blacklist_and_learning_paused(k, monkeypatch):
    lines = chat(k, [("tg:banned", "黑名单", f"对对对你说得都对{i}") for i in range(30)], thread=G)
    await k.growth.observe_messages(G, "group", lines)
    await k.clock.advance(5000)
    assert not k.mock.calls_for("grow.learn_style")
    monkeypatch.setattr(type(k.gov), "learning_paused", property(lambda self: True))
    await k.growth.observe_messages(G, "group", group_lines(k))
    await k.clock.advance(5000)
    assert not k.mock.calls_for("grow.learn_style")


async def test_expression_activation_through_the_night(k):
    k.mock.on("grow.verify", lambda req: {"results": [{"item": i, "ok": True, "generic": True}
                                                      for i in range(1, 20)]})
    # the same rule observed in two batches -> seen twice -> passes the observation gate
    for _ in range(2):
        await k.growth.observe_messages(G, "group", group_lines(k))
        await k.clock.advance(1)
        await k.growth.drain()
    style = [a for a in await k.growth.artifacts.list(type_="expression") if a.kind == "style"][0]
    assert style.seen_count == 2
    rep = await k.growth.run_sleep(k.clock.now(), stages=["learn", "verify"])
    st = {s["stage"]: s for s in rep["stages"]}
    assert st["learn"]["report"]["quarantined"] == 2 and st["verify"]["report"]["promoted"] == 2
    got = await k.growth.expressions_for("xswl 这也太好笑了", [f"channel:{G}"])
    assert got[0].kind == "jargon"                               # jargon in the cue comes first
    assert any(a.kind == "style" for a in got) or len(got) == 1
    block = k.growth.format_playbook([], got)
    assert "xswl：笑死我了" in block
    # other channels do not see channel-scoped expressions
    assert await k.growth.expressions_for("xswl", ["channel:tg:other"]) == []
