"""Write path: INV-11 / INV-14 provenance rules, provenance tags, events, bulk writes, seeds, pinning."""
import os

import pytest

from alive.core.types import Trust
from alive.memory.records import (PINNED_STRENGTH, apply_provenance_rules, initial_strength, recall_strength,
                                  retrievability)
from tests.memory.memhelp import MONDAY, kernel

DAY = 86400.0


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


@pytest.mark.parametrize("kind,source,trust,expected", [
    ("semantic", "dreamed", Trust.SYSTEM, "episodic"),
    ("semantic", "seeded", Trust.SYSTEM, "episodic"),
    ("semantic", "monologue", Trust.SYSTEM, "opinion"),
    ("relational", "dreamed", Trust.SYSTEM, "episodic"),
    ("self", "monologue", Trust.SYSTEM, "opinion"),
    ("semantic", "inferred", Trust.SYSTEM, "opinion"),
    ("semantic", "reflected", Trust.SYSTEM, "opinion"),
    ("episodic", "reflected", Trust.SYSTEM, "opinion"),
    ("relational", "inferred", Trust.OWNER, "opinion"),
    ("semantic", "read_from:https://x.example", Trust.UNTRUSTED, "episodic"),
    ("semantic", "told_by:tg:stranger", Trust.UNTRUSTED, "episodic"),
    ("self", "told_by:tg:stranger", Trust.UNTRUSTED, "episodic"),
    ("semantic", "experienced", Trust.SYSTEM, "semantic"),
    ("semantic", "told_by:tg:jie", Trust.CONTACT, "semantic"),
    ("semantic", "told_by:tg:groupmate", Trust.PUBLIC, "semantic"),
    ("opinion", "reflected", Trust.SYSTEM, "opinion"),
])
async def test_inv11_inv14_downgrade(k, kind, source, trust, expected):
    rid = await k.memory.note(kind=kind, content="一条测试记忆内容", source=source, trust=trust)
    h = await k.memory.get(rid)
    assert h.kind == expected
    # provenance is kept, never rewritten
    assert h.source == source.split(":", 1)[0]
    if expected != kind:
        assert h.meta["requested_kind"] == kind


def test_provenance_rules_pure():
    p = apply_provenance_rules("semantic", "told_by:tg:a", None, Trust.CONTACT)
    assert (p.kind, p.source, p.told_by) == ("semantic", "told_by", "tg:a")
    p = apply_provenance_rules("self", "told_by", "tg:a", Trust.CONTACT)
    assert p.kind == "self" and p.status == "pending"     # claims about me await verification
    p = apply_provenance_rules("nonsense", "whatever", None, 2)
    assert (p.kind, p.source) == ("episodic", "experienced")
    p = apply_provenance_rules("semantic", "told_by:tg:a", None, Trust.PUBLIC, semantic_min_trust=int(Trust.CONTACT))
    assert p.kind == "episodic"


async def test_write_event_and_index(k):
    got = []

    async def h(ev):
        got.append(ev)
    k.bus.subscribe("mem.write", h)
    rid = await k.memory.note(kind="semantic", content="我梦见下雨", source="dreamed")
    await k.bus.drain()
    assert got and got[0].payload["id"] == rid and got[0].payload["downgraded_from"] == "semantic"
    assert "content" not in got[0].payload  # small payload, no content
    assert k.memory.index.get(rid) is not None
    assert await k.db.fetchval("SELECT COUNT(*) FROM mem_fts WHERE mem_fts MATCH '\"我梦见\"'") == 1


async def test_provenance_tags(k):
    m = k.memory
    m.remember_name("tg:l", "L")
    now = k.clock.now()
    a = await m.note(kind="semantic", content="L 换了新工作", source="told_by", told_by="tg:l", trust=Trust.CONTACT,
                     observed_at=now - 21 * DAY)
    b = await m.note(kind="opinion", content="L 最近压力好像很大", source="inferred")
    c = await m.note(kind="episodic", content="网上说明天会降温", source="read_from:https://x", trust=Trust.UNTRUSTED)
    d = await m.note(kind="episodic", content="和 L 一起看了电影", observed_at=now - DAY)
    e = await m.note(kind="self", content="L 说我上次答应过请她吃饭", source="told_by", told_by="tg:l",
                     trust=Trust.CONTACT)
    hits = await m.get_many([a, b, c, d, e])
    txt = m.format_hits(hits, now)
    lines = txt.splitlines()
    assert lines[0].endswith("（L 说的，3 周前）")
    assert lines[1].endswith("（我猜的）")
    assert "（读到的，不一定可靠）" in lines[2]
    assert lines[3].endswith("（我亲历的，昨天）")
    assert "还没核实" in lines[4]
    assert (await m.get(e)).status == "pending"


async def test_note_many_bulk(k):
    ids = await k.memory.note_many([{"kind": "semantic", "content": f"第{i}条批量记忆", "importance": 3}
                                    for i in range(50)] + [{"kind": "semantic", "content": ""}])
    assert len(ids) == 50
    assert (await k.memory.stats())["total"] == 50
    assert len(k.memory.index) == 50


def test_strength_model():
    s_epi = initial_strength("episodic", 3, 0, 0, False)
    s_sem = initial_strength("semantic", 3, 0, 0, False)
    assert s_sem == pytest.approx(s_epi * 10)
    assert initial_strength("episodic", 3, -0.8, 0.9, True) > 2 * s_epi   # emotional peak lasts longer
    assert initial_strength("episodic", 9, 0, 0, False) > s_epi
    assert initial_strength("episodic", 3, 0, 0, False, pinned=True) == PINNED_STRENGTH
    assert retrievability(100.0, 100.0, s_epi) == 1.0
    assert retrievability(100.0 + s_epi, 100.0, s_epi) == pytest.approx(0.3679, abs=1e-3)
    # spacing bonus: a recall after a long gap strengthens more than an immediate one
    soon = recall_strength(s_epi, 0.0, 60.0)
    late = recall_strength(s_epi, 0.0, 5 * s_epi)
    assert s_epi < soon < late <= s_epi * 1.5 + 1


async def test_pin(k):
    rid = await k.memory.note(kind="episodic", content="第一次见到 L 的那天")
    assert await k.memory.pin(rid)
    h = await k.memory.get(rid)
    assert h.status == "pinned" and h.strength == PINNED_STRENGTH
    assert await k.memory.pin(rid, False)
    assert (await k.memory.get(rid)).status == "active"


async def test_seed_memories_loaded_once_as_seeded(tmp_path):
    persona = tmp_path / "persona"
    (persona / "seed_memories").mkdir(parents=True)
    (persona / "seed_memories" / "childhood.md").write_text("# 童年\n- 小时候在海边捡贝壳\n- 第一次学会骑自行车\n",
                                                            encoding="utf-8")
    (persona / "seed_memories" / "facts.yaml").write_text(
        "- {content: 我最喜欢的颜色是蓝色, kind: semantic, importance: 5}\n", encoding="utf-8")
    from alive.runtime.clock import ManualClock
    from alive.testing import make_test_kernel
    k = await make_test_kernel(tmp_path / "data", real=["memory"], clock=ManualClock(start=MONDAY),
                               cfg_overrides={"persona": str(persona)})
    try:
        rows = await k.db.fetchall("SELECT kind, source, content FROM mem_records ORDER BY content")
        assert len(rows) == 3
        assert all(r["source"] == "seeded" for r in rows)
        assert all(r["kind"] == "episodic" for r in rows)      # INV-11: seeded never semantic
        assert await k.memory.load_seed_memories() == 0          # idempotent
    finally:
        await k.stop()
