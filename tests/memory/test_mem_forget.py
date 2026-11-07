"""Forgetting (docs/03 §6–7): compression of weak low-importance episodes, 25% max-loss rewrite rule with
pre-images; privacy deletion (forget_person, docs/03 §8); sleep-time precompute."""
import json

import pytest

from alive.core.types import Trust
from tests.memory.memhelp import MONDAY, at, chat, kernel

DAY = 86400.0


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


async def test_compress_weak_low_importance_episodes(k):
    m = k.memory
    t = MONDAY - 40 * DAY
    weak = [await m.note(kind="episodic", content=f"和阿杰随便聊了几句天气{i}", importance=2, thread_id="tg:jie",
                         observed_at=t + i * 60) for i in range(3)]
    important = await m.note(kind="episodic", content="阿杰告诉我他要结婚了", importance=8, thread_id="tg:jie",
                             observed_at=t + 600)
    fresh = await m.note(kind="episodic", content="今天和阿杰吃了饭", importance=2, thread_id="tg:jie")
    pinned = await m.note(kind="episodic", content="和阿杰第一次见面", importance=2, thread_id="tg:jie",
                          observed_at=t, pinned=True)
    # make the old ones old in "last access" terms too
    await k.db.execute("UPDATE mem_records SET last_access=? WHERE id IN (?,?,?,?)", (t, *weak, important))
    k.mock.on("mem.compress", {"summary": "那天和阿杰闲聊了天气"})
    rep = await m.consolidate("forget", {"now": k.clock.now()})
    assert rep["compressed"] == 3 and rep["summaries"] == 1
    st = {r["id"]: r for r in await k.db.fetchall("SELECT * FROM mem_records")}
    assert all(st[i]["status"] == "compressed" for i in weak)          # raw records kept, not deleted
    assert st[important]["status"] == "active" and st[fresh]["status"] == "active" and st[pinned]["status"] == "pinned"
    summary = [r for r in st.values() if r["content"] == "那天和阿杰闲聊了天气"][0]
    meta = json.loads(summary["meta"])
    assert set(meta["summary_of"]) == set(weak) and summary["observed_at"] == t
    assert set(json.loads(summary["derived_from"])) == set(weak)
    # compressed originals are out of standard retrieval but still reachable by deep retrieval
    assert not set(weak) & {h.id for h in await m.retrieve("聊天气")}
    k.mock.on("mem.deep", {"enough": True})
    assert set(weak) & {h.id for h in await m.retrieve("聊天气", tier="deep")}


async def test_summary_rewrite_respects_max_loss_and_keeps_preimages(k):
    m = k.memory
    sid = await m.note(kind="episodic", content="和阿杰聊了天气。一起吐槽了加班。约好周末去爬山。", importance=3)
    # dropping 2 of 3 items (67%) is refused
    assert not await m.rewrite_summary(sid, "和阿杰聊了天气。")
    assert (await m.get(sid)).content.startswith("和阿杰聊了天气。一起吐槽了加班")
    # keeping everything and adding is fine; the pre-image is saved
    assert await m.rewrite_summary(sid, "和阿杰聊了天气。一起吐槽了加班。约好周末去爬山。还聊了新电影。")
    pre = await m.preimages(sid)
    assert len(pre) == 1 and pre[0]["content"] == "和阿杰聊了天气。一起吐槽了加班。约好周末去爬山。"
    assert "新电影" in (await m.get(sid)).content
    assert await m.restore_preimage(sid)
    assert (await m.get(sid)).content == "和阿杰聊了天气。一起吐槽了加班。约好周末去爬山。"
    # FTS follows rewrites
    assert sid in [h.id for h in await m.retrieve("爬山")]
    # pinned records are never auto-rewritten
    p = await m.note(kind="episodic", content="很重要的一天", pinned=True)
    assert not await m.rewrite_summary(p, "很重要的一天。补充一点。")


async def test_forget_stage_merges_into_existing_summary_with_guard(k):
    m = k.memory
    t = at(2026, 9, 1, 10)
    first = [await m.note(kind="episodic", content=f"小事{i}", importance=1, thread_id="tg:a", observed_at=t + i)
             for i in range(2)]
    await k.db.execute("UPDATE mem_records SET last_access=?", (t,))
    k.mock.on("mem.compress", {"summary": "那天做了几件小事。顺便买了菜。"})
    await m.consolidate("forget", {})
    later = [await m.note(kind="episodic", content=f"又一件小事{i}", importance=1, thread_id="tg:a",
                          observed_at=t + 100 + i) for i in range(2)]
    await k.db.execute(f"UPDATE mem_records SET last_access=? WHERE id IN (?,?)", (t, *later))
    k.mock.on("mem.compress", {"summary": "完全不同的内容"})            # would lose 100% of the old summary
    rep = await m.consolidate("forget", {})
    assert rep["rewrites_rejected"] == 1 and rep["summaries"] == 1   # appended a new summary instead
    k.mock.on("mem.compress", lambda req: {"summary": "那天做了几件小事。顺便买了菜。后来又忙了点别的。"})
    more = [await m.note(kind="episodic", content=f"第三批小事{i}", importance=1, thread_id="tg:a",
                         observed_at=t + 200 + i) for i in range(2)]
    await k.db.execute(f"UPDATE mem_records SET last_access=? WHERE id IN (?,?)", (t, *more))
    rep = await m.consolidate("forget", {})
    assert rep["rewrites"] == 1
    req = k.mock.calls_for("mem.compress")[-1]
    assert "已有的摘要" in req.messages[0]["content"]


async def test_forget_person_physical_delete_and_redaction(k):
    m = k.memory
    pk = "tg:jie"
    # an incoming message event from that person and one from someone else
    await k.bus.emit("percept.message", {"channel": "tg", "chat_id": "g1", "sender_id": "jie", "sender_name": "阿杰",
                                         "text": "我下周三面试"}, thread_id="tg:g1")
    await k.bus.emit("percept.message", {"channel": "tg", "chat_id": "g1", "sender_id": "lin", "sender_name": "小林",
                                         "text": "加油"}, thread_id="tg:g1")
    a = await m.note(kind="semantic", content="阿杰下周三面试", source="told_by", told_by=pk, trust=Trust.CONTACT,
                     person_keys=[pk], entities=["面试"])
    b = await m.note(kind="semantic", content="小林说阿杰在找工作", source="told_by", told_by="tg:lin",
                     trust=Trust.CONTACT, person_keys=[pk])
    c = await m.note(kind="opinion", content="他最近压力很大", source="inferred", derived_from=[a])
    keep = await m.note(kind="semantic", content="小林喜欢猫", source="told_by", told_by="tg:lin", trust=Trust.CONTACT,
                        person_keys=["tg:lin"])
    lines = chat(k, [(pk, "阿杰", "还在吗")], thread="tg:jie")
    await m.observe_conversation("tg:jie", lines, [pk])
    tomb = []

    async def h(ev):
        tomb.append(ev.payload)
    k.bus.subscribe("mem.forget", h)
    n = await m.forget_person(pk)
    await k.bus.drain()
    assert n == 3
    ids = {r["id"] for r in await k.db.fetchall("SELECT id FROM mem_records")}
    assert ids == {keep}
    for tbl, col in (("mem_vec", "id"), ("mem_ent", "record_id"), ("mem_deps", "record_id")):
        assert await k.db.fetchval(f"SELECT COUNT(*) FROM {tbl} WHERE {col} IN (?,?,?)", (a, b, c)) == 0
    assert await k.db.fetchval("SELECT COUNT(*) FROM mem_fts WHERE mem_fts MATCH '\"下周三\"'") == 0
    assert all(k.memory.index.get(i) is None for i in (a, b, c))
    evs = await k.bus.query(types=["percept.message"])
    texts = [e.payload.get("text") for e in evs]
    assert "我下周三面试" not in texts and "加油" in texts
    assert any(e.payload.get("redacted") for e in evs)
    assert tomb and tomb[0]["records"] == 3 and "jie" not in json.dumps(tomb[0])   # hashed key only
    assert k.memory.status()["pending_lines"] == 0
    assert await m.about_person(pk) == []


async def test_precompute_bundles_for_tomorrow(k):
    m = k.memory
    exam = await m.note(kind="semantic", content="小林明天下午有数学考试", source="told_by", told_by="tg:lin",
                        trust=Trust.CONTACT, person_keys=["tg:lin"])
    await m.note(kind="episodic", content="小林这几天一直在复习数学", person_keys=["tg:lin"])
    await m.note(kind="semantic", content="阿杰下个月搬家", source="told_by", told_by="tg:jie", trust=Trust.CONTACT)
    await k.motivation.add_intention({"goal": "问问阿杰面试结果", "level": "day", "kind": "outreach"})
    # sleeping at 23:30 on Monday -> tomorrow is Tuesday
    k.clock.set(at(2026, 11, 9, 23, 30))
    rep = await m.consolidate("precompute", {})
    assert rep["date"] == "2026-11-10" and rep["topics"] >= 2
    k.clock.set(at(2026, 11, 10, 8, 0))
    bundles = m.precomputed("tg:lin")
    assert bundles and bundles[0]["source_id"] == exam and exam in bundles[0]["hit_ids"]
    assert "考试" in bundles[0]["text"]
    assert any(b["kind"] == "open_loop" for b in m.precomputed())
    # stored in kv for other processes / restarts
    assert await k.db.kv_get("mem.precompute", "2026-11-10")
    # the fast tier uses the precomputed bundle as an extra route
    hits = await m.retrieve("最近怎么样", person_key="tg:lin", tier="fast")
    assert exam in [h.id for h in hits]


async def test_consolidate_unknown_stage(k):
    assert (await k.memory.consolidate("nope", {}))["error"] == "unknown stage"


async def test_forget_person_waits_for_inflight_extraction(k):
    import asyncio
    started = asyncio.Event()

    async def slow_extract(req):
        started.set()
        await asyncio.sleep(0.05)
        return {"episodes": [{"summary": "阿杰说他下周搬家", "importance": 4, "lines": [1, 1]}],
                "facts": [{"content": "阿杰下周搬家", "about": "p1", "told_by": "p1", "line": 1}]}
    k.mock.on("mem.extract", slow_extract)
    lines = chat(k, [("tg:jie", "阿杰", "我下周搬家")], thread="tg:jie")
    await k.memory.observe_conversation("tg:jie", lines, ["tg:jie"])
    flush = asyncio.ensure_future(k.memory.flush_all())
    await started.wait()
    n = await k.memory.forget_person("tg:jie")          # must not let the in-flight batch resurrect records
    await flush
    assert n >= 1
    assert await k.db.fetchval("SELECT COUNT(*) FROM mem_records") == 0
