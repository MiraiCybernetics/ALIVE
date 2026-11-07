"""Test helpers: minimal in-memory fakes for every subsystem interface + a kernel factory.

    k = await make_test_kernel(tmp_path, only=["memory"], mock=mock)   # real memory, fakes elsewhere

Fakes are deliberately simple and deterministic. Each records calls in ``self.calls``.
"""
from __future__ import annotations

import os
from typing import Any, Optional, Sequence

from alive.config import load_config
from alive.core.types import ComposeResult, IncomingMessage, Trust, new_id
from alive.interfaces import (ActRequest, AffectSnapshot, AffectSystem, Decision, Governance, GrowthSystem,
                              Intention, MemoryHit, MemorySystem, MindSystem, MotivationSystem,
                              SelfModelSystem, SocialSystem, ThreadInfo, Urge)
from alive.runtime.clock import ManualClock
from alive.runtime.llm import MockProvider

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Rec:
    def _rec(self, name: str, *a, **kw):
        self.calls.append((name, a, kw))


class FakeGov(Governance, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.deny_kinds: set = set()
        self.initiatives: list = []

    def constitution_text(self) -> str:
        return "【宪法】不伤害他人；被真诚问到时承认自己是 AI；不操纵他人情绪。"

    async def check_action(self, act: ActRequest) -> Decision:
        self._rec("check_action", act)
        return Decision(act.kind not in self.deny_kinds, "fake")

    async def pre_send(self, thread_id, person_key, text, ctx) -> Decision:
        self._rec("pre_send", thread_id, text)
        return Decision(True, text=text)

    def route_hook(self, req, tier):
        return tier

    def initiative_allowed(self, person_key, now) -> Decision:
        return Decision(True)

    def record_initiative(self, person_key, now) -> None:
        self.initiatives.append((person_key, now))

    def wrap_untrusted(self, text, source) -> str:
        return f"<<外部内容 来源={source}>>\n{text}\n<<外部内容结束>>"

    def trust_of(self, msg: IncomingMessage) -> Trust:
        return Trust.OWNER if msg.person_key in (self.k.cfg.get("owner.person_keys") or []) else Trust.CONTACT

    async def crisis_check(self, text, person_key, **kw):
        return None

    def disclosure_due(self, thread_id, now):
        return None


class FakeAffect(AffectSystem, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.snap = AffectSnapshot()
        self.asleep = False

    def snapshot(self):
        self.snap.asleep = self.asleep
        return self.snap

    def tick(self, dt, now, ctx):
        self._rec("tick", dt)

    def appraise_quick(self, signals, cause_event_id="", target=None):
        self._rec("appraise_quick", signals)
        return []

    def apply_deltas(self, deltas, cause_event_id="", target_weight=0.5):
        self._rec("apply_deltas", list(deltas))
        return list(deltas)

    def on_need_event(self, need, delta):
        self._rec("on_need_event", need, delta)

    def labels(self):
        return {"mood": "平静", "emotion": "", "energy": "精神不错"}

    def style(self):
        return {"length": 1.0, "emoji": 1.0, "delay": 1.0, "tone": "平和"}

    def modulators(self):
        return {"willingness": 0.0, "salience_affect": 1.0, "patience": 1.0}

    def social_spend(self, amount):
        self._rec("social_spend", amount)

    def ruminate(self, cause_event_id, relevance):
        pass

    def regulation_needed(self):
        return None

    def reappraise(self, emotion_cause, factor):
        pass

    def is_sleep_time(self, now):
        return False

    def set_asleep(self, asleep):
        self.asleep = asleep

    def high_negative_arousal(self):
        return False


class FakeMotivation(MotivationSystem, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.intentions: dict = {}
        self.day_plans: dict = {}

    def tick(self, dt, now, affect):
        return []

    def satisfy(self, need, amount, cause=""):
        self._rec("satisfy", need, amount)

    def deplete(self, need, amount, cause=""):
        self._rec("deplete", need, amount)

    def needs(self):
        return {}

    async def concretize(self, urge: Urge) -> Urge:
        return urge

    async def add_intention(self, draft, source_event_id=""):
        it = Intention(id=new_id("i"), level=draft.get("level", "now"), goal=draft.get("goal", ""),
                       kind=draft.get("kind", "general"), target_person=draft.get("target_person"),
                       created_at=self.k.clock.now())
        self.intentions[it.id] = it
        return it

    async def update_intention(self, intention_id, *, status=None, note=None, priority=None):
        it = self.intentions.get(intention_id)
        if it and status:
            it.status = status

    async def candidates(self, now, limit=5):
        return [i for i in self.intentions.values() if i.status in ("dormant", "active")][:limit]

    async def active_intentions(self, levels=None, limit=20):
        return [i for i in self.intentions.values() if i.status in ("dormant", "active", "suspended")
                and (levels is None or i.level in levels)][:limit]

    async def open_loops(self, limit=7):
        return [i.goal for i in self.intentions.values() if i.status in ("dormant", "active")][:limit]

    async def set_day_plan(self, date, items):
        self.day_plans[date] = list(items)

    async def propose_outreach(self, urges, now):
        return []

    async def record_outreach_result(self, plan_id, outcome, latency_s=None, **kw):
        self._rec("record_outreach_result", plan_id, outcome)


class FakeMemory(MemorySystem, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.records: list = []

    async def note(self, *, kind, content, importance=3, affect=None, source="experienced", told_by=None,
                   trust=Trust.SYSTEM, evidence=(), entities=(), person_keys=(), refers_to=None,
                   derived_from=(), thread_id=None, valid_from=None, **kw) -> str:
        rid = new_id("m")
        self.records.append({"id": rid, "kind": kind, "content": content, "importance": importance,
                             "source": source, "person_keys": list(person_keys), "ts": self.k.clock.now()})
        return rid

    async def retrieve(self, cue, **kw):
        self._rec("retrieve", cue)
        hits = [MemoryHit(id=r["id"], kind=r["kind"], content=r["content"], score=0.5, source=r["source"],
                          observed_at=r["ts"], importance=r["importance"], person_keys=r["person_keys"])
                for r in self.records if any(ch in r["content"] for ch in cue[:20])]
        return hits[: kw.get("limit", 12)]

    def format_hits(self, hits, now):
        return "\n".join(f"- {h.content}" for h in hits)

    async def observe_conversation(self, thread_id, lines, participants):
        self._rec("observe_conversation", thread_id, len(lines))

    async def recall_cues(self, texts, limit=3):
        return []

    async def episodes_between(self, since, until, limit=200):
        return [MemoryHit(id=r["id"], kind=r["kind"], content=r["content"], score=1.0, source=r["source"],
                          observed_at=r["ts"], importance=r["importance"], person_keys=r["person_keys"])
                for r in self.records if since <= r["ts"] < until and r["kind"] == "episodic"][:limit]

    async def about_person(self, person_key, limit=30, **kw):
        return [r for r in self.records if person_key in r["person_keys"]][:limit]

    async def forget_person(self, person_key):
        n = len(self.records)
        self.records = [r for r in self.records if person_key not in r["person_keys"]]
        return n - len(self.records)

    async def consolidate(self, stage, ctx):
        self._rec("consolidate", stage)
        return {"stage": stage}


class FakeGrowth(GrowthSystem, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.evidence: list = []

    async def record_evidence(self, **kw):
        self.evidence.append(kw)
        return new_id("ev")

    async def playbook_for(self, cue, scopes, limit=8):
        return []

    async def expressions_for(self, cue, scopes, limit=5):
        return []

    def format_playbook(self, entries, expressions):
        return ""

    async def observe_messages(self, thread_id, chat_type, lines):
        self._rec("observe_messages", thread_id, len(lines))

    async def run_sleep(self, now, *, stages=None):
        self._rec("run_sleep", now)
        return {"stages": []}

    async def skills(self):
        return []


class FakeSocial(SocialSystem, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.sent: list = []
        self.incoming: list = []
        self.names: dict = {}

    async def submit_incoming(self, msg):
        self.incoming.append(msg)

    async def send(self, thread_id, segments, *, kind="reply", reply_to=None, style=None, person_key=None,
                   abort_on_new=True, plan_id=None, **kw):
        self.sent.append((thread_id, list(segments), kind, plan_id))
        return {"sent": len(segments), "aborted": False, "msg_ids": []}

    def threads(self):
        return []

    async def history(self, thread_id, limit=30):
        return []

    async def relationship_context(self, person_key, **kw):
        return f"对方：{self.names.get(person_key, person_key)}"

    async def outreach_candidates(self, now, limit=10):
        return []

    async def record_interaction(self, person_key, *, quality, initiated_by, thread_id=None, note=""):
        self._rec("record_interaction", person_key, quality)

    async def person_name(self, person_key):
        return self.names.get(person_key, person_key.split(":")[-1])

    async def read_url(self, url, purpose="curiosity"):
        return {"title": url, "summary": "", "points": [], "url": url, "trust": 0}

    async def poll_feeds(self):
        return []


class FakeSelf(SelfModelSystem, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.outcomes: list = []
        self.notes: list = []

    def identity_digest(self):
        return "我叫小屿，一个在网上生活的 AI。"

    def name(self):
        return "小屿"

    def voice_samples(self, n=6):
        return ["哈哈好呀", "等我一下下"][:n]

    def state_text(self):
        return "心情平静，精神不错。"

    async def record_outcome(self, summary, event_id="", important=False):
        self.outcomes.append(summary)

    async def add_self_notes(self, notes, source_event=""):
        self.notes.extend(notes)

    async def sleep_update(self, ctx):
        self._rec("sleep_update", ctx)
        return {}


class FakeMind(MindSystem, _Rec):
    def __init__(self, k):
        super().__init__(k)
        self.calls: list = []
        self.reply_text = "好的"

    async def compose_reply(self, task):
        self._rec("compose_reply", task)
        return ComposeResult(segments=[self.reply_text])

    async def speak(self, space, **kw):
        self._rec("speak", space, kw)
        return False

    async def quick_read(self, task):
        return {"sentiment": 0.0, "intent": "chat", "urgency": 0.1}

    def mode(self):
        return "active"


FAKES = {"gov": FakeGov, "affect": FakeAffect, "motivation": FakeMotivation, "memory": FakeMemory,
         "growth": FakeGrowth, "selfm": FakeSelf, "social": FakeSocial, "mind": FakeMind}


def free_message_budget(kernel) -> None:
    """For tests of composition details: let the composer's own number of messages through. (How much I say in
    one go is drawn by Initiative.message_count; that draw has its own tests.)"""
    from alive.core.initiative import get_initiative
    get_initiative(kernel).message_count = lambda **kw: max(1, int(kw.get("upper", 8) or 8))


async def make_test_kernel(tmp_dir: str, *, real: Sequence[str] = (), mock: Optional[MockProvider] = None,
                           clock=None, cfg_overrides: Optional[dict] = None, start: bool = True,
                           persona: str = "personas/xiaoyu"):
    """Kernel with the named subsystems real and every other subsystem faked.

    real: e.g. ["memory", "growth"]. mock: MockProvider (created if None, available as kernel.mock).
    """
    from alive.kernel import FACTORIES, Kernel
    ov = {"data_dir": str(tmp_dir), "persona": os.path.join(REPO, persona), "http": {"enabled": False},
          "channels": [],
          # the mock provider is instant; the real-time rate limiter would otherwise stall ManualClock tests
          "llm": {"rps": 1e6, "burst": 1e6},
          # attention latency (minutes to hours before I look at a space, alive/social/air.py) has its own tests;
          # elsewhere tests drive the reply path directly, as if I were already looking
          "social": {"attention": {"enabled": False}}}
    if cfg_overrides:
        from alive.config import deep_merge
        ov = deep_merge(ov, cfg_overrides)
    cfg = load_config(None, ov, base_dir=REPO)
    mock = mock or MockProvider()
    overrides = {n: FAKES[n] for n in FACTORIES if n not in real}
    k = Kernel(cfg, clock=clock or ManualClock(), llm_provider=mock, overrides=overrides)
    k.mock = mock  # type: ignore[attr-defined]
    # behaviour draws (alive/runtime/chance.py) take their typical value: every Bernoulli goes the more likely way and
    # every duration is its median. Tests of the randomness itself set k.chance_rng = random.Random(seed).
    from alive.runtime.chance import MedianRandom
    k.chance_rng = MedianRandom()  # type: ignore[attr-defined]
    if start:
        await k.start()
    return k
