"""Tests for the runtime foundation: clock, store, bus, scheduler, llm router, embeddings."""
import asyncio
import json

import numpy as np
import pytest

from alive.core.types import Event, Trust, new_id
from alive.runtime.bus import EventBus
from alive.runtime.clock import ManualClock
from alive.runtime.embed import HashingEmbedder, VectorIndex
from alive.runtime.llm import (LLMRequest, MockProvider, ModelRouter, apply_schema, parse_json_loose)
from alive.runtime.scheduler import Scheduler
from alive.runtime.store import Database, register_schema

register_schema("test_rt", 1, ["CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"])


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.start()
    yield d
    await d.close()


async def test_ids_are_ordered():
    ids = [new_id() for _ in range(2000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


async def test_manual_clock_orders_sleepers():
    c = ManualClock(start=1000)
    order = []

    async def s(n, d):
        await c.sleep(d)
        order.append((n, c.now()))

    ts = [asyncio.create_task(s("b", 20)), asyncio.create_task(s("a", 10)), asyncio.create_task(s("c", 30))]
    await c.advance(25)
    assert order == [("a", 1010), ("b", 1020)]
    await c.advance(10)
    assert order[-1] == ("c", 1030)
    await asyncio.gather(*ts)


async def test_db_concurrent_writes_group_commit(db):
    async def w(i):
        return await db.execute("INSERT INTO t (v) VALUES (?)", (f"v{i}",))
    await asyncio.gather(*(w(i) for i in range(500)))
    assert await db.fetchval("SELECT COUNT(*) FROM t") == 500
    assert db.stats["write_batches"] < 500  # batched


async def test_db_failed_job_isolated(db):
    async def bad():
        await db.execute("INSERT INTO nope VALUES (1)")
    good = db.execute("INSERT INTO t (v) VALUES ('ok')")
    res = await asyncio.gather(bad(), good, return_exceptions=True)
    assert isinstance(res[0], Exception)
    assert await db.fetchval("SELECT COUNT(*) FROM t WHERE v='ok'") == 1


async def test_db_kv(db):
    await db.kv_set("ns", "a", {"x": [1, 2]})
    assert await db.kv_get("ns", "a") == {"x": [1, 2]}
    assert await db.kv_all("ns") == {"a": {"x": [1, 2]}}


async def test_bus_persist_dispatch_query(db):
    clock = ManualClock(start=5000)
    bus = EventBus(db, clock)
    got = []

    async def h(ev):
        got.append(ev.type)
    bus.subscribe("percept.*|core.thought", h, name="t")
    await bus.emit("percept.message", {"text": "hi"}, trust=Trust.CONTACT, thread_id="cli:1")
    await bus.emit("core.thought", {"text": "hmm"})
    await bus.emit("aff.delta", {})
    await bus.drain()
    assert got == ["percept.message", "core.thought"]
    evs = await bus.query(thread_id="cli:1")
    assert len(evs) == 1 and evs[0].payload["text"] == "hi" and evs[0].trust == Trust.CONTACT
    all_ = [e async for e in bus.replay()]
    assert [e.type for e in all_] == ["percept.message", "core.thought", "aff.delta"]
    await bus.close()


async def test_bus_slow_subscriber_does_not_block(db):
    clock = ManualClock()
    bus = EventBus(db, clock)

    async def slow(ev):
        await asyncio.sleep(10)
    sub = bus.subscribe("*", slow, maxsize=5)
    for i in range(20):
        await bus.emit("x.y", {"i": i}, persist=False)
    assert sub.dropped > 0
    await bus.close()


async def test_scheduler_every_and_restart():
    clock = ManualClock(start=0)
    s = Scheduler(clock)
    n = {"every": 0, "crash": 0}

    async def tick():
        n["every"] += 1

    async def crashy():
        n["crash"] += 1
        raise RuntimeError("boom")
    s.every(10, tick, "tick")
    s.spawn(crashy, "crashy", restart=True)
    await clock.advance(55)
    assert n["every"] == 5
    assert n["crash"] >= 3 and s.errors >= 3
    await s.close()


def test_json_loose():
    assert parse_json_loose('```json\n{"a": 1,}\n```') == {"a": 1}
    assert parse_json_loose('好的：{"a": true} 完毕') == {"a": True}
    assert parse_json_loose("nope") is None
    assert apply_schema({"a": "3", "c": "x"}, {"a": 0, "b": [], "c": ["d"]}) == {"a": 3, "b": [], "c": ["x"]}


async def test_router_mock_json_retry_and_ledger():
    clock = ManualClock()
    mock = MockProvider()
    state = {"n": 0}

    def h(req):
        state["n"] += 1
        return "not json" if state["n"] == 1 else {"ok": True}
    mock.on("t.json", h)
    tiers = {t: {"provider": "m", "model": "mock", "concurrency": 2} for t in ("fast", "deliberate", "deep")}
    r = ModelRouter(tiers, {"m": mock}, clock=clock, pricing={"mock-fast": {"input_miss": 1e6, "output": 1e6}})
    out = await r.json("t.json", "x", schema={"ok": False, "n": 1})
    assert out == {"ok": True, "n": 1}
    assert len(mock.calls) == 2
    assert r.ledger.calls == 2 and r.ledger.today_usd > 0


async def test_router_hooks_downgrade_and_identity_floor():
    clock = ManualClock()
    mock = MockProvider()
    tiers = {t: {"provider": "m", "model": "mock"} for t in ("fast", "deliberate", "deep")}
    r = ModelRouter(tiers, {"m": mock}, clock=clock)
    r.add_hook(lambda req, tier: "fast")
    resp = await r.complete(LLMRequest(purpose="a", tier="deep"))
    assert resp.tier == "fast"
    resp = await r.complete(LLMRequest(purpose="a", tier="deep", identity_critical=True))
    assert resp.tier == "deliberate"


async def test_router_concurrency_limit():
    clock = ManualClock()
    mock = MockProvider(latency=0.05)
    tiers = {"fast": {"provider": "m", "model": "x", "concurrency": 3}, "deliberate": {"provider": "m", "model": "x"},
             "deep": {"provider": "m", "model": "x"}}
    r = ModelRouter(tiers, {"m": mock}, clock=clock, rps=1000, burst=1000)
    peak = {"v": 0}
    orig = mock.complete

    async def wrapped(req, cfg):
        peak["v"] = max(peak["v"], r.inflight)
        return await orig(req, cfg)
    mock.complete = wrapped  # type: ignore
    await asyncio.gather(*(r.complete(LLMRequest(purpose="p", tier="fast")) for _ in range(12)))
    assert peak["v"] <= 3


async def test_hashing_embedder_similarity():
    e = HashingEmbedder(512)
    v = await e.embed(["我明天要去面试，有点紧张", "明天面试好紧张", "今天天气很好适合晒太阳", "interview tomorrow nervous"])
    sim = v @ v.T
    assert sim[0, 1] > sim[0, 2]
    assert abs(float(np.linalg.norm(v[0])) - 1) < 1e-5


async def test_vector_index_add_remove_search():
    e = HashingEmbedder(256)
    idx = VectorIndex(256, capacity=4)
    texts = {f"id{i}": f"文本{i} 关于猫和狗" if i % 2 else f"文本{i} 关于数学和物理" for i in range(50)}
    vecs = await e.embed(list(texts.values()))
    idx.add_many(texts.keys(), vecs)
    q = await e.embed_one("猫狗")
    res = await idx.search(q, k=5)
    assert all(int(i[2:]) % 2 == 1 for i, _ in res)
    idx.remove("id1")
    assert "id1" not in [i for i, _ in idx.search_sync(q, k=50)]
    assert len(idx) == 49


async def test_router_state_temperature_for_composition_only_and_override_wins():
    """Per-request temperature (docs/10 §4.2): composition purposes follow playfulness and arousal around the tier's
    base, clipped; other purposes keep the base; an explicit request temperature always wins."""
    clock = ManualClock()
    mock = MockProvider()
    tiers = {t: {"provider": "m", "model": "mock", "temperature": b} for t, b in
             (("fast", 0.7), ("deliberate", 0.9), ("deep", 0.8))}
    r = ModelRouter(tiers, {"m": mock}, clock=clock)
    state = {"playfulness": 0.5, "arousal": 0.0}
    r.set_state_source(lambda: dict(state))
    temps = []
    for play, ar in ((0.2, -0.3), (0.5, 0.0), (0.9, 0.4)):
        state.update(playfulness=play, arousal=ar)
        req = LLMRequest(purpose="s2.reply", tier="deliberate")
        await r.complete(req)
        temps.append(mock.calls[-1].temperature)
        assert req.temperature is None                     # the caller's request is not mutated
    assert temps[1] == 0.9 and temps[0] < temps[1] < temps[2]
    assert abs(temps[2] - (0.9 + 0.15 * 0.4 + 0.1 * 0.4)) < 1e-9
    state.update(playfulness=1.0, arousal=5.0)
    await r.complete(LLMRequest(purpose="s2.reply", tier="deliberate"))
    assert mock.calls[-1].temperature == 1.2                # safe bound
    await r.complete(LLMRequest(purpose="mem.extract", tier="fast"))
    assert mock.calls[-1].temperature is None               # provider uses the tier's configured 0.7
    await r.complete(LLMRequest(purpose="s2.reply", tier="deliberate", temperature=0.2))
    assert mock.calls[-1].temperature == 0.2                # per-request override
