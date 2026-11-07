"""What reaches a composition is per person and per audience (INV-34, docs/17 §3, docs/22 §1):
- memories and facts only if everyone who would hear me may hear them;
- a System-1 read belongs to the person it is about and to its own batch;
- every relevant person present gets a line in "在场的人";
- whether I stop to read carefully is a draw from a smooth, energy-modulated probability."""
import asyncio
import random

import pytest

from alive.core.types import ChatLine, ReplyTask, Trust
from alive.core.understanding import deep_read_probability
from alive.testing import make_test_kernel


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm", "memory"])
    yield kern
    await kern.stop()


def world(k):
    A = k.world.person("sim", "a", "阿明")
    B = k.world.person("sim", "b", "小白")
    G = k.world.group("sim", "g1", "大群")
    for p in (A, B):
        k.world.note_member(G, p, k.clock.now())
    k.social.names.update({A: "阿明", B: "小白"})
    return A, B, G


def task(k, space, lines, chat_type="private", title=""):
    """lines: [(pk, name, text)]"""
    now = k.clock.now()
    nl = [ChatLine(sender=n, person_key=pk, text=t, ts=now + i, msg_id=f"{space}:{now}:{i}")
          for i, (pk, n, t) in enumerate(lines)]
    return ReplyTask(thread_id=space, channel="sim", chat_type=chat_type, chat_title=title,
                     participants=list(dict.fromkeys(pk for pk, _, _ in lines)), history=[], new_lines=nl,
                     trust=Trust.CONTACT, mentioned=chat_type == "group")


def last_prompt(k):
    return k.mock.calls_for("s2.reply")[-1].messages[0]["content"]


async def test_Bs_private_disclosure_about_A_never_reaches_A_or_the_group(k):
    A, B, G = world(k)
    await k.memory.note(kind="semantic", content="阿明偷偷在准备跳槽去深圳，还没告诉任何人", source="told_by", told_by=B,
                        trust=Trust.CONTACT, person_keys=[A], thread_id=B, observed_at=k.clock.now() - 86400)
    k.mock.on("s2.reply", {"reply": ["哈哈"]})
    res = await k.mind.compose_reply(task(k, A, [(A, "阿明", "最近在想换个城市生活，深圳跳槽怎么样")]))
    assert "跳槽去深圳，还没告诉" not in last_prompt(k)
    assert any("跳槽去深圳" in w for w in res.withheld)               # handed to the outbound leak check
    assert any("跳槽去深圳" in w for w in k.mind.withheld_for(A))
    await k.mind.compose_reply(task(k, G, [(A, "阿明", "@小屿 你觉得深圳跳槽好吗")], chat_type="group", title="大群"))
    assert "跳槽去深圳，还没告诉" not in last_prompt(k)
    # with B, who told me, one-to-one: I may use it (and that context holds private data: rule of two)
    res = await k.mind.compose_reply(task(k, B, [(B, "小白", "阿明跳槽去深圳的事你别说出去哈")]))
    assert "跳槽去深圳，还没告诉" in last_prompt(k)
    assert res.private_used is True


async def test_Bs_crisis_does_not_colour_my_reply_to_A_and_follows_B(k):
    A, B, G = world(k)

    async def crisis_check(text, person_key, **kw):
        if "安眠药" in text or "不想活" in text:
            return {"level": "crisis", "kind": "suicide", "resources": "希望24小时热线 400-161-9995"}
        return None
    k.gov.crisis_check = crisis_check
    k.mock.on("s1.read", {"sentiment": 0.0})
    k.mock.on("s2.reply", {"reply": ["嗯"]})
    tb = task(k, G, [(B, "小白", "我已经准备好安眠药了")], chat_type="group", title="大群")
    await k.mind.quick_read(tb)
    # A speaks in the same group right after: the read and the composition run together (as social runs them)
    ta = task(k, G, [(A, "阿明", "@小屿 晚上吃啥")], chat_type="group", title="大群")
    await asyncio.gather(k.mind.quick_read(ta), k.mind.compose_reply(ta))
    assert "危机" not in last_prompt(k) and "400-161" not in last_prompt(k)
    # this batch's own read is what composition uses, even while it is still in flight
    ta2 = task(k, G, [(A, "阿明", "其实我也不想活了")], chat_type="group", title="大群")
    await asyncio.gather(k.mind.quick_read(ta2), k.mind.compose_reply(ta2))
    assert "危机" in last_prompt(k)


async def test_crisis_from_governance_follows_the_person_into_a_dm(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm", "gov"])
    try:
        A, B, G = world(kern)
        kern.mock.on("s2.reply", {"reply": ["嗯"]})
        kern.gov.crisis_scan("我已经准备好安眠药了", B, space=G)        # said in the group (inbound auto-scan)
        await kern.mind.compose_reply(task(kern, B, [(B, "小白", "在吗")]))
        p = last_prompt(kern)
        assert "危机" in p                                              # B was in G: same audience
        await kern.mind.compose_reply(task(kern, A, [(A, "阿明", "在吗")]))
        assert "危机" not in last_prompt(kern)                           # not A's
    finally:
        await kern.stop()


async def test_everyone_relevant_who_is_here_appears_in_the_context(k):
    A, B, G = world(k)
    k.mock.on("s2.reply", {"reply": ["好呀"]})
    t = task(k, G, [(B, "小白", "周末去爬山吧"), (A, "阿明", "@小屿 你去吗")], chat_type="group", title="大群")
    await k.mind.compose_reply(t)
    p = last_prompt(k)
    block = p.split("## 在场的人", 1)[1].split("##", 1)[0]
    assert "阿明" in block and "小白" in block
    assert "我此刻主要在回应 TA" in block.split("阿明", 1)[1].split("\n", 1)[0]
    assert "## 关于阿明" in p                                           # the same person key throughout
    # a shared space without fresh lines has no single focus person (not an arbitrary participants[0])
    t2 = ReplyTask(thread_id=G, channel="sim", chat_type="group", chat_title="大群", participants=[B, A],
                   history=list(t.new_lines), new_lines=[], trust=Trust.CONTACT)
    await k.mind.compose_reply(t2)
    p = last_prompt(k)
    assert "## 关于这里的人" in p and "## 关于小白" not in p


def test_deep_read_probability_is_smooth_and_energy_modulated():
    base = dict(sentiment=-0.5, venting=True, closeness=0.5)
    ps = [deep_read_probability(**{**base, "sentiment": -x / 20})[0] for x in range(0, 21)]
    assert all(b >= a for a, b in zip(ps, ps[1:]))                    # monotone in negativity
    assert max(b - a for a, b in zip(ps, ps[1:])) < 0.15              # no step: a smooth curve
    tired, rested = (deep_read_probability(**base, energy=e)[0] for e in (0.1, 0.9))
    assert tired < rested
    assert deep_read_probability(crisis=True, energy=0.0)[0] == 1.0  # safety: a possible crisis is always read
    neutral = deep_read_probability()[0]
    strong = deep_read_probability(sentiment=-0.7, venting=True, bad_news=True, closeness=0.8)[0]
    assert neutral < 0.1 < 0.9 < strong
    rng = random.Random(3)
    mid = deep_read_probability(**base)[0]
    hits = sum(rng.random() < mid for _ in range(4000)) / 4000
    assert abs(hits - mid) < 0.03 and 0.2 < mid < 0.8                # genuinely a draw in the middle range


async def test_baseline_and_open_topics_come_from_the_person_in_any_space(k):
    A, B, G = world(k)
    now = k.clock.now()
    grp = [ChatLine(sender="阿明", person_key=A, text=f"哈哈哈今天又加班到很晚 {i}", ts=now - 7200 + i * 120,
                    msg_id=f"g{i}") for i in range(12)]
    dm = [ChatLine(sender="阿明", person_key=A, text=f"哈哈哈我下周三要去面试 {i}", ts=now - 3600 + i * 60,
                   msg_id=f"d{i}") for i in range(8)]

    async def person_lines(pk, limit=300):
        return {G: grp, A: dm} if pk == A else {}
    k.social.person_lines = person_lines
    u = k.mind.understanding
    b = await u.baseline(A, G)
    assert b.n == 20 and b.laugh_rate > 0.9                           # one person, one baseline
    from alive.world.audience import Scope
    in_group = await u.person_history(A, task(k, G, [(A, "阿明", "嗯")], chat_type="group"), Scope(k.world, G))
    in_dm = await u.person_history(A, task(k, A, [(A, "阿明", "嗯")]), Scope(k.world, A))
    assert not any("面试" in l.text for l in in_group)                 # what he told me one-to-one stays there
    assert any("加班" in l.text for l in in_dm) and any("面试" in l.text for l in in_dm)


async def test_an_intention_learnt_one_to_one_stays_out_of_the_group(k):
    """'关心一下阿明（面试）' came from his DM: in the group (others present) it is not on my mind's surface, nor in
    my own-agenda topics; in his DM it is."""
    from alive.core.workspace import WorkspaceItem

    A, B, G = world(k)

    class It:
        def __init__(self, origins):
            self.meta = {"origins": origins}
            self.goal = "关心一下阿明（面试）"
    now = k.clock.now()
    k.mind.ws.offer(WorkspaceItem(key="intent:x", kind="intention", text="关心一下阿明（面试）", salience=0.9,
                                  ttl=7200, ref=It([A])), now)
    k.mind.ws.offer(WorkspaceItem(key="intent:y", kind="intention", text="想把那本书读完", salience=0.8, ttl=7200,
                                  ref=It([])), now)
    from alive.world.audience import Scope
    grp = task(k, G, [(A, "阿明", "@小屿 在吗"), (B, "小白", "今晚吃啥")], chat_type="group", title="大群")
    dm = task(k, A, [(A, "阿明", "在吗")])
    in_group = k.mind._workspace_text(grp, Scope(k.world, G, [A, B]))
    in_dm = k.mind._workspace_text(dm, Scope(k.world, A, [A]))
    assert "面试" not in in_group and "读完" in in_group
    assert "面试" in in_dm
    items_g, _ = await k.mind.dialogue.own_agenda(grp, A, "在吗")
    items_d, _ = await k.mind.dialogue.own_agenda(dm, A, "在吗")
    assert not any("面试" in x for x in items_g) and any("面试" in x for x in items_d)
