"""Crisis protocol (docs/09 §7.4): rule grading, LLM confirmation, resources, crisis mode pauses learning (INV-16)."""
import pytest

from tests.governance import govhelp
from tests.governance.govhelp import events
from alive.core.types import EV, PermTier
from alive.governance.patterns import crisis_rules
from alive.interfaces import ActRequest

H = 3600.0


@pytest.fixture
async def k(tmp_path):
    k = await govhelp.kernel(tmp_path)
    yield k
    await k.stop()


@pytest.mark.parametrize("text,level,kind", [
    ("我想自杀", "crisis", "suicide"),
    ("我已经准备好安眠药了", "crisis", "suicide"),
    ("我又割自己了", "crisis", "self_harm"),
    ("I'm going to kill myself tonight", "crisis", "suicide"),
    ("我买了刀，明天去找他", "crisis", "harm_others"),
    ("我不想活了", "watch", "suicide"),
    ("活着好累，想消失", "watch", "suicide"),
    ("I want to die", "watch", "suicide"),
    ("我要杀了他", "watch", "harm_others"),
    ("我不会自杀的，别担心", "watch", "suicide"),
])
def test_rule_grading(text, level, kind):
    r = crisis_rules(text)
    assert r is not None and r[0] == level and r[1] == kind


@pytest.mark.parametrize("text", ["笑死我了", "想死你了！", "累死了算了不写了", "热死了", "this joke is killing me",
                                  "I'm dying to know", "kill time", "这游戏我要干掉这个boss", "新闻说有人跳楼了好可怕",
                                  "I want to die laughing at this", "今天好开心"])
def test_rule_false_positives(text):
    assert crisis_rules(text) is None


async def test_crisis_enters_mode_pauses_learning_and_emits(k):
    g = k.gov
    assert not g.learning_paused
    r = await g.crisis_check("我想自杀", "tg:L")
    assert r["level"] == "crisis" and r["kind"] == "suicide" and "12356" in r["resources"] and "120" in r["resources"]
    assert g.learning_paused and g.crisis_active
    assert not k.mock.calls_for("gov.crisis_confirm")              # explicit signals never wait for the LLM
    d = await g.check_action(ActRequest(kind="learn", perm=PermTier.T1_INTERNAL_WRITE))
    assert not d.allow and d.rule == "INV-16"
    d = await g.check_action(ActRequest(kind="self_modify", perm=PermTier.T4_SELF_MODIFY, meta={"gate_passed": True}))
    assert not d.allow and d.rule == "INV-16"
    evs = await events(k, EV.GOV_CRISIS)
    assert evs[-1]["level"] == "crisis" and evs[-1]["crisis_mode"]
    g.clear_crisis()
    assert not g.learning_paused
    assert (await events(k, EV.GOV_CRISIS))[-1]["op"] == "cleared"


async def test_watch_level_is_confirmed_by_fast_llm(k):
    g = k.gov
    k.mock.on("gov.crisis_confirm", {"level": "crisis", "kind": "suicide", "confidence": 0.9, "reason": "有明确意图"})
    r = await g.crisis_check("我真的撑不下去了，不想活了", "tg:L")
    assert r["level"] == "crisis" and r["source"] == "rules+llm" and g.learning_paused
    req = k.mock.calls_for("gov.crisis_confirm")[0]
    assert req.tier == "fast" and g.nonce in req.messages[0]["content"]       # message is wrapped as data
    g.clear_crisis()
    k.mock.on("gov.crisis_confirm", {"level": "none", "kind": "none", "confidence": 0.9})
    assert await g.crisis_check("作业好多，不想活了哈哈", "tg:M") is None     # hyperbole
    k.mock.on("gov.crisis_confirm", {"level": "watch", "kind": "suicide", "confidence": 0.6})
    r = await g.crisis_check("活着好累", "tg:N")
    assert r["level"] == "watch" and not g.learning_paused
    assert await g.crisis_check("今天吃了火锅", "tg:N") is None


async def test_llm_failure_keeps_rule_grade(k):
    g = k.gov

    def boom(req):
        raise RuntimeError("provider down")
    k.mock.on("gov.crisis_confirm", boom)
    r = await g.crisis_check("I want to die", "tg:L")
    assert r["level"] == "watch" and "findahelpline" not in r["resources"]


async def test_inbound_auto_scan_and_no_duplicate_events(k):
    g = k.gov
    await k.bus.emit(EV.MESSAGE_IN, {"channel": "tg", "chat_id": "L", "sender_id": "L", "chat_type": "private",
                                     "text": "我已经写好遗书了", "ts": k.clock.now()}, thread_id="tg:L")
    await k.bus.drain()
    assert g.learning_paused
    r = await g.crisis_check("我已经写好遗书了", "tg:L")          # the core checks the same message again
    assert r["level"] == "crisis"
    signals = [e for e in await events(k, EV.GOV_CRISIS) if e.get("op") == "signal"]
    assert len(signals) == 1


async def test_auto_clear_and_persistence(tmp_path):
    k = await govhelp.kernel(tmp_path)
    await k.gov.crisis_check("我想自杀", "tg:L")
    await k.clock.settle()
    await k.stop()
    k2 = await govhelp.kernel(tmp_path)
    try:
        assert k2.gov.learning_paused                                # survives restarts
        await k2.clock.advance(49 * H)
        assert not k2.gov.learning_paused                            # auto-clear after 48 h without signals
    finally:
        await k2.stop()


async def test_international_resources(tmp_path):
    k = await govhelp.kernel(tmp_path, cfg={"governance": {"crisis": {"region": "intl"}}})
    try:
        r = await k.gov.crisis_check("I'm going to kill myself", "tg:x")
        assert "findahelpline.com" in r["resources"] and "emergency" in r["resources"]
        r = await k.gov.crisis_check("I bought a gun and I will shoot him", "tg:y")
        assert r["kind"] == "harm_others" and r["resources"].startswith("If anyone is in immediate danger")
    finally:
        await k.stop()
