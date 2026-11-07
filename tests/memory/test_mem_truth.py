"""Truth maintenance (docs/03 §4.3): invalidation propagates `stale` along derived_from up to depth 3, and the
sleep reconcile stage re-evaluates stale records and conflicts (multi-hop knowledge update)."""
import pytest

from alive.core.types import Trust
from tests.memory.memhelp import kernel


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


async def chain(k):
    """L works at A -> L commutes through B -> recommend the B-district shop to L -> ... (depth 4)."""
    m = k.memory
    a = await m.note(kind="semantic", content="L 在 A 公司上班", source="told_by", told_by="tg:l", trust=Trust.CONTACT,
                     person_keys=["tg:l"])
    b = await m.note(kind="opinion", content="L 每天通勤经过 B 区", source="inferred", derived_from=[a],
                     person_keys=["tg:l"])
    c = await m.note(kind="opinion", content="可以推荐 B 区那家面包店给 L", source="inferred", derived_from=[b],
                     person_keys=["tg:l"])
    d = await m.note(kind="opinion", content="L 下班路上可以顺便买面包", source="inferred", derived_from=[c])
    e = await m.note(kind="opinion", content="周五可以约 L 一起去面包店", source="inferred", derived_from=[d])
    return a, b, c, d, e


async def test_dependency_propagation_depth3(k):
    a, b, c, d, e = await chain(k)
    other = await k.memory.note(kind="semantic", content="L 喜欢猫", source="told_by", told_by="tg:l",
                                trust=Trust.CONTACT)
    res = await k.memory.invalidate(a, reason="L 跳槽了")
    assert set(res["stale"]) == {b, c, d}
    st = {r["id"]: r["status"] for r in await k.db.fetchall("SELECT id, status FROM mem_records")}
    assert st[a] == "invalid" and st[b] == st[c] == st[d] == "stale"
    assert st[e] == "active" and st[other] == "active"      # depth 4 and unrelated untouched
    q = {r["record_id"]: r["depth"] for r in await k.db.fetchall("SELECT record_id, depth FROM mem_recheck")}
    assert q == {b: 1, c: 2, d: 3}
    # invalid records are excluded from standard retrieval, stale ones are penalised and tagged
    hits = await k.memory.retrieve("L 通勤 B 区 面包店")
    assert a not in [h.id for h in hits]
    assert all(h.status == "stale" for h in hits if h.id in (b, c, d))


async def test_propagation_follows_versions(k):
    m = k.memory
    a = await m.note(kind="semantic", content="L 住在海淀", source="told_by", told_by="tg:l", trust=Trust.CONTACT)
    dep = await m.note(kind="opinion", content="L 去公司很近", source="inferred", derived_from=[a])
    a2 = await m.note(kind="semantic", content="L 住在海淀五道口", source="told_by", told_by="tg:l",
                      trust=Trust.CONTACT, version=2, parent_version=a)
    await m.supersede(a, a2)
    res = await m.invalidate(a2, reason="搬家了")
    assert dep in res["stale"]


async def test_sleep_recheck_keep_revise_invalid(k):
    a, b, c, d, e = await chain(k)
    new = await k.memory.note(kind="semantic", content="L 跳槽到了 C 公司，在 D 区", source="told_by", told_by="tg:l",
                              trust=Trust.CONTACT)
    await k.memory.invalidate(a, reason="跳槽", replaced_by=new)
    seen = {}

    def recheck(req):
        txt = req.messages[0]["content"]
        seen["prompt"] = txt
        out = []
        for n, line in enumerate([l for l in txt.splitlines() if l[:1].isdigit()], start=1):
            if "通勤" in line:
                out.append({"item": n, "verdict": "revise", "content": "L 每天通勤经过 D 区"})
            elif "顺便买面包" in line:
                out.append({"item": n, "verdict": "invalid"})
            else:
                out.append({"item": n, "verdict": "keep"})
        return {"results": out}
    k.mock.on("mem.recheck", recheck)
    rep = await k.memory.consolidate("reconcile", {})
    assert rep["revised"] == 1 and rep["invalidated"] == 1 and rep["kept"] == 1
    assert "新情况是「L 跳槽到了 C 公司，在 D 区」" in seen["prompt"]
    assert k.mock.calls_for("mem.recheck")[0].tier == "deliberate"
    st = {r["id"]: r for r in await k.db.fetchall("SELECT * FROM mem_records")}
    assert st[b]["status"] == "superseded" and st[c]["status"] == "active" and st[d]["status"] == "invalid"
    revised = [r for r in st.values() if r["content"] == "L 每天通勤经过 D 区"][0]
    assert revised["parent_version"] == b and new in revised["derived_from"]
    # invalidating d during the recheck propagated one more hop: e is now stale and queued for next night
    assert st[e]["status"] == "stale"
    q = await k.db.fetchall("SELECT record_id, reason FROM mem_recheck")
    assert [(r["record_id"], r["reason"]) for r in q] == [(e, f"dependency:{d}")]


async def test_recheck_keep_is_not_applied_to_restaled_sibling(k):
    m = k.memory
    a = await m.note(kind="semantic", content="L 养了一只狗", source="told_by", told_by="tg:l", trust=Trust.CONTACT)
    b = await m.note(kind="opinion", content="L 每天早上遛狗", source="inferred", derived_from=[a])
    c = await m.note(kind="opinion", content="早上别找 L 聊天", source="inferred", derived_from=[a, b])
    await m.invalidate(a)
    k.mock.on("mem.recheck", lambda req: {"results": [
        {"item": n, "verdict": "keep" if "别找" in l else "invalid"}
        for n, l in enumerate([x for x in req.messages[0]["content"].splitlines() if x[:1].isdigit()], start=1)]})
    await m.consolidate("reconcile", {})
    assert (await m.get(b)).status == "invalid"
    assert (await m.get(c)).status == "stale"          # its other dependency just became invalid
    assert await k.db.fetchval("SELECT COUNT(*) FROM mem_recheck WHERE record_id=?", (c,)) == 1


async def test_recheck_garbage_keeps_queue_then_gives_up(k):
    a, b, *_ = await chain(k)
    await k.memory.invalidate(a)
    k.mock.on("mem.recheck", "???")
    for _ in range(3):
        await k.memory.consolidate("reconcile", {})
    assert await k.db.fetchval("SELECT COUNT(*) FROM mem_recheck") == 0
    assert (await k.memory.get(b)).status == "stale"       # never silently re-activated


async def test_conflict_resolution(k):
    m = k.memory
    a = await m.note(kind="semantic", content="阿杰的生日是5月3日", source="told_by", told_by="cli:owner",
                     trust=Trust.OWNER)
    b = await m.note(kind="semantic", content="阿杰的生日是6月1日", source="told_by", told_by="tg:x",
                     trust=Trust.PUBLIC)
    await m.mark_conflict(b, a)
    k.mock.on("mem.conflict", lambda req: {"results": [{"pair": 1, "winner": "b" if "5月3日" in
                                                        req.messages[0]["content"].split("b：")[1] else "a"}]})
    rep = await m.consolidate("reconcile", {})
    assert rep["conflicts_resolved"] == 1
    sa, sb = await m.get(a), await m.get(b)
    assert {sa.status, sb.status} == {"active", "invalid"}
    winner = sa if sa.status == "active" else sb
    assert winner.id == a and not winner.conflict
    # unknown -> kept with the conflict flag
    c = await m.note(kind="semantic", content="小林住在上海", source="told_by", told_by="tg:y", trust=Trust.CONTACT)
    d = await m.note(kind="semantic", content="小林住在杭州", source="told_by", told_by="tg:z", trust=Trust.CONTACT)
    await m.mark_conflict(c, d)
    k.mock.on("mem.conflict", {"results": [{"pair": 1, "winner": "unknown"}]})
    rep = await m.consolidate("reconcile", {})
    assert rep["conflicts_kept"] == 1 and (await m.get(c)).conflict and (await m.get(d)).status == "active"
