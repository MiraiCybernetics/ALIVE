"""Dependency monitoring (docs/09 §7.3): usage minutes, late-night share, streaks, reliance cues."""
import pytest

from tests.governance import govhelp
from tests.governance.govhelp import events, ts
from alive.core.types import EV

M = 60.0


@pytest.fixture
async def k(tmp_path):
    k = await govhelp.kernel(tmp_path)
    yield k
    await k.stop()


def session(g, pk, start, minutes, every=2.0, text="嗯嗯"):
    t = start
    while t < start + minutes * M:
        g.observe_usage(pk, t, text)
        t += every * M


async def test_light_use_is_low_risk(k):
    g = k.gov
    for d in range(7):
        session(g, "tg:A", ts("2026-11-02 19:00") + d * 86400, 20)
    rep = g.dependency_report("tg:A", ts("2026-11-08 22:00"))
    assert rep["avg_minutes"] == pytest.approx(20, abs=3)
    assert g.dependency_risk("tg:A") < 0.2
    assert g.dependency_risk("tg:unknown") == 0.0


async def test_heavy_late_night_use_and_cues_raise_risk_and_emit(k):
    g = k.gov
    for d in range(14):
        session(g, "tg:B", ts("2026-10-26 23:00") + d * 86400, 200)           # 3+ h per night, after 23:00
    rep = g.dependency_report("tg:B")
    assert rep["avg_minutes"] > 150 and rep["late_share"] > 0.5 and rep["streak_days"] >= 7
    r1 = g.dependency_risk("tg:B")
    assert r1 >= 0.75
    g.observe_usage("tg:B", k.clock.now(), "只有你懂我，我不想和别人说话")
    assert g.dependency_report("tg:B")["cues_7d"] >= 2
    assert g.dependency_risk("tg:B") > r1
    evs = await events(k, "gov.dependency")
    levels = [e["level"] for e in evs]
    assert "elevated" in levels and "high" in levels
    assert all(e["person_key"] == "tg:B" for e in evs)
    assert len(evs) <= 16                                                  # not one per message


async def test_usage_counted_from_bus_and_persisted(tmp_path):
    k = await govhelp.kernel(tmp_path)
    try:
        for i in range(10):
            await k.bus.emit(EV.MESSAGE_IN, {"channel": "tg", "chat_id": "C", "sender_id": "C", "chat_type": "private",
                                             "text": "在吗", "ts": k.clock.now() + i * 60})
        await k.bus.emit(EV.MESSAGE_IN, {"channel": "tg", "chat_id": "C", "sender_id": "bot", "is_bot": True,
                                         "text": "spam", "ts": k.clock.now()})
        await k.bus.drain()
        day = k.gov._usage["tg:C"][k.clock.today()]
        assert day["msgs"] == 10 and day["minutes"] == pytest.approx(10, abs=0.01)
        assert "tg:bot" not in k.gov._usage
    finally:
        await k.stop()
    k2 = await govhelp.kernel(tmp_path)
    try:
        assert k2.gov._usage["tg:C"][k2.clock.today()]["msgs"] == 10
    finally:
        await k2.stop()
