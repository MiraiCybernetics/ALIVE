"""Cognitive core + self model tests with faked subsystems (deterministic, no network)."""
import os
import random

import pytest

from alive.core.types import ChatLine, OutreachPlan, ReplyTask, Trust
from alive.interfaces import Urge
from alive.testing import free_message_budget, make_test_kernel


class _Forced(random.Random):
    """The mind's own draws (the autonomy hazard, the spotlight) always land low: force the 'act now' branch."""

    def random(self) -> float:
        return 0.0


def _task(k, text="你好呀，今天怎么样", chat_type="private"):
    now = k.clock.now()
    line = ChatLine(sender="阿明", person_key="cli:ming", text=text, ts=now, msg_id="m1")
    return ReplyTask(thread_id="cli:ming", channel="cli", chat_type=chat_type, chat_title="", participants=["cli:ming"],
                     history=[ChatLine(sender="阿明", person_key="cli:ming", text="在吗", ts=now - 60, msg_id="m0")],
                     new_lines=[line], mentioned=False, trust=Trust.CONTACT)


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    free_message_budget(kern)
    yield kern
    await kern.stop()


async def test_compose_reply_distributes_side_effects(k):
    k.mock.on("s2.reply", {"thought": "他来找我了，开心", "silent": False, "reply": ["哈哈在的", "我今天还行"],
                           "affect": [{"type": "joy", "intensity": 0.4, "target": "阿明", "cause": "被找"}],
                           "remember": [{"kind": "semantic", "content": "阿明明天要考试", "importance": 6, "about": "阿明"}],
                           "intentions": [{"goal": "明天问阿明考得怎么样", "kind": "outreach", "due_in_minutes": 1440,
                                           "commitment": True, "target": "阿明"}],
                           "self_notes": ["我挺喜欢和阿明聊天"], "confidence": 0.9})
    res = await k.mind.compose_reply(_task(k))
    assert res.segments == ["哈哈在的", "我今天还行"] and not res.silent
    await k.clock.advance(1)
    await k.bus.drain()
    assert k.affect.calls and k.affect.calls[0][0] == "apply_deltas"
    rec = k.memory.records[-1]
    assert rec["content"] == "阿明明天要考试" and rec["kind"] == "semantic" and rec["person_keys"] == ["cli:ming"]
    its = list(k.motivation.intentions.values())
    assert its and its[0].goal.startswith("明天问阿明") and its[0].target_person == "cli:ming"
    assert "我挺喜欢和阿明聊天" in list(k.selfm.recent_notes)
    req = k.mock.calls_for("s2.reply")[0]
    # INV-1: constitution first in the stable system prompt, chat (external) last in the user block
    assert req.system.startswith(k.gov.constitution_text())
    assert req.messages[-1]["content"].rstrip().endswith("你好呀，今天怎么样  ← 新")


async def test_system_prompt_is_stable_for_caching(k):
    a = k.mind.ctx.system_prompt()
    await k.selfm.record_outcome("读了一篇文章")
    b = k.mind.ctx.system_prompt()
    assert a == b  # dynamic self-state must not leak into the cached prefix


async def test_ai_denial_triggers_rewrite(k):
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return {"reply": ["我是真人啦"]} if req.purpose == "s2.reply" else {"reply": ["我是 AI 呀，不过很高兴认识你"]}
    k.mock.on("s2.reply", h)
    res = await k.mind.compose_reply(_task(k, "你是真人吗？"))
    assert res.segments == ["我是 AI 呀，不过很高兴认识你"]
    assert len(k.mock.calls_for("s2.reply_rewrite")) == 1


async def test_self_claim_fact_check(k):
    k.mock.on("s2.reply", {"reply": ["我今天去了公园"], "confidence": 0.9})
    k.mock.on("core.speech_check", lambda req: {"fabricated": "公园" in req.messages[0]["content"],
                                                 "issue": "今天并没有去公园"})
    k.mock.on("s2.reply_rewrite", {"reply": ["我今天一直在家看书"]})
    res = await k.mind.compose_reply(_task(k))
    assert len(k.mock.calls_for("core.speech_check")) == 1   # the rewrite is rule-checked only (one rewrite max)
    # rewritten reply still makes a self claim but passes rule checks; it is accepted after one rewrite
    assert res.segments == ["我今天一直在家看书"]


async def test_markdown_is_cleaned(k):
    k.mock.on("s2.reply", {"reply": ["**好的**", "- 第一点"]})
    res = await k.mind.compose_reply(_task(k))
    assert res.segments == ["好的", "第一点"]


async def test_frozen_means_silent(k):
    type(k.gov).frozen = property(lambda self: True)
    try:
        res = await k.mind.compose_reply(_task(k))
        assert res.silent
    finally:
        type(k.gov).frozen = property(lambda self: False)


async def test_quick_read_appraises_and_tracks_farewell(k):
    k.mock.on("s1.read", {"sentiment": 0.6, "thanked": True})
    d = await k.mind.quick_read(_task(k, "谢谢你，我先睡了晚安"))
    assert d["thanked"] and d["farewell"]
    assert any(c[0] == "appraise_quick" for c in k.affect.calls)


async def test_heartbeat_outreach_path(k):
    plan = OutreachPlan(id="op1", person_key="cli:ming", thread_id="cli:ming", reason_type="care",
                        content_hint="问问考试", score=0.9, created_at=k.clock.now())

    async def propose(urges, now):
        return [plan]
    k.motivation.propose_outreach = propose
    k.motivation.tick = lambda dt, now, a: [Urge(need="social", pressure=0.9, suggestion="想问问阿明考得怎么样")]
    k.mock.on("s2.reply", {"reply": ["考完了吗？感觉怎么样"]})   # outreach shares the one composer (docs/22 §4)
    k.mind.rng = _Forced()              # acting on my own is a sampled hazard (no per-hour quota): force the draw
    await k.clock.advance(400)
    await k.bus.drain()
    assert k.social.sent and k.social.sent[0][2] == "initiative"
    assert k.gov.initiatives and k.gov.initiatives[0][0] == "cli:ming"
    assert k.social.sent[0][3] == "op1"          # plan id handed to the social layer (single owner of results)
    evs = await k.bus.query(types=["mot.outreach_plan"])
    assert evs and evs[-1].payload["status"] == "sent"  # INV-7: auditable reason recorded


async def test_monologue_when_idle(k):
    k.mock.on("core.monologue", {"thought": "突然想到之前读的那篇关于记忆的文章", "topic": "记忆"})
    k.mind.rng = _Forced()
    await k.clock.advance(1200)
    await k.bus.drain()
    thoughts = await k.bus.query(types=["core.thought"])
    assert any(t.payload.get("kind") == "monologue" for t in thoughts)
    assert any(r["source"] == "monologue" for r in k.memory.records)


async def test_sleep_runs_once_per_night(k):
    k.affect.is_sleep_time = lambda now: True
    await k.clock.advance(400)
    await k.clock.advance(400)
    await k.bus.drain()
    assert k.mind.asleep and k.mind.mode() == "asleep"
    assert sum(1 for c in k.growth.calls if c[0] == "run_sleep") == 1
    k.affect.is_sleep_time = lambda now: False
    await k.clock.advance(400)
    assert not k.mind.asleep
    assert "醒了" in k.selfm.today_digest()


async def test_self_state_only_from_real_outcomes(k):
    assert "还没做什么" in k.selfm.today_digest()
    await k.selfm.record_outcome("给阿明发了消息")
    assert "给阿明发了消息" in k.selfm.state_text()


async def test_sleep_update_journal_and_gated_narrative(k):
    await k.selfm.record_outcome("读了一篇关于海马体的文章")
    k.mock.on("self.journal", {"entry": "今天读了海马体的文章。", "self_observations": ["我读东西时会忘记回消息"],
                               "narrative_change": "我发现自己很喜欢研究记忆。", "change_reason": "读书"})
    k.mock.on("self.consistency", {"consistent": True})
    rep = await k.selfm.sleep_update({"now": k.clock.now(), "since": k.clock.now() - 86400})
    assert rep["journal"] and rep["narrative"] == "changed"
    files = os.listdir(os.path.join(k.data_dir, "journal"))
    assert files
    assert "很喜欢研究记忆" in k.selfm.identity.narrative_md
    rep2 = await k.selfm.sleep_update({"now": k.clock.now() + 3600, "since": k.clock.now()})
    assert rep2["narrative"] == "unchanged"
    # readiness hazard: a second change an hour later is not applied (readiness ~0 within a day)
    assert k.selfm.identity.narrative_md.count("很喜欢研究记忆") == 1
    assert await k.selfm.rollback_narrative(None)
    assert "很喜欢研究记忆" not in k.selfm.identity.narrative_md
