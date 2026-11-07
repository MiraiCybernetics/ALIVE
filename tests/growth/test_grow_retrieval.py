"""playbook_for / expressions_for / format_playbook: scope matching, cue similarity, stable global prefix."""
import pytest

from tests.growth.growhelp import kernel


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path, cfg={"growth": {"prompt": {"global_slots": 3}}})
    yield kern
    await kern.stop()


async def activate(k, text, scope="global", type_="playbook", **kw):
    if type_ == "playbook":
        a = await k.growth.propose_entry(text, scope=scope)
    else:
        a, _ = await k.growth.artifacts.create("expression", text, scope=scope, status="candidate", **kw)
    await k.growth.artifacts.set_status(a.id, "quarantined")
    return await k.growth.artifacts.set_status(a.id, "active")


async def test_scopes_similarity_and_stable_global_prefix(k):
    g = [await activate(k, t) for t in ("别连发消息", "答应的事要记得", "不懂就问", "少用感叹号")]
    jie_work = await activate(k, "和阿杰聊工作时先问感受", scope="person:tg:jie")
    jie_game = await activate(k, "和阿杰聊游戏时可以多开玩笑", scope="person:tg:jie")
    lin = await activate(k, "小林不喜欢被催", scope="person:tg:lin")
    group = await activate(k, "这个群里少发长消息", scope="channel:tg:g1")
    a = await k.growth.playbook_for("阿杰说工作压力好大", ["global", "person:tg:jie", "channel:tg:g1"], limit=6)
    b = await k.growth.playbook_for("阿杰在玩新游戏", ["global", "person:tg:jie", "channel:tg:g1"], limit=6)
    glob_a = [e.id for e in a if e.scope == "global"]
    glob_b = [e.id for e in b if e.scope == "global"]
    assert len(glob_a) == 3 and glob_a == glob_b == sorted(glob_a)          # cue-independent, stable order
    assert [e.id for e in a if e.scope != "global"][0] == jie_work.id
    assert [e.id for e in b if e.scope != "global"][0] == jie_game.id
    assert lin.id not in [e.id for e in a]
    assert group.id in [e.id for e in a]
    ta = k.growth.format_playbook(a, [])
    tb = k.growth.format_playbook(b, [])
    prefix_a, prefix_b = ta.split("【和这里")[0], tb.split("【和这里")[0]
    assert prefix_a == prefix_b and prefix_a.startswith("【我的做事习惯】")   # byte-stable cached prefix
    # without "global" in scopes, no global entries
    only = await k.growth.playbook_for("工作", ["person:tg:jie"])
    assert all(e.scope == "person:tg:jie" for e in only)
    assert len(g) == 4


async def test_expressions_and_jargon(k):
    s = await activate(k, "用叠词加一个表情", type_="expression", kind="style", situation="有人分享了好消息")
    j = await activate(k, "笑死我了", type_="expression", kind="jargon", situation="有人说「xswl」",
                       jargon={"term": "xswl", "meaning": "笑死我了"})
    other = await activate(k, "回一句“稳”", type_="expression", kind="style", situation="有人说自己考过了",
                           scope="channel:tg:other")
    got = await k.growth.expressions_for("xswl 我拿到 offer 了，好消息分享一下", ["channel:tg:g1"])
    assert got[0].id == j.id
    got = await k.growth.expressions_for("好消息！我升职了", ["channel:tg:g1"])
    assert [x.id for x in got] == [s.id]
    assert other.id not in [x.id for x in got]
    txt = k.growth.format_playbook([], [s, j])
    assert "当「有人分享了好消息」时，可以「用叠词加一个表情」" in txt and "xswl：笑死我了" in txt


def test_format_playbook_accepts_dicts():
    from alive.growth.system import Growth
    out = Growth.format_playbook(None, [{"id": "b", "text": "别刷屏", "scope": "global"},
                                        {"id": "a", "text": "先问感受", "scope": "global"},
                                        {"id": "c", "text": "多鼓励阿杰", "scope": "person:x"}],
                                 [{"kind": "jargon", "jargon": {"term": "yyds", "meaning": "永远的神"}}])
    lines = out.splitlines()
    assert lines[:3] == ["【我的做事习惯】", "- 先问感受", "- 别刷屏"]
    assert "- 多鼓励阿杰" in lines and "- yyds：永远的神" in lines
    assert Growth.format_playbook(None, [], []) == ""


async def test_global_prefix_stable_until_membership_changes(k):
    entries = [await activate(k, t) for t in ("别连发消息", "答应的事要记得", "不懂就问", "少用感叹号", "晚上少打扰别人")]
    before = [e.id for e in await k.growth.playbook_for("x", ["global"])]
    outside = [e for e in entries if e.id not in before][0]
    for _ in range(3):     # counters move during the day, but the cached prefix does not
        await k.growth.record_evidence(level="S2", polarity="helpful", about="t", entry_id=outside.id)
    assert [e.id for e in await k.growth.playbook_for("y", ["global"])] == before
    inside = before[0]
    for _ in range(2):     # an auto-demoted entry must leave the prompt immediately
        await k.growth.record_evidence(level="S2", polarity="harmful", about="t", entry_id=inside)
    after = [e.id for e in await k.growth.playbook_for("y", ["global"])]
    assert inside not in after and len(after) == 3


async def test_expressions_are_sampled_not_a_fixed_top_k(k):
    """The same cue does not always bring up the same phrases: a Plackett-Luce draw over cue similarity (temperature
    from the playfulness texture); the better match still comes up more often."""
    import random
    texts = ["分享好消息时用叠词", "听到好消息时说恭喜恭喜", "好消息就发个撒花", "别人分享好消息时追问细节"]
    arts = [await activate(k, t, type_="expression", kind="style", situation="有人分享了好消息") for t in texts]
    k.chance_rng = random.Random(4)
    seen, first = set(), {}
    for _ in range(60):
        got = await k.growth.expressions_for("好消息！我升职了，分享一下", ["channel:tg:g1"], limit=2)
        assert len(got) == 2 and len({a.id for a in got}) == 2
        seen.add(tuple(a.id for a in got))
        first[got[0].id] = first.get(got[0].id, 0) + 1
    assert len(seen) > 2                                       # varies between calls
    assert set(first) <= {a.id for a in arts}
