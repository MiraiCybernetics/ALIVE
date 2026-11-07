"""Initiative safety gate (docs/09 §4): governance refuses only what must never happen - strangers without
pairing, opt-out, high dependency risk, kill switch, budget - and trips a malfunction fuse on inhuman bursts.
It has NO behavioural quotas (per-day caps, minimum gaps, quiet-hour windows, ignored-streak blocks): timing and
frequency come from the state-dependent initiative process (alive/core/initiative.py, tests/core/test_initiative.py)."""
import pytest

from tests.governance import govhelp
from tests.governance.govhelp import events, ts
from alive.core.types import EV

H = 3600.0


@pytest.fixture
async def k(tmp_path):
    k = await govhelp.kernel(tmp_path)
    for p in ("tg:L", "tg:M", "tg:N", "tg:O"):
        k.gov.set_trust(p, "contact")
    yield k
    await k.stop()


async def test_no_quotas_no_gaps_no_quiet_windows(k):
    """Normal human-scale behaviour is never refused by count, gap or clock time."""
    g = k.gov
    now = k.clock.now()
    for i in range(6):                                   # six initiatives to one person over a day: not refused
        t = now + i * 2 * H
        assert g.initiative_allowed("tg:L", t).allow
        g.record_initiative("tg:L", t)
    for p in ("tg:M", "tg:N", "tg:O"):
        assert g.initiative_allowed(p, now).allow
    assert g.initiative_allowed("tg:L", ts("2026-11-09 23:30")).allow  # late at night: the initiative process
    assert g.initiative_allowed("tg:L", ts("2026-11-10 03:00")).allow  # damps it smoothly; no hard window here


async def test_malfunction_fuse_trips_only_on_inhuman_bursts(k):
    g = k.gov
    now = k.clock.now()
    for i in range(g.fuse_n):
        assert g.initiative_allowed("tg:L", now + i).allow
        g.record_initiative("tg:L", now + i)
    d = g.initiative_allowed("tg:L", now + 30)
    assert not d.allow and d.rule == "fuse"
    assert g.initiative_allowed("tg:L", now + g.fuse_window_s + 60).allow     # the burst has passed
    assert g.initiative_allowed("tg:M", now + 30).allow                       # other people unaffected


async def test_first_contact_and_opt_out(k):
    g = k.gov
    d = g.initiative_allowed("tg:stranger", k.clock.now())
    assert not d.allow and d.rule == "first_contact"
    g.set_trust("tg:pub", "public")
    assert g.initiative_allowed("tg:pub", k.clock.now()).rule == "first_contact"
    g.set_proactive_ok("tg:L", False)
    assert g.initiative_allowed("tg:L", k.clock.now()).rule == "opt_out"
    g.set_proactive_ok("tg:L", True)
    assert g.initiative_allowed("tg:L", k.clock.now()).allow
    assert g.initiative_allowed("cli:owner", k.clock.now()).allow    # owner from config
    denials = [e for e in await events(k, EV.GOV_DECISION) if e.get("op") == "initiative_allowed"]
    assert {e["rule"] for e in denials} >= {"first_contact", "opt_out"}


async def test_ignored_results_are_tracked_but_do_not_block(k):
    """How earlier initiatives were received feeds the initiative process (reception); governance doesn't block."""
    g = k.gov
    now = k.clock.now()
    for _ in range(4):
        await k.bus.emit(EV.OUTREACH_PLAN, {"op": "result", "person_key": "tg:M", "outcome": "ignored"})
    await k.bus.drain()
    assert g._outcomes["tg:M"]["ignored_streak"] == 4
    assert g.initiative_allowed("tg:M", now).allow
    g.record_initiative_result("tg:M", "replied_warm")
    assert g._outcomes["tg:M"]["ignored_streak"] == 0


async def test_dependency_risk_is_a_safeguard(k):
    g = k.gov
    now = k.clock.now()
    g.dependency_risk = lambda pk: 0.6
    assert g.initiative_allowed("tg:L", now).allow          # moderate risk only damps the initiative process
    g.dependency_risk = lambda pk: 0.9
    assert g.initiative_allowed("tg:M", now).rule == "dependency_cooldown"


async def test_initiatives_persist(tmp_path):
    k = await govhelp.kernel(tmp_path)
    k.gov.set_trust("tg:L", "contact")
    now = k.clock.now()
    k.gov.record_initiative("tg:L", now)
    await k.stop()
    k2 = await govhelp.kernel(tmp_path)
    try:
        for i in range(1, k2.gov.fuse_n):
            k2.gov.record_initiative("tg:L", now + i)
        assert k2.gov.initiative_allowed("tg:L", now + 60).rule == "fuse"      # the persisted act counts
    finally:
        await k2.stop()


async def test_require_paired_can_be_relaxed(tmp_path):
    k = await govhelp.kernel(tmp_path, cfg={"governance": {"initiative": {"require_paired": False}}})
    try:
        g = k.gov
        assert g.initiative_allowed("tg:unpaired", k.clock.now()).allow
        g.set_trust("tg:pub", "public")
        assert g.initiative_allowed("tg:pub", k.clock.now()).rule == "first_contact"   # explicit low trust still blocks
    finally:
        await k.stop()
