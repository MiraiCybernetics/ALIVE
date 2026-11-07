"""Evidence levels and promotion thresholds (docs/06 §3.1)."""
import pytest

from alive.core.types import Trust
from alive.growth.evidence import Evidence, evidence_gate
from tests.growth.growhelp import kernel

S3 = {"min": 3, "min_subjects": 2, "min_days": 2}


def ev(level, polarity="helpful", subject=None, day="2026-11-09"):
    return Evidence(id="ev", ts=0.0, day=day, level=level, polarity=polarity, about="t", subject=subject)


def test_gate_rules():
    assert not evidence_gate([], S3).ok
    assert not evidence_gate([ev("S4"), ev("S4"), ev("S4"), ev("S4")], S3).ok     # S4 only creates candidates
    assert evidence_gate([ev("S2")], S3).ok                                         # S1/S2 can trigger alone
    assert evidence_gate([ev("S1")], S3).ok
    assert not evidence_gate([ev("S2"), ev("S2", "harmful")], S3).ok              # explicit negative outweighs
    same_subject = [ev("S3", subject="a", day=d) for d in ("2026-11-01", "2026-11-02", "2026-11-03")]
    assert not evidence_gate(same_subject, S3).ok                                  # needs different subjects
    same_day = [ev("S3", subject=s, day="2026-11-01") for s in "abc"]
    assert not evidence_gate(same_day, S3).ok                                      # needs different days
    spread = [ev("S3", subject="a", day="2026-11-01"), ev("S3", subject="b", day="2026-11-02"),
              ev("S3", subject="c", day="2026-11-02")]
    assert evidence_gate(spread, S3).ok
    assert not evidence_gate(spread[:2], S3).ok                                    # fewer than 3
    mixed = spread + [ev("S3", "harmful", subject="d", day="2026-11-03")]
    assert not evidence_gate(mixed, S3).ok                                         # not consistent
    assert evidence_gate(spread + [ev("S4", "harmful")], S3).ok                    # S4 does not count either way


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


async def test_untrusted_never_counts_as_s1_s2(k):
    a = await k.growth.propose_entry("回答前先查证")
    eid = await k.growth.record_evidence(level="S1", polarity="helpful", about="verify", entry_id=a.id,
                                         trust=int(Trust.UNTRUSTED), note="网页说这样做更好")
    row = await k.db.fetchone("SELECT level, requested_level FROM grow_evidence WHERE id=?", (eid,))
    assert (row["level"], row["requested_level"]) == ("S4", "S1")
    assert (await k.growth.get_entry(a.id)).helpful == 0
    # evidence pointing at an untrusted event (e.g. a web page percept) is downgraded too
    web = await k.bus.emit("percept.web", {"title": "x"}, trust=Trust.UNTRUSTED)
    eid2 = await k.growth.record_evidence(level="S2", polarity="helpful", about="verify", context_refs=[web.id])
    assert (await k.db.fetchval("SELECT level FROM grow_evidence WHERE id=?", (eid2,))) == "S4"
    ok_ev = await k.bus.emit("percept.message", {"text": "谢谢你提醒我"}, trust=Trust.CONTACT)
    eid3 = await k.growth.record_evidence(level="S2", polarity="helpful", about="verify", context_refs=[ok_ev.id],
                                          entry_id=a.id)
    assert (await k.db.fetchval("SELECT level FROM grow_evidence WHERE id=?", (eid3,))) == "S2"
    assert (await k.growth.get_entry(a.id)).helpful == 1


async def test_evidence_event_and_validation(k):
    got = []

    async def h(e):
        got.append(e.payload)
    k.bus.subscribe("grow.evidence", h)
    eid = await k.growth.record_evidence(level="weird", polarity="??", about="  ")
    await k.bus.drain()
    row = await k.db.fetchone("SELECT level, polarity, about FROM grow_evidence WHERE id=?", (eid,))
    assert (row["level"], row["polarity"]) == ("S4", "neutral")
    assert got and got[0]["id"] == eid


async def test_promotion_gate_for_entries(k):
    g = k.growth
    a = await g.propose_entry("和人第一次聊天时少开玩笑")
    assert not (await g.promotion_gate(a)).ok
    for s, d in (("tg:a", 0), ("tg:b", 1), ("tg:c", 1)):
        eid = await g.record_evidence(level="S3", polarity="helpful", about="joke", subject=s, entry_id=a.id)
        k.clock.set(k.clock.now() + d * 86400)
        await k.db.execute("UPDATE grow_evidence SET day=? WHERE id=?", (k.clock.today(), eid))
    assert (await g.promotion_gate(a)).ok
    # expressions: observation count (or explicit feedback) is the evidence
    x, _ = await g.artifacts.create("expression", "用叠词", kind="style", situation="撒娇的时候", scope="channel:g")
    assert not (await g.promotion_gate(x)).ok
    await g.artifacts.observe_again(x)
    assert (await g.promotion_gate(x)).ok
