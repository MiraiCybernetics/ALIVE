"""Intentions: lifecycle, triggers, ranking, abandonment, open loops, day plan + INV-6 (docs/05 §4)."""
import pytest

from tests.motivation import mothelp
from tests.motivation.mothelp import ts
from alive.core.types import EV
from alive.runtime.clock import ManualClock

H = 3600.0


@pytest.fixture
async def k(tmp_path):
    k = await mothelp.kernel(tmp_path, real=("motivation",))
    yield k
    await k.stop()


async def intention_events(k, op=None):
    await k.clock.settle()
    evs = await k.bus.query(types=[EV.INTENTION])
    return [e.payload for e in evs if op is None or e.payload["op"] == op]


async def db_status(k, iid):
    return await k.db.fetchval("SELECT status FROM mot_intentions WHERE id=?", (iid,))


async def test_lifecycle_and_done_satisfies_competence(k):
    m = k.motivation
    it = await m.add_intention({"goal": "读完那篇关于记忆巩固的论文", "level": "now", "kind": "read"}, "ev-src")
    assert it.status == "dormant" and await db_status(k, it.id) == "dormant"
    assert [c.id for c in await m.candidates(k.clock.now())] == [it.id]
    await m.update_intention(it.id, status="active")
    await m.update_intention(it.id, status="suspended", note="被消息打断")
    assert it.suspensions == 1 and it.progress[-1]["note"] == "被消息打断"
    await m.update_intention(it.id, status="active")
    before = m.needs()["competence"]["level"]
    await m.update_intention(it.id, status="done")
    assert await db_status(k, it.id) == "done"
    assert m.needs()["competence"]["level"] > before
    assert any(c[0] == "on_need_event" and c[1][0] == "competence" for c in k.affect.calls)
    assert any(c[0] == "on_need_event" and c[1][0] == "autonomy" for c in k.affect.calls)   # self-initiated
    await m.update_intention(it.id, status="active")             # terminal: ignored
    assert await db_status(k, it.id) == "done"
    ops = [p["op"] for p in await intention_events(k)]
    assert ops == ["created", "active", "suspended", "active", "done"]
    with pytest.raises(ValueError):
        await m.add_intention({"goal": "   "})


async def test_giving_up_after_interruptions_is_a_draw_with_affect_and_memory(k):
    """No fixed "abandon at the 4th suspension": each intention has its own patience (median ~3.5 interruptions
    at priority 0.5), more for what matters more."""
    import random
    import statistics
    m = k.motivation
    m.rng = random.Random(5)

    async def interruptions(goal, priority):
        it = await m.add_intention({"goal": goal, "level": "now", "kind": "general", "priority": priority})
        n = 0
        while it.status != "abandoned" and n < 40:
            await m.update_intention(it.id, status="active")
            await m.update_intention(it.id, status="suspended")
            n += 1
        return n, it
    low = [(await interruptions(f"整理书签{i}", 0.5))[0] for i in range(60)]
    high = [(await interruptions(f"写信{i}", 0.9))[0] for i in range(60)]
    assert 2 <= statistics.median(low) <= 6 and min(low) < max(low)          # a spread, around the old 4th
    assert statistics.mean(high) > statistics.mean(low)
    _, it = await interruptions("整理书签", 0.5)
    assert it.status == "abandoned" and await db_status(k, it.id) == "abandoned"
    ab = await intention_events(k, "abandoned")
    assert ab and ab[-1]["reason"] == "gave_up"
    deltas = [c[1][0][0] for c in k.affect.calls if c[0] == "apply_deltas"]
    assert deltas and deltas[-1]["type"] == "disappointment"
    await k.clock.settle()
    assert any("放弃" in r["content"] for r in k.memory.records)


async def test_commitments_are_not_auto_abandoned(k):
    m = k.motivation
    it = await m.add_intention({"goal": "把链接发给 L", "level": "now", "kind": "outreach", "target_person": "tg:L",
                                "is_commitment": True, "due_in_s": 3600})
    for _ in range(6):
        await m.update_intention(it.id, status="suspended")
        await m.update_intention(it.id, status="active")
    await k.clock.advance(3 * 86400)
    await m.candidates(k.clock.now())
    assert it.status == "active"
    await m.update_intention(it.id, status="abandoned", note="链接失效了")
    deltas = [c[1][0][0] for c in k.affect.calls if c[0] == "apply_deltas"]
    assert deltas[-1]["type"] == "guilt"


async def test_forgetting_is_a_draw_and_day_passed(k):
    """No fixed "stale after 24 h": each untouched intention slips my mind at its own time (median ~1 day for a
    'now' thing at priority 0.5), later for what matters more; a day plan for a day that is over is a fact."""
    import random
    m = k.motivation
    m.rng = random.Random(3)
    low = [await m.add_intention({"goal": f"看看那个视频{i}", "level": "now", "kind": "read", "priority": 0.3})
           for i in range(80)]
    high = [await m.add_intention({"goal": f"回那封邮件{i}", "level": "now", "kind": "general", "priority": 0.9})
            for i in range(80)]
    later = await m.add_intention({"goal": "过几天提醒自己交作业", "level": "now",
                                   "trigger": {"type": "time", "in_s": 4 * 86400}})
    day = await m.add_intention({"goal": "上午读论文", "level": "day", "kind": "read"})
    proj = await m.add_intention({"goal": "写一篇关于记忆的文章", "level": "project", "kind": "learn"})
    gone = []
    for hours in (6, 19, 47):
        await k.clock.advance(hours * H)
        await m.candidates(k.clock.now())
        gone.append((sum(i.status == "abandoned" for i in low), sum(i.status == "abandoned" for i in high)))
    assert gone[0][0] < gone[1][0] < gone[2][0]               # likelier the longer it goes untouched
    assert 0 < gone[1][0] < 80                                # around a day: some slip, some don't
    assert all(h < l for l, h in gone[1:])                    # what matters more is kept longer
    assert later.status == "dormant" and proj.status == "dormant"
    reasons = {p["id"]: p["reason"] for p in await intention_events(k, "abandoned")}
    assert reasons[day.id] == "day_passed"
    assert {reasons[i.id] for i in low + high if i.status == "abandoned"} == {"forgotten"}


async def test_time_need_and_event_triggers(k):
    m = k.motivation
    t_it = await m.add_intention({"goal": "15 分钟后看看 M 回了没", "level": "now",
                                  "trigger": {"type": "time", "in_s": 900}})
    n_it = await m.add_intention({"goal": "社交需求高了就去群里看看", "level": "now",
                                  "trigger": {"type": "need", "need": "social", "threshold": 0.05}})
    o_it = await m.add_intention({"goal": "L 上线了就问她考试结果", "level": "now", "kind": "outreach",
                                  "target_person": "tg:L", "trigger": {"type": "online", "person": "tg:L"}})
    ids = lambda cs: {c.id for c in cs}  # noqa: E731
    assert ids(await m.candidates(k.clock.now())) == set()
    await k.clock.advance(901)
    assert ids(await m.candidates(k.clock.now())) == {t_it.id}
    assert t_it.status == "active"
    m.model.needs["social"].level = 0.3
    assert n_it.id in ids(await m.candidates(k.clock.now()))
    await k.bus.emit(EV.MESSAGE_IN, {"channel": "tg", "sender_id": "Q", "chat_id": "Q", "text": "hi"})
    await k.bus.drain()
    assert o_it.status == "dormant"
    await k.bus.emit(EV.MESSAGE_IN, {"channel": "tg", "sender_id": "L", "chat_id": "L", "text": "我回来啦"})
    await k.bus.drain()
    await k.clock.settle()
    assert o_it.status == "active" and o_it.meta["trigger_event"]
    assert o_it.id in ids(await m.candidates(k.clock.now()))


async def test_ranking_priority_components(k):
    m = k.motivation
    plain = await m.add_intention({"goal": "随便逛逛订阅源", "level": "now", "kind": "read", "priority": 0.6})
    promise = await m.add_intention({"goal": "给 L 发资料", "level": "now", "kind": "outreach", "target_person": "tg:L",
                                     "is_commitment": True, "due_in_s": 1800, "priority": 0.5})
    parent = await m.add_intention({"goal": "成为一个会倾听的人", "level": "life", "priority": 0.9})
    child = await m.add_intention({"goal": "回顾今天的对话", "level": "now", "kind": "reflect", "priority": 0.4,
                                   "parent": parent.id})
    dread = await m.add_intention({"goal": "处理烦人的报销", "level": "now", "priority": 0.6,
                                   "affect_tag": {"valence": -0.8}})
    c = await m.candidates(k.clock.now(), limit=10)
    order = [x.id for x in c]
    assert order[0] == promise.id                               # commitment + due soon
    assert child.score == pytest.approx(0.4 + 0.3 * 0.9, abs=0.01)
    assert dread.score < plain.score                            # dreaded tasks rank lower
    assert parent.id not in order                               # life goals are context, not candidates
    assert len(await m.candidates(k.clock.now(), limit=2)) == 2


async def test_dedupe_same_goal(k):
    m = k.motivation
    a = await m.add_intention({"goal": "回 L 的消息", "kind": "reply", "target_person": "tg:L"})
    b = await m.add_intention({"goal": "回L的消息！", "kind": "reply", "target_person": "tg:L", "priority": 0.9,
                               "is_commitment": True})
    assert a.id == b.id and a.priority == 0.9 and a.is_commitment and a.max_suspensions is None
    assert len(await m.active_intentions()) == 1


async def test_open_loops(k):
    m = k.motivation
    k.social.names["tg:L"] = "L"
    await m.add_intention({"goal": "把那篇文章发给她", "kind": "outreach", "target_person": "tg:L",
                           "is_commitment": True, "due_in_s": -60})
    act = await m.add_intention({"goal": "想清楚 L 说的那个问题", "kind": "reflect"})
    await m.update_intention(act.id, status="active")
    pid = "op-test"
    m._plans[pid] = {"id": pid, "person_key": "tg:M", "thread_id": "tg:M", "reason_type": "care",
                     "content_hint": "问问面试怎么样", "score": 0.4, "source_event_ids": ["e1"],
                     "created_at": k.clock.now(), "sent_at": None, "outcome": None, "latency_s": None,
                     "resolved_at": None, "meta": {}}
    m.outreach_sent(pid)
    loops = await m.open_loops()
    assert loops[0] == "答应了L：把那篇文章发给她（已经过了约定时间）"
    assert any(x.startswith("在等M回复") for x in loops)
    assert "没做完：想清楚 L 说的那个问题" in loops
    assert len(await m.open_loops(limit=1)) == 1


async def test_day_plan_enforces_inv6(k):
    m = k.motivation
    date = "2026-11-10"
    await m.set_day_plan(date, [
        {"goal": "回 L 昨晚的消息", "kind": "reply", "target_person": "tg:L", "window": "09:00-10:00"},
        {"goal": "帮 M 看简历", "kind": "general", "requested_by": "tg:M", "window": "10:00-11:00"},
    ])
    plan = await m.active_intentions(levels=["day"])
    assert len(plan) == 4
    auto = [i for i in plan if i.meta.get("autonomous")]
    free = [i for i in plan if i.meta.get("free")]
    assert len(auto) == 1 and auto[0].meta["added_by"] == "INV-6"
    assert len(free) == 1 and free[0].meta["added_by"] == "INV-6"
    windows = sorted(tuple(i.meta["window"]) for i in plan)
    for (a1, b1), (a2, b2) in zip(windows, windows[1:]):
        assert b1 <= a2                                  # added slots do not overlap the plan
    ev = (await intention_events(k, "day_plan"))[-1]
    assert sorted(ev["inv6_added"]) == ["autonomous", "free"]
    # free slots are not tasks
    cands = await m.candidates(ts("2026-11-10 11:30"), limit=10)
    assert all(not c.meta.get("free") for c in cands)
    # a compliant plan is kept as is, and replaces the previous one for that date
    await m.set_day_plan(date, [
        {"goal": "读论文", "kind": "read", "window": "09:00-11:00"},
        {"goal": "发呆", "kind": "free", "window": "14:00-15:00"},
    ])
    plan2 = await m.active_intentions(levels=["day"])
    assert sorted(i.goal for i in plan2) == ["发呆", "读论文"]
    assert (await intention_events(k, "day_plan"))[-1]["inv6_added"] == []
    replaced = [p for p in await intention_events(k, "abandoned") if p["reason"] == "replaced"]
    assert len(replaced) == 4


async def test_day_plan_triggers_by_window(k):
    m = k.motivation
    await m.set_day_plan("2026-11-09", [{"goal": "下午读论文", "kind": "read", "window": "14:00-16:00"},
                                        {"goal": "放空", "kind": "free"}])
    assert not [c for c in await m.candidates(k.clock.now()) if c.goal == "下午读论文"]   # 10:00
    cands = await m.candidates(ts("2026-11-09 14:05"))
    assert [c.goal for c in cands if c.goal == "下午读论文"]


async def test_open_intentions_reload_after_restart(tmp_path):
    clock = ManualClock(start=ts("2026-11-09 10:00"))
    k = await mothelp.kernel(tmp_path, real=("motivation",), clock=clock)
    it = await k.motivation.add_intention({"goal": "学会用 Zotero", "level": "project", "kind": "learn",
                                           "trigger": {"type": "online", "person": "tg:L"}})
    done = await k.motivation.add_intention({"goal": "已经完成的事"})
    await k.motivation.update_intention(done.id, status="done")
    await k.stop()
    k2 = await mothelp.kernel(tmp_path, real=("motivation",), clock=ManualClock(start=clock.now() + 60))
    try:
        ids = [i.id for i in await k2.motivation.active_intentions()]
        assert ids == [it.id]
        assert it.id in k2.motivation._event_index[EV.MESSAGE_IN]
    finally:
        await k2.stop()


async def test_added_day_plan_slots_are_sampled_over_my_waking_energy(tmp_path):
    """INV-6 slots are not the first gap in fixed active hours: they are sampled over the waking span the affect
    system expects (sampled wake/bed times), weighted by expected energy; they vary and never overlap the plan."""
    import random
    k = await mothelp.kernel(tmp_path, real=("affect", "motivation"))
    try:
        k.chance_rng = random.Random(8)
        m, a = k.motivation, k.affect
        wake, bed = a.waking_span()
        wake_h = k.clock.hour_of_day(wake)
        starts = []
        for _ in range(60):
            gap = m._find_gap([(14.0, 16.0)])
            assert gap is not None and (gap[1] <= 14.0 or gap[0] >= 16.0)     # no overlap with the plan
            assert wake_h + 0.5 - 1e-9 <= gap[0] and gap[1] <= wake_h + (bed - wake) / 3600.0
            starts.append(gap[0])
        assert len(set(starts)) > 10                                          # sampled, not the first gap
        # energy-weighted: the afternoon (near the circadian peak) is chosen more often than the late evening
        afternoon = sum(1 for s in starts if 12.0 <= s < 18.0)
        late = sum(1 for s in starts if s >= 21.5)
        assert afternoon > late
    finally:
        await k.stop()
