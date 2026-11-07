"""check_action: tiers T0-T4, rule of two (INV-13), kill switch, affect gating, approvals, caps (docs/09 §3, §6)."""
import os

import pytest

from tests.governance import govhelp
from tests.governance.govhelp import events
from alive.core.types import EV, PermTier
from alive.interfaces import ActRequest
from alive.runtime.llm import BudgetDenied, LLMRequest

H = 3600.0
T0, T1, T2, T3, T4 = (PermTier.T0_READ, PermTier.T1_INTERNAL_WRITE, PermTier.T2_EXTERNAL_COMM,
                      PermTier.T3_IRREVERSIBLE, PermTier.T4_SELF_MODIFY)


@pytest.fixture
async def k(tmp_path):
    k = await govhelp.kernel(tmp_path)
    yield k
    await k.stop()


def act(kind="send_message", perm=T2, **kw):
    return ActRequest(kind=kind, perm=perm, **kw)


async def test_rule_of_two(k):
    g = k.gov
    d = await g.check_action(act(session_caps={"untrusted", "private"}))
    assert not d.allow and d.rule == "INV-13"
    assert (await g.check_action(act("write_note", T1, session_caps={"untrusted", "private"}))).allow
    assert (await g.check_action(act(session_caps={"untrusted"}))).allow          # reader-like: untrusted + external
    assert (await g.check_action(act(session_caps={"private"}))).allow            # contact chat: private + external
    d = await g.check_action(act("read_memory", T0, session_caps={"untrusted", "private", "external"}))
    assert not d.allow and d.rule == "INV-13"


async def test_tiers_t0_t1_allowed_silently_t2_audited(k):
    g = k.gov
    assert (await g.check_action(act("read_memory", T0))).allow
    assert (await g.check_action(act("write_note", T1))).allow
    assert (await g.check_action(act("send_message", T2, thread_id="tg:1", person_key="tg:L"))).allow
    evs = await events(k, EV.GOV_DECISION)
    assert [e["kind"] for e in evs] == ["send_message"] and evs[0]["allow"]


async def test_t3_escalates_and_owner_approval_allows_once(k):
    g = k.gov
    a = act("transfer_money", T3, text="给 L 转 50 元", person_key="tg:L")
    d = await g.check_action(a)
    assert not d.allow and d.escalate and d.rule == "T3_owner_approval"
    aid = a.meta["approval_id"]
    assert [p["id"] for p in g.pending_approvals()] == [aid]
    d2 = await g.check_action(a)                   # still pending: same approval id
    assert d2.escalate and a.meta["approval_id"] == aid
    assert g.approve(aid)
    assert (await g.check_action(a)).allow
    d3 = await g.check_action(a)                   # approval was single-use
    assert d3.escalate and a.meta["approval_id"] != aid
    assert not g.approve("nope")
    await k.clock.settle()
    assert await k.db.fetchval("SELECT status FROM gov_approvals WHERE id=?", (aid,)) == "used"
    evs = await events(k, EV.GOV_DECISION)
    assert any(e.get("escalate") for e in evs)


async def test_t4_self_modification_gates_and_protected_targets(k):
    g = k.gov
    d = await g.check_action(act("self_modify", T4, meta={"layer": "L2", "target": "skills/summarize"}))
    assert not d.allow and d.rule == "T4_gate"
    assert (await g.check_action(act("self_modify", T4, meta={"layer": "L2", "gate_passed": True}))).allow
    d = await g.check_action(act("self_modify", T4, meta={"layer": "identity", "target": "identity/narrative.md"}))
    assert d.escalate
    d = await g.check_action(act("self_modify", T4, meta={"layer": "L2", "gate_passed": True,
                                                          "path": "alive/governance/engine.py"}))
    assert not d.allow and d.rule == "protected_target"
    d = await g.check_action(act("self_modify", T4, meta={"target": "eval/frozen/suite.yaml", "gate_passed": True}))
    assert d.rule == "protected_target"


async def test_kill_switch_methods_and_file(k):
    g = k.gov
    assert not g.frozen and not g.learning_paused
    g.freeze("owner pressed stop")
    assert g.frozen and g.learning_paused
    d = await g.check_action(act())
    assert not d.allow and d.rule == "kill_switch"
    assert (await g.check_action(act("write_note", T1))).allow      # System-1 keeps recording
    assert not (await g.check_action(act("learn", T1))).allow       # but no learning
    assert not (await g.pre_send("tg:1", "tg:L", "好的", {})).allow
    assert not g.initiative_allowed("tg:L", k.clock.now()).allow
    with pytest.raises(BudgetDenied):
        g.route_hook(LLMRequest(purpose="s2.reply", tier="deliberate"), "deliberate")
    assert g.route_hook(LLMRequest(purpose="s1.read", tier="deliberate"), "deliberate") == "fast"
    g.unfreeze()
    assert not g.frozen
    # file-based switch (outside the individual's process, INV-12)
    path = os.path.join(k.data_dir, "FROZEN")
    open(path, "w").close()
    assert g.refresh_kill_switch() and g.frozen
    g.unfreeze()
    assert g.frozen                                   # the file wins
    os.remove(path)
    g.refresh_kill_switch()
    assert not g.frozen
    ops = [e.get("op") for e in await events(k, EV.GOV_DECISION)]
    assert "freeze" in ops and "unfreeze" in ops


async def test_kill_switch_persisted(tmp_path):
    k = await govhelp.kernel(tmp_path)
    k.gov.freeze("test")
    await k.stop()
    k2 = await govhelp.kernel(tmp_path)
    try:
        assert k2.gov.frozen
    finally:
        await k2.stop()


async def test_high_negative_arousal_extra_confirmation(k):
    g = k.gov
    k.affect.high_negative_arousal = lambda: True
    d = await g.check_action(act(initiated_by="reactive"))
    assert d.allow and d.rule == "affect_recheck"
    d = await g.check_action(act("initiate", T2, initiated_by="initiative", person_key="tg:L"))
    assert not d.allow and d.rule == "affect_defer"
    d = await g.check_action(act("delete_external", T3))
    assert not d.allow and not d.escalate and d.rule == "affect_defer"
    assert (await g.check_action(act("write_note", T1))).allow


async def test_autonomous_cycles_have_no_count_cap_only_the_budget(k):
    """INV-31 / docs/09 §4: no 'n autonomous cycles per hour'; how often I act on my own is the mind's autonomy
    hazard. Governance only enforces the spending budget."""
    g = k.gov
    c = lambda: act("autonomous_cycle", T0, initiated_by="internal")  # noqa: E731
    allowed = [(await g.check_action(c())).allow for _ in range(60)]
    assert all(allowed)
    led = k.llm.ledger
    led.day, led.today_usd = k.clock.today(), g.daily_usd * 1.01
    d = await g.check_action(c())
    assert not d.allow and d.rule == "budget"


async def test_posting_cap_and_channel_enablement(tmp_path):
    k = await govhelp.kernel(tmp_path, cfg={"governance": {"enabled_channels": ["tg"]}})
    try:
        g = k.gov
        for _ in range(g.fuse_n):                              # no daily posting quota; only the fuse
            assert (await g.check_action(act("post", T2, thread_id="tg:channel"))).allow
        d = await g.check_action(act("post", T2, thread_id="tg:channel"))
        assert not d.allow and d.rule == "fuse"
        d = await g.check_action(act("send_message", T2, thread_id="qq:123"))
        assert not d.allow and d.rule == "channel"
    finally:
        await k.stop()


async def test_initiative_acts_go_through_initiative_limits(k):
    g = k.gov
    d = await g.check_action(act("initiate", T2, initiated_by="initiative", person_key="tg:new"))
    assert not d.allow and d.rule == "first_contact"
    g.set_trust("tg:L", "contact")
    assert (await g.check_action(act("initiate", T2, initiated_by="initiative", person_key="tg:L"))).allow
