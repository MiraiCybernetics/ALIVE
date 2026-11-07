"""L2 artifacts: unified lifecycle, append-only versions (INV-9), rollback, deterministic merge, auto-demote,
capacity caps, dedupe, privacy."""
import json
import sqlite3

import pytest

from tests.growth.growhelp import kernel


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


async def active_entry(k, text="聊工作时先问感受再给建议", scope="global"):
    a = await k.growth.propose_entry(text, scope=scope)
    await k.growth.artifacts.set_status(a.id, "quarantined")
    return await k.growth.artifacts.set_status(a.id, "active", eval_score=0.8)


async def test_lifecycle_versions_are_append_only(k):
    a = await active_entry(k)
    assert a.status == "active" and a.version == 3
    hist = await k.growth.entry_history(a.id)
    assert [h["change"] for h in hist] == ["create", "quarantine", "promote"]
    assert hist[0]["parent_vid"] is None
    assert [h["parent_vid"] for h in hist[1:]] == [hist[0]["vid"], hist[1]["vid"]]
    assert hist[2]["eval_score"] == 0.8 and a.head_vid == hist[2]["vid"]
    # INV-9: the version table cannot be rewritten or deleted
    with pytest.raises(sqlite3.IntegrityError):
        await k.db.execute("UPDATE grow_versions SET reason='x' WHERE vid=?", (hist[0]["vid"],))
    with pytest.raises(sqlite3.IntegrityError):
        await k.db.execute("DELETE FROM grow_versions")
    # candidate entries never reach prompts; active ones do
    c = await k.growth.propose_entry("另一条候选")
    out = await k.growth.playbook_for("工作", ["global"])
    assert [e.id for e in out] == [a.id] and c.id not in [e.id for e in out]


async def test_auto_demote_when_harmful(k):
    a = await active_entry(k)
    await k.growth.record_evidence(level="S4", polarity="harmful", about="x", entry_id=a.id)   # S4 does not count
    await k.growth.record_evidence(level="S2", polarity="harmful", about="x", entry_id=a.id)
    assert (await k.growth.get_entry(a.id)).status == "active"          # harmful 1 < threshold 2
    await k.growth.record_evidence(level="S3", polarity="harmful", about="x", entry_id=a.id, subject="tg:a")
    e = await k.growth.get_entry(a.id)
    assert (e.status, e.helpful, e.harmful) == ("quarantined", 0, 2)
    assert e.meta.get("needs_review") or "auto-demote" in e.meta.get("status_reason", "")
    assert a.id not in [x.id for x in await k.growth.playbook_for("工作", ["global"])]
    # helpful majority keeps it active
    b = await active_entry(k, "别在深夜连发很多消息")
    for _ in range(3):
        await k.growth.record_evidence(level="S2", polarity="helpful", about="y", entry_id=b.id)
    for _ in range(2):
        await k.growth.record_evidence(level="S2", polarity="harmful", about="y", entry_id=b.id)
    assert (await k.growth.get_entry(b.id)).status == "active"


async def test_rollback_is_a_pointer_switch(k):
    a = await active_entry(k)
    promote_vid = a.head_vid
    await k.growth.edit_entry(a.id, "聊工作时先问对方感受，再给建议，别急着讲道理")
    e = await k.growth.get_entry(a.id)
    assert e.version == 4 and "讲道理" in e.text
    n_before = len(await k.growth.entry_history(a.id))
    r = await k.growth.rollback(a.id, promote_vid)
    assert r.text == "聊工作时先问感受再给建议" and r.head_vid == promote_vid and r.version == 3
    assert len(await k.growth.entry_history(a.id)) == n_before               # history untouched
    assert (await k.growth.playbook_for("工作", ["global"]))[0].text == "聊工作时先问感受再给建议"
    # the next change branches from the rolled-back version
    await k.growth.edit_entry(a.id, "聊工作时先问感受")
    hist = await k.growth.entry_history(a.id)
    assert hist[-1]["parent_vid"] == promote_vid and hist[-1]["version"] == 5


async def test_deterministic_merge(k):
    a = await active_entry(k, "和人吵架后先冷静一下")
    b = await active_entry(k, "和人吵架以后先冷静一下再说")
    await k.growth.record_evidence(level="S2", polarity="helpful", about="calm", entry_id=a.id)
    await k.growth.record_evidence(level="S2", polarity="helpful", about="calm", entry_id=b.id)
    await k.growth.record_evidence(level="S3", polarity="harmful", about="calm", entry_id=b.id, subject="tg:x")
    b_head = (await k.growth.get_entry(b.id)).head_vid
    m = await k.growth.merge_entries(a.id, b.id)
    assert (m.helpful, m.harmful) == (2, 1)
    assert set(m.provenance) >= set((await k.growth.get_entry(b.id)).provenance)
    hist = await k.growth.entry_history(a.id)
    assert hist[-1]["change"] == "merge" and hist[-1]["merged_vid"] == b_head
    bb = await k.growth.get_entry(b.id)
    assert bb.status == "retired" and bb.meta["merged_into"] == a.id


async def test_merge_duplicates_at_night(k):
    a = await active_entry(k, "早上不要给人发长消息")
    b = await k.growth.propose_entry("早上不要给别人发长消息")
    await k.growth.artifacts.set_status(b.id, "quarantined")
    c = await k.growth.propose_entry("晚上不要给人发长消息")       # lexically close, different meaning
    await k.growth.artifacts.set_status(c.id, "quarantined")

    def judge(req):
        out = []
        txt = req.messages[0]["content"]
        for n in range(1, txt.count("a：") + 1):
            seg = txt.split(f"{n}. a：")[1].splitlines()[:2]
            same = not any("晚上" in x for x in seg)
            out.append({"pair": n, "same": same, "text": "早上别给人发长消息" if same else ""})
        return {"results": out}
    k.mock.on("grow.merge", judge)
    assert await k.growth.merge_duplicates() == 1
    assert (await k.growth.get_entry(b.id)).status == "retired"
    s = await k.growth.get_entry(a.id)
    assert s.status == "active" and s.text == "早上别给人发长消息"       # LLM proposed the text only
    assert (await k.growth.get_entry(c.id)).status == "quarantined"


async def test_capacity_caps(tmp_path):
    k = await kernel(tmp_path, cfg={"growth": {"caps": {"global": 3, "person": 1}}})
    try:
        ids = []
        for i in range(4):
            a = await active_entry(k, f"第{i}条全局习惯")
            if i == 0:
                await k.growth.record_evidence(level="S2", polarity="helpful", about="t", entry_id=a.id)
            ids.append(a.id)
            await k.growth.artifacts.enforce_caps("playbook", "global")
        act = {a.id for a in k.growth.artifacts.active("playbook", ["global"])}
        assert len(act) == 3 and ids[0] in act        # the one with helpful evidence survives
        p1 = await active_entry(k, "和阿杰说话直接一点", scope="person:tg:jie")
        p2 = await active_entry(k, "和阿杰聊天多用表情", scope="person:tg:jie")
        await k.growth.artifacts.enforce_caps("playbook", "person:tg:jie")
        assert len(k.growth.artifacts.active("playbook", ["person:tg:jie"])) == 1
        assert {p1.id, p2.id} & {a.id for a in k.growth.artifacts.active("playbook", ["person:tg:jie"])}
    finally:
        await k.stop()


async def test_dedupe_counts_observations(k):
    a, created = await k.growth.artifacts.create("expression", "使用“对对对”", kind="style", situation="表示讽刺的赞同",
                                                 scope="channel:tg:g1", examples=["对对对你说得都对"],
                                                 speakers=["tg:a"])
    b, created2 = await k.growth.artifacts.create("expression", "使用 “对对对”", kind="style",
                                                  situation="表示讽刺的赞同。", scope="channel:tg:g1",
                                                  examples=["对对对对"], speakers=["tg:b"])
    assert created and not created2 and a.id == b.id
    e = await k.growth.get_entry(a.id)
    assert e.seen_count == 2 and e.examples == ["对对对你说得都对", "对对对对"]
    assert e.meta["example_by"] == ["tg:a", "tg:b"]
    # examples / speakers are never part of version snapshots
    for h in await k.growth.entry_history(a.id):
        assert "examples" not in h["snapshot"] and "speakers" not in h["snapshot"]["meta"]


async def test_forget_person_in_growth(k):
    a, _ = await k.growth.artifacts.create("expression", "用叠词", kind="style", situation="撒娇的时候",
                                           scope="channel:tg:g1", examples=["好嘛好嘛", "去嘛去嘛"],
                                           speakers=["tg:a"])
    await k.growth.artifacts.observe_again(a, examples=["走嘛走嘛"], speakers=["tg:b"])
    p = await active_entry(k, "和小A说话要温柔", scope="person:tg:a")
    await k.growth.record_evidence(level="S3", polarity="helpful", about="gentle", subject="tg:a")
    rep = await k.growth.forget_person("tg:a")
    assert rep == {"examples": 1, "evidence": 1, "artifacts": 1}
    e = await k.growth.get_entry(a.id)
    assert e.examples == ["走嘛走嘛"] and "tg:a" not in e.meta["speakers"]
    assert await k.growth.get_entry(p.id) is None
    snaps = await k.db.fetchall("SELECT snapshot, reason FROM grow_versions WHERE artifact_id=?", (p.id,))
    assert snaps and all(json.loads(r["snapshot"]) == {"redacted": True} for r in snaps)
    assert p.id not in [x.id for x in await k.growth.playbook_for("", ["global", "person:tg:a"])]


async def test_forget_tombstone_from_memory_reaches_growth(tmp_path):
    k = await kernel(tmp_path, real=("memory", "growth"))
    try:
        await active_entry(k, "和小A说话要温柔", scope="person:tg:a")
        await k.memory.forget_person("tg:a")
        await k.bus.drain()
        assert await k.growth.artifacts.list(scope="person:tg:a") == []
    finally:
        await k.stop()
