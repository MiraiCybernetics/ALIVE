"""Budget (docs/09 §4): router hook downgrade at 80 %, deny at 100 % (except S1/mentions), weekly cap,
throttle events, persistence across restarts."""
import pytest

from tests.governance import govhelp
from tests.governance.govhelp import events, ts
from alive.core.types import EV
from alive.runtime.clock import ManualClock
from alive.runtime.llm import BudgetDenied, LLMRequest


@pytest.fixture
async def k(tmp_path):
    k = await govhelp.kernel(tmp_path, cfg={"governance": {"budget": {"daily_usd": 1.0}}})
    yield k
    await k.stop()


def spend(k, usd):
    led = k.llm.ledger
    led.day = k.clock.today()
    led.today_usd = usd


async def test_route_hook_registered_and_passthrough(k):
    assert k.gov.route_hook in k.llm.hooks
    resp = await k.llm.complete(LLMRequest(purpose="s2.reply", tier="deep"))
    assert resp.tier == "deep"


async def test_downgrade_at_80_percent(k):
    spend(k, 0.85)
    g = k.gov
    assert g.budget_ratio() == pytest.approx(0.85)
    assert g.route_hook(LLMRequest(purpose="sleep.reflect", tier="deep"), "deep") == "deliberate"
    assert g.route_hook(LLMRequest(purpose="s2.reply"), "deliberate") == "fast"
    assert g.route_hook(LLMRequest(purpose="x"), "fast") == "fast"
    assert g.route_hook(LLMRequest(purpose="self.narrative", tier="deep", identity_critical=True), "deep") == "deep"
    resp = await k.llm.complete(LLMRequest(purpose="s2.reply", tier="deep"))
    assert resp.tier == "deliberate"
    thr = await events(k, EV.BUDGET_THROTTLED)
    assert len(thr) == 1 and thr[0]["kind"] == "downgrade"              # rate-limited


async def test_deny_at_100_percent_except_system1_and_mentions(k):
    spend(k, 1.2)
    g = k.gov
    with pytest.raises(BudgetDenied):
        g.route_hook(LLMRequest(purpose="core.monologue", tier="deliberate"), "deliberate")
    with pytest.raises(BudgetDenied):
        await k.llm.complete(LLMRequest(purpose="mot.concretize", tier="fast"))
    # direct mentions (priority <= 2) and System-1 still run, on the fast tier
    assert g.route_hook(LLMRequest(purpose="s2.reply", tier="deliberate", priority=1), "deliberate") == "fast"
    assert g.route_hook(LLMRequest(purpose="s1.read", tier="fast"), "fast") == "fast"
    assert g.route_hook(LLMRequest(purpose="gov.crisis_confirm", tier="fast"), "fast") == "fast"
    resp = await k.llm.complete(LLMRequest(purpose="s2.reply", tier="deliberate", priority=1))
    assert resp.tier == "fast"
    kinds = {e["kind"] for e in await events(k, EV.BUDGET_THROTTLED)}
    assert kinds == {"deny", "downgrade"}
    # autonomous cycles and initiatives stop too
    g.set_trust("tg:L", "contact")
    assert not g.initiative_allowed("tg:L", k.clock.now()).allow


async def test_weekly_cap_and_persistence(tmp_path):
    clock = ManualClock(start=ts("2026-11-09 10:00"))
    cfg = {"governance": {"budget": {"daily_usd": 1.0, "weekly_usd": 2.0}}}
    k = await govhelp.kernel(tmp_path, clock=clock, cfg=cfg)
    spend(k, 0.7)
    await k.stop()                                       # persisted at stop
    # restart later the same day: spend survives the in-memory ledger reset
    k2 = await govhelp.kernel(tmp_path, clock=ManualClock(start=ts("2026-11-09 18:00")), cfg=cfg)
    try:
        assert k2.gov.spend_today() == pytest.approx(0.7)
        spend(k2, 0.2)
        assert k2.gov.spend_today() == pytest.approx(0.9)
        await k2.gov._periodic()
    finally:
        await k2.stop()
    # next days: the weekly cap binds even though each day is under its daily cap
    k3 = await govhelp.kernel(tmp_path, clock=ManualClock(start=ts("2026-11-10 10:00")), cfg=cfg)
    try:
        assert k3.gov.spend_today() == pytest.approx(0.0)
        spend(k3, 0.75)
        assert k3.gov.spend_week() == pytest.approx(1.65)
        assert k3.gov.budget_ratio() == pytest.approx(0.825)
        spend(k3, 1.0)
        assert k3.gov.budget_ratio() >= 1.0
    finally:
        await k3.stop()


async def test_day_rollover_resets_daily_spend(k):
    spend(k, 0.9)
    await k.clock.advance(86400)
    assert k.gov.spend_today() == pytest.approx(0.0)
    assert k.gov._spend_hist[govhelp.datetime.fromtimestamp(k.clock.now() - 86400, govhelp.TZ).strftime("%Y-%m-%d")] \
        == pytest.approx(0.9)
