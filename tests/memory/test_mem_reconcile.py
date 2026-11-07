"""observe_conversation: debounce, extraction, Mem0-style reconciliation (ADD/UPDATE/INVALIDATE/NOOP),
trust conflicts, claims about me, sensitive categories, my own statements."""
import pytest

from alive.core.types import Trust
from tests.memory.memhelp import MONDAY, chat, day_of, kernel

JIE = "tg:jie"
OWNER = "cli:owner"   # FakeGov: owner.person_keys -> Trust.OWNER, everyone else CONTACT


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


def extract_reply(facts=(), episodes=None):
    return {"episodes": episodes if episodes is not None else
            [{"summary": "和阿杰聊了聊他的工作", "importance": 5, "peak": False, "entities": ["工作"], "lines": [1, 3]}],
            "facts": list(facts)}


async def observe_and_flush(k, specs, thread="tg:jie", participants=(JIE,)):
    lines = chat(k, specs, thread=thread)
    await k.memory.observe_conversation(thread, lines, list(participants))
    await k.memory.flush_all()
    await k.memory.drain()
    return lines


async def test_debounce_idle_and_max_lines(k):
    k.mock.on("mem.extract", extract_reply())
    lines = chat(k, [(JIE, "阿杰", "在吗"), ("", "小屿", "在呀"), (JIE, "阿杰", "今天好累")])
    await k.memory.observe_conversation("tg:jie", lines, [JIE])
    await k.clock.advance(60)
    assert not k.mock.calls_for("mem.extract")          # still debouncing
    await k.clock.advance(600)                           # idle window passed -> one extraction
    assert len(k.mock.calls_for("mem.extract")) == 1
    req = k.mock.calls_for("mem.extract")[0]
    assert req.tier == "fast" and "p1 阿杰: 在吗" in req.messages[0]["content"]
    eps = await k.memory.episodes_between(MONDAY - 1, MONDAY + 10_000)
    assert len(eps) == 1 and eps[0].person_keys == [JIE] and eps[0].thread_id == "tg:jie"
    # max_lines triggers immediately, no idle wait
    many = chat(k, [(JIE, "阿杰", f"第{i}句") for i in range(12)], base=k.clock.now())
    await k.memory.observe_conversation("tg:jie", many, [JIE])
    await k.clock.advance(1)
    assert len(k.mock.calls_for("mem.extract")) == 2
    # duplicates (same msg ids) are ignored
    await k.memory.observe_conversation("tg:jie", many, [JIE])
    await k.clock.advance(700)
    assert len(k.mock.calls_for("mem.extract")) == 2


async def test_add_with_date_resolved_from_line_time(k):
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰下周三有一场面试", "kind": "semantic", "about": "p1",
                                             "told_by": "p1", "line": 1, "when": "下周三", "importance": 7}]))
    await observe_and_flush(k, [(JIE, "阿杰", "我下周三有个面试"), ("", "小屿", "加油！")])
    assert not k.mock.calls_for("mem.reconcile")        # nothing similar -> ADD without an LLM call
    facts = [h for h in await k.memory.about_person(JIE) if h.kind == "semantic"]
    assert len(facts) == 1
    f = facts[0]
    assert f.source == "told_by" and f.told_by == JIE and f.trust == int(Trust.CONTACT)
    assert day_of(f.refers_to["start"]) == "2026-11-18" and f.refers_to["raw"] == "下周三"


async def test_update_creates_new_version(k):
    old = await k.memory.note(kind="semantic", content="阿杰在学吉他", source="told_by", told_by=JIE,
                              trust=Trust.CONTACT, person_keys=[JIE])
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰在学吉他，已经学了三个月", "about": "p1", "told_by": "p1",
                                             "line": 1}]))
    k.mock.on("mem.reconcile", {"decisions": [{"fact": 1, "action": "UPDATE", "target": "r1",
                                               "content": "阿杰在学吉他，已经坚持三个月了"}]})
    await observe_and_flush(k, [(JIE, "阿杰", "吉他我已经学三个月啦")])
    req = k.mock.calls_for("mem.reconcile")[0]
    assert "r1. 阿杰在学吉他" in req.messages[0]["content"]
    o = await k.memory.get(old)
    assert o.status == "superseded"
    new = [h for h in await k.memory.about_person(JIE) if h.kind == "semantic" and h.status == "active"]
    assert len(new) == 1 and new[0].content == "阿杰在学吉他，已经坚持三个月了"
    assert new[0].version == 2
    row = await k.db.fetchone("SELECT parent_version, expired_at FROM mem_records WHERE id=?", (new[0].id,))
    assert row["parent_version"] == old
    assert (await k.db.fetchone("SELECT expired_at FROM mem_records WHERE id=?", (old,)))["expired_at"] is not None


async def test_invalidate_is_bitemporal(k):
    old = await k.memory.note(kind="semantic", content="阿杰在腾讯公司上班", source="told_by", told_by=JIE,
                              trust=Trust.CONTACT, person_keys=[JIE], observed_at=MONDAY - 30 * 86400)
    got = []

    async def h(ev):
        got.append(ev.payload)
    k.bus.subscribe("mem.invalidate", h)
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰已经从腾讯公司离职，现在在字节上班", "about": "p1",
                                             "told_by": "p1", "line": 1}]))
    k.mock.on("mem.reconcile", {"decisions": [{"fact": 1, "action": "INVALIDATE", "target": "r1", "reason": "换工作"}]})
    lines = await observe_and_flush(k, [(JIE, "阿杰", "我从腾讯离职啦，现在在字节")])
    await k.bus.drain()
    row = await k.db.fetchone("SELECT status, valid_to, expired_at, valid_from FROM mem_records WHERE id=?", (old,))
    assert row["status"] == "invalid"
    assert row["valid_to"] == lines[0].ts and row["expired_at"] == k.clock.now()   # valid time vs system time
    cur = [h for h in await k.memory.retrieve("阿杰在哪上班", person_key=JIE) if h.kind == "semantic"]
    assert cur[0].content.startswith("阿杰已经从腾讯公司离职")
    assert old not in [h.id for h in cur]
    assert got and got[0]["id"] == old


async def test_contradiction_with_little_word_overlap_is_found(k):
    """Regression from the live run: '已经从阿里离职，现在在腾讯' shares no trigram with '在阿里巴巴上班' and has a
    low hashing cosine; the same-person route + name-stripped bigram overlap must still surface it."""
    old = await k.memory.note(kind="semantic", content="阿杰在阿里巴巴上班", source="told_by", told_by=JIE,
                              trust=Trust.CONTACT, person_keys=[JIE])
    await k.memory.note(kind="semantic", content="小林在阿里巴巴上班", source="told_by", told_by="tg:lin",
                        trust=Trust.CONTACT, person_keys=["tg:lin"])
    k.memory.remember_name(JIE, "阿杰")
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰已经从阿里离职，现在在腾讯工作", "about": "p1",
                                             "told_by": "p1", "line": 1}]))
    k.mock.on("mem.reconcile", {"decisions": [{"fact": 1, "action": "INVALIDATE", "target": "r1"}]})
    await observe_and_flush(k, [(JIE, "阿杰", "我从阿里离职了，现在在腾讯")])
    req = k.mock.calls_for("mem.reconcile")
    assert req and "r1. 阿杰在阿里巴巴上班" in req[0].messages[0]["content"]
    assert (await k.memory.get(old)).status == "invalid"


async def test_lower_trust_does_not_overwrite(k):
    old = await k.memory.note(kind="semantic", content="阿杰的生日是5月3日", source="told_by", told_by=OWNER,
                              trust=Trust.OWNER, person_keys=[JIE])
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰的生日是6月1日", "about": "p2", "told_by": "p1",
                                             "line": 1}]))
    k.mock.on("mem.reconcile", {"decisions": [{"fact": 1, "action": "INVALIDATE", "target": "r1"}]})
    await observe_and_flush(k, [("tg:someone", "路人", "阿杰生日是6月1日吧"), (JIE, "阿杰", "嗯？")], thread="tg:g1",
                            participants=("tg:someone", JIE))
    o = await k.memory.get(old)
    assert o.status == "active" and o.conflict           # kept, marked conflicting
    new = [h for h in await k.memory.about_person(JIE) if "6月1日" in h.content][0]
    assert new.status == "active" and new.conflict and new.trust == int(Trust.CONTACT)
    q = await k.db.fetchall("SELECT record_id, reason FROM mem_recheck ORDER BY record_id")
    assert {r["record_id"] for r in q} == {old, new.id} and all(r["reason"].startswith("conflict:") for r in q)


async def test_noop_and_garbage_reconcile(k):
    old = await k.memory.note(kind="semantic", content="阿杰喜欢喝冰美式", source="told_by", told_by=JIE,
                              trust=Trust.CONTACT, person_keys=[JIE])
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰喜欢喝冰美式咖啡", "about": "p1", "told_by": "p1",
                                             "line": 1}]))
    k.mock.on("mem.reconcile", {"decisions": [{"fact": 1, "action": "NOOP", "target": "r1"}]})
    await observe_and_flush(k, [(JIE, "阿杰", "冰美式yyds")])
    sem = [h for h in await k.memory.about_person(JIE) if h.kind == "semantic"]
    assert [h.id for h in sem] == [old]
    # unparsable reconcile output -> ADD (never lose the fact)
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰喜欢喝冰美式，不加糖", "about": "p1", "told_by": "p1",
                                             "line": 1}]))
    k.mock.on("mem.reconcile", "我觉得应该更新吧")
    await observe_and_flush(k, [(JIE, "阿杰", "不加糖的那种")])
    sem = [h for h in await k.memory.about_person(JIE) if h.kind == "semantic"]
    assert len(sem) == 2


async def test_claims_about_me_sensitive_and_my_statements(k):
    k.mock.on("mem.extract", extract_reply([
        {"content": "阿杰说我上周答应过请他吃饭", "kind": "self_claim", "about": "me", "told_by": "p1", "line": 1},
        {"content": "阿杰最近在吃抗抑郁的药", "about": "p1", "told_by": "p1", "line": 2, "sensitive": True},
        {"content": "阿杰确诊了抑郁症", "about": "p1", "told_by": "p1", "line": 2},     # keyword backstop
        {"content": "我明天要去看展", "about": "me", "told_by": "me", "line": 3},
    ]))
    await observe_and_flush(k, [(JIE, "阿杰", "你上周答应过请我吃饭的"), (JIE, "阿杰", "最近在吃药"),
                                ("", "小屿", "我明天要去看展")])
    rows = await k.db.fetchall("SELECT kind, status, source, content FROM mem_records ORDER BY recorded_at")
    claims = [r for r in rows if r["kind"] == "self"]
    assert len(claims) == 1 and claims[0]["status"] == "pending" and claims[0]["source"] == "told_by"
    assert not any("抑郁" in r["content"] for r in rows)
    mine = [r for r in rows if r["content"].startswith("我说过")]
    assert len(mine) == 1 and mine[0]["kind"] == "episodic"
    assert not k.mock.calls_for("mem.reconcile")
    assert k.memory.counters["facts_skipped_sensitive"] == 2
    # explicitly asked to remember -> kept
    k.mock.on("mem.extract", extract_reply([{"content": "阿杰对花生过敏", "about": "p1", "told_by": "p1", "line": 1,
                                             "sensitive": True, "explicit_remember": True}]))
    await observe_and_flush(k, [(JIE, "阿杰", "记住哦我对花生过敏")])
    assert any("花生" in h.content for h in await k.memory.about_person(JIE))


async def test_extraction_failure_retries_then_drops(k):
    calls = {"n": 0}

    def boom(req):
        calls["n"] += 1
        raise RuntimeError("provider down")
    k.mock.on("mem.extract", boom)
    lines = chat(k, [(JIE, "阿杰", "hello")])
    await k.memory.observe_conversation("tg:jie", lines, [JIE])
    await k.clock.advance(601)
    assert calls["n"] == 1 and k.memory.status()["pending_lines"] == 1   # kept for retry
    await k.clock.advance(601)
    await k.clock.advance(601)
    assert calls["n"] == 3 and k.memory.status()["pending_lines"] == 0   # dropped after max_failures


async def test_buffers_survive_restart(tmp_path):
    k = await kernel(tmp_path)
    lines = chat(k, [(JIE, "阿杰", "我下个月搬家")])
    await k.memory.observe_conversation("tg:jie", lines, [JIE])
    await k.stop()
    k2 = await kernel(tmp_path)
    try:
        assert k2.memory.status()["pending_lines"] == 1
        k2.mock.on("mem.extract", extract_reply([{"content": "阿杰下个月搬家", "about": "p1", "told_by": "p1", "line": 1,
                                                  "when": "下个月"}]))
        rep = await k2.memory.consolidate("extract", {})
        assert rep["lines"] == 1
        assert any("搬家" in h.content for h in await k2.memory.about_person(JIE))
    finally:
        await k2.stop()
