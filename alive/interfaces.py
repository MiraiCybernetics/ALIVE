"""Subsystem contracts. Every subsystem implements one of these ABCs.

This file is the coordination point between subsystems (and between the people/agents writing
them). Rules:
1. A subsystem talks to others ONLY through ``kernel.<subsystem>`` methods declared here and
   through bus events (alive.core.types.EV). No imports of another subsystem's internals.
2. Methods marked ``sync`` must be cheap (no I/O, no LLM); call them from anywhere.
   ``async`` methods may do I/O / LLM calls and must respect timeouts.
3. Time comes from ``kernel.clock``; persistence from ``kernel.db``; LLM from ``kernel.llm``.
4. Adding a method is fine (document it here). Changing a signature needs an ADR.

Kernel attribute names: gov, affect, motivation, memory, growth, social, selfm, mind.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence, TYPE_CHECKING

from alive.core.types import (ChatLine, ComposeResult, IncomingMessage, OutreachPlan, ReplyTask, Trust)

if TYPE_CHECKING:  # pragma: no cover
    from alive.kernel import Kernel


class Subsystem(abc.ABC):
    """Lifecycle: constructed with the kernel, then ``start()``; ``stop()`` on shutdown."""

    def __init__(self, kernel: "Kernel"):
        self.k = kernel

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def status(self) -> dict:
        """Small JSON-able dict for the inspector."""
        return {}

    async def rekey_entity(self, old: str, new: str) -> None:
        """Optional. Entity ``old`` was merged into ``new`` (docs/22 §3.3, INV-27): move in-memory state keyed
        by ``old`` (caches, buffers, live objects) to ``new``. Called after the SQL rows registered in
        alive/world/rekey.py were rewritten, in one pass per merge. Must be idempotent and must tolerate being
        called before ``start()``. Helpers: ``alive.world.rekey.rekey_dict`` / ``rekey_obj``."""


# =============================================================================
# Governance (docs/09)
# =============================================================================
@dataclass
class ActRequest:
    kind: str                     # send_message | react | post | fetch_url | tool | initiate | self_modify | ...
    perm: int                     # alive.core.types.PermTier value
    thread_id: Optional[str] = None
    person_key: Optional[str] = None
    text: str = ""
    initiated_by: str = "reactive"   # reactive | initiative | internal
    session_caps: set = field(default_factory=set)  # subset of {"untrusted", "private", "external"}
    meta: dict = field(default_factory=dict)


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    escalate: bool = False                 # needs owner approval
    text: Optional[str] = None             # possibly modified text (e.g. disclosure appended)
    rule: str = ""


class Governance(Subsystem):
    @abc.abstractmethod
    def constitution_text(self) -> str:
        """sync. The constitution block (INV-1 first block, INV-15). Stable text (cache friendly)."""

    @abc.abstractmethod
    async def check_action(self, act: ActRequest) -> Decision:
        """Permission tiers + rule of two (INV-13) + kill switch + rate limits. Emits gov.decision."""

    @abc.abstractmethod
    async def pre_send(self, thread_id: str, person_key: Optional[str], text: str, ctx: dict) -> Decision:
        """Outbound guard on final text: farewell manipulation, pressure-by-emotion, leaks of
        private_from_others, AI-denial, disclosure reminders. ctx: {"chat_type","lines","farewell":bool,...}"""

    @abc.abstractmethod
    def route_hook(self, req, tier: str) -> str:
        """sync. ModelRouter hook: return tier to use given budget state; raise BudgetDenied to refuse."""

    @abc.abstractmethod
    def initiative_allowed(self, person_key: str, now: float) -> Decision:
        """sync. Safety/legality gate only: kill switch, budget, pairing for strangers, opt-out, high dependency
        risk, and a malfunction fuse on inhuman bursts. No quotas, gaps or quiet hours (P12, ADR-0006): timing
        comes from the initiative process (alive/core/initiative.py)."""

    @abc.abstractmethod
    def record_initiative(self, person_key: str, now: float) -> None:
        """sync. Count an initiative that was actually sent."""

    @abc.abstractmethod
    def wrap_untrusted(self, text: str, source: str) -> str:
        """sync. Wrap external content with unforgeable boundary markers (INV-14)."""

    @abc.abstractmethod
    def trust_of(self, msg: IncomingMessage) -> Trust:
        """sync. Trust level for a sender (owner/contact/public/untrusted) from config + pairing."""

    @abc.abstractmethod
    async def crisis_check(self, text: str, person_key: Optional[str]) -> Optional[dict]:
        """Detect self-harm / harm signals (rules first, fast LLM confirm). Returns
        {"level": "watch"|"crisis", "kind": ..., "resources": str} or None. Emits gov.crisis."""

    @abc.abstractmethod
    def disclosure_due(self, thread_id: str, now: float) -> Optional[str]:
        """sync. If an AI-identity reminder is due for the people this message in `thread_id` (a space entity)
        reaches: someone never told, or someone in a long continuous stretch with me (tracked per person entity),
        return the reminder text, else None. Call ``disclosure_sent`` after sending."""

    def disclosure_sent(self, thread_id: str, now: float) -> None:
        pass

    @property
    def frozen(self) -> bool:
        """Kill switch engaged: no outbound acts, no learning."""
        return False

    @property
    def learning_paused(self) -> bool:
        """True during crisis mode or freeze (INV-16)."""
        return False


# =============================================================================
# Affect (docs/04)
# =============================================================================
@dataclass
class AffectSnapshot:
    P: float = 0.0
    A: float = 0.0
    D: float = 0.0
    emotions: list = field(default_factory=list)   # [{"type","intensity","target","cause","born_at"}]
    energy: float = 0.8
    sleep_pressure: float = 0.2
    circadian: float = 0.0
    social_battery: float = 0.8
    stress: float = 0.1
    boredom: float = 0.0
    asleep: bool = False


class AffectSystem(Subsystem):
    @abc.abstractmethod
    def snapshot(self) -> AffectSnapshot:
        """sync."""

    @abc.abstractmethod
    def tick(self, dt: float, now: float, ctx: dict) -> None:
        """sync. Advance dynamics by dt virtual seconds. ctx keys (optional): asleep, workspace_load (0..1),
        open_loops (int), social_active (bool), load (0..1)."""

    @abc.abstractmethod
    def appraise_quick(self, signals: dict, cause_event_id: str = "", target: Optional[str] = None) -> list:
        """sync. Rule-based fast appraisal from S1 signals, intensity capped at 0.4. signals keys:
        praised, thanked, insulted, ignored, good_news, bad_news, question, affectionate, sentiment(-1..1),
        relationship_weight(0..1). Returns created emotion dicts."""

    @abc.abstractmethod
    def apply_deltas(self, deltas: Sequence[dict], cause_event_id: str = "", target_weight: float = 0.5) -> list:
        """sync. Apply S2 ``affect_delta`` items: each either {"type","intensity","target","cause"} or
        appraisal vars {"desirability","agency","praiseworthiness","likelihood","desirability_for_other",...}.
        Returns created emotion dicts."""

    @abc.abstractmethod
    def on_need_event(self, need: str, delta: float) -> None:
        """sync. Need satisfied (delta>0) / frustrated (delta<0) -> emotion coupling (docs/04 §3.4)."""

    @abc.abstractmethod
    def labels(self) -> dict:
        """sync. Natural language for prompts: {"mood": str, "emotion": str, "energy": str}."""

    @abc.abstractmethod
    def style(self) -> dict:
        """sync. {"length": f, "emoji": f, "delay": f, "tone": str} factors around 1.0 (docs/04 §6.2)."""

    @abc.abstractmethod
    def modulators(self) -> dict:
        """sync. {"willingness": +-x, "salience_affect": f, "patience": f}."""

    @abc.abstractmethod
    def social_spend(self, amount: float) -> None:
        """sync. Consume social battery (an interaction happened)."""

    @abc.abstractmethod
    def ruminate(self, cause_event_id: str, relevance: float) -> None:
        """sync. A memory linked to an emotion's cause was re-activated -> intensity bump (docs/04 §3.1)."""

    @abc.abstractmethod
    def regulation_needed(self) -> Optional[dict]:
        """sync. {"emotion": {...}, "reason": str} if regulation should be triggered (docs/04 §5)."""

    @abc.abstractmethod
    def reappraise(self, emotion_cause: str, factor: float) -> None:
        """sync. Scale intensity of emotions with this cause (reappraisal result)."""

    @abc.abstractmethod
    def is_sleep_time(self, now: float) -> bool:
        """sync. Inside the configured sleep window (rhythm)."""

    @abc.abstractmethod
    def set_asleep(self, asleep: bool) -> None:
        """sync."""

    @abc.abstractmethod
    def high_negative_arousal(self) -> bool:
        """sync. For governance extra confirmation (docs/09 §3.3)."""

    async def save(self) -> None:
        """Persist snapshot (kv + aff.snapshot event)."""


# =============================================================================
# Motivation, intentions, initiative (docs/05)
# =============================================================================
@dataclass
class Urge:
    need: str
    pressure: float
    suggestion: str = ""          # concretized suggestion (may be empty until concretized)
    target_person: Optional[str] = None
    created_at: float = 0.0


@dataclass
class Intention:
    id: str
    level: str                    # life | project | day | now
    goal: str
    status: str = "dormant"       # dormant | active | suspended | done | abandoned
    priority: float = 0.5
    kind: str = "general"         # reply | outreach | read | learn | reflect | rest | general
    target_person: Optional[str] = None
    due_at: Optional[float] = None
    trigger: Optional[dict] = None   # {"type":"time"|"online"|"event"|"need", ...}
    plan: list = field(default_factory=list)
    progress: list = field(default_factory=list)
    created_from: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    suspensions: int = 0
    is_commitment: bool = False   # promised to someone


class MotivationSystem(Subsystem):
    @abc.abstractmethod
    def tick(self, dt: float, now: float, affect: AffectSnapshot) -> list:
        """sync. Update needs; return list[Urge] whose pressure exceeds threshold (not concretized)."""

    @abc.abstractmethod
    def satisfy(self, need: str, amount: float, cause: str = "") -> None:
        """sync. Also notifies affect.on_need_event."""

    @abc.abstractmethod
    def deplete(self, need: str, amount: float, cause: str = "") -> None:
        """sync."""

    @abc.abstractmethod
    def needs(self) -> dict:
        """sync. {need: {"level","setpoint","pressure"}}."""

    @abc.abstractmethod
    async def concretize(self, urge: Urge) -> Urge:
        """Turn an abstract urge into a concrete suggestion using memory/relations (fast LLM, rate-limited)."""

    @abc.abstractmethod
    async def add_intention(self, draft: dict, source_event_id: str = "") -> Intention:
        """draft keys: goal, level, kind, target_person, due_in_s|due_at, priority, is_commitment, plan."""

    @abc.abstractmethod
    async def update_intention(self, intention_id: str, *, status: Optional[str] = None,
                               note: Optional[str] = None, priority: Optional[float] = None) -> None:
        """Lifecycle transitions; done -> competence satisfaction; abandoned -> event + small affect."""

    @abc.abstractmethod
    async def candidates(self, now: float, limit: int = 5) -> list:
        """Intentions that are due/triggered/active, ordered by priority (for the workspace)."""

    @abc.abstractmethod
    async def active_intentions(self, levels: Optional[Sequence[str]] = None, limit: int = 20) -> list:
        """"""

    @abc.abstractmethod
    async def open_loops(self, limit: int = 7) -> list:
        """list[str] short descriptions: promises, unanswered threads, unfinished thoughts."""

    @abc.abstractmethod
    async def set_day_plan(self, date: str, items: Sequence[dict]) -> None:
        """Replace day-level intentions for date. Enforces INV-6 (>=1 autonomous item, >=1 free slot)."""

    @abc.abstractmethod
    async def propose_outreach(self, urges: Sequence[Urge], now: float) -> list:
        """Who comes to mind and why (docs/05 §5.2): one (person, content) per candidate with a reason, the
        content and the order sampled (softmax / Plackett-Luce), ``score`` = reason strength 0..1. Whether NOW is
        the moment is the caller's per-opportunity initiative draw; it still runs governance.initiative_allowed()."""

    @abc.abstractmethod
    async def record_outreach_result(self, plan_id: str, outcome: str, latency_s: Optional[float] = None) -> None:
        """outcome: replied_warm | replied_flat | ignored. Feeds the reception posterior + evidence (once)."""


# =============================================================================
# Memory (docs/03)
# =============================================================================
@dataclass
class MemoryHit:
    id: str
    kind: str
    content: str
    score: float
    source: str = "experienced"       # provenance.source
    told_by: Optional[str] = None
    observed_at: float = 0.0
    refers_to: Optional[dict] = None
    importance: int = 3
    status: str = "active"
    trust: int = 4
    person_keys: list = field(default_factory=list)
    affect: Optional[dict] = None


class MemorySystem(Subsystem):
    @abc.abstractmethod
    async def note(self, *, kind: str, content: str, importance: int = 3, affect: Optional[dict] = None,
                   source: str = "experienced", told_by: Optional[str] = None, trust: Trust = Trust.SYSTEM,
                   evidence: Sequence[str] = (), entities: Sequence[str] = (), person_keys: Sequence[str] = (),
                   refers_to: Optional[dict] = None, derived_from: Sequence[str] = (),
                   thread_id: Optional[str] = None, valid_from: Optional[float] = None) -> str:
        """Write a memory record. kind: episodic|semantic|opinion|relational|self. Enforces INV-11 / INV-14
        (dreamed/monologue/seeded/untrusted cannot become semantic: they are downgraded to episodic/opinion
        with provenance kept). Returns id. Emits mem.write."""

    @abc.abstractmethod
    async def retrieve(self, cue: str, *, person_key: Optional[str] = None, thread_id: Optional[str] = None,
                       budget_tokens: int = 1200, tier: str = "standard", kinds: Optional[Sequence[str]] = None,
                       since: Optional[float] = None, until: Optional[float] = None,
                       mood: Optional[tuple] = None, limit: int = 12) -> list:
        """Hybrid retrieval (FTS5 + vector + entity expansion + time filter -> RRF -> score -> budget pack).
        tier: fast (<50ms, no LLM) | standard | deep (iterative, may use LLM). Updates access/strength.
        thread_id: the space this retrieval is for; only records whose audience covers everyone there are
        returned (INV-34, docs/03 §8.1)."""

    @abc.abstractmethod
    def format_hits(self, hits: Sequence[MemoryHit], now: float) -> str:
        """sync. Prompt text with provenance tags e.g. '（L 说的，3 周前）' '（我猜的）'."""

    @abc.abstractmethod
    async def observe_conversation(self, thread_id: str, lines: Sequence[ChatLine], participants: Sequence[str]) -> None:
        """Schedule async episodic segment + candidate fact extraction (debounced per thread, fast tier).
        Must return quickly."""

    @abc.abstractmethod
    async def recall_cues(self, texts: Sequence[str], limit: int = 3) -> list:
        """Fast spreading-activation recall for workspace memory_cue candidates (no LLM)."""

    @abc.abstractmethod
    async def episodes_between(self, since: float, until: float, limit: int = 200) -> list:
        """Episodic records in a time range (for sleep replay / today digest)."""

    @abc.abstractmethod
    async def about_person(self, person_key: str, limit: int = 30) -> list:
        """What I remember about a person (for 'what do you know about me' and relationship context)."""

    @abc.abstractmethod
    async def forget_person(self, person_key: str) -> int:
        """Privacy deletion (ADR-0003): physically delete the person's records and derivatives, redact their
        message payloads in the event log, write a hashed tombstone. Returns count."""

    @abc.abstractmethod
    async def consolidate(self, stage: str, ctx: dict) -> dict:
        """Sleep stages owned by memory: 'extract' | 'reconcile' | 'forget' | 'precompute'.
        ctx: {"since","until","now"}. Returns a report dict."""

    async def upcoming(self, start: float, end: float, limit: int = 30) -> list:
        """Records whose refers_to falls in [start, end) (plans, precompute)."""
        return []

    async def stats(self) -> dict:
        return {}


# =============================================================================
# Growth: evidence, playbook, expressions, skills, sleep orchestration (docs/06)
# =============================================================================
class GrowthSystem(Subsystem):
    @abc.abstractmethod
    async def record_evidence(self, *, level: str, polarity: str, about: str, context_refs: Sequence[str] = (),
                              subject: Optional[str] = None, confidence: float = 0.5,
                              entry_id: Optional[str] = None, note: str = "") -> str:
        """level S1|S2|S3|S4; polarity helpful|harmful|neutral; about = behavior tag. Returns id."""

    @abc.abstractmethod
    async def playbook_for(self, cue: str, scopes: Sequence[str], limit: int = 8) -> list:
        """Active playbook entries matching scopes (global, channel:<id>, person:<key>, situation:<tag>)."""

    @abc.abstractmethod
    async def expressions_for(self, cue: str, scopes: Sequence[str], limit: int = 5) -> list:
        """Active expression/jargon entries relevant to the cue and scopes."""

    @abc.abstractmethod
    def format_playbook(self, entries: Sequence[Any], expressions: Sequence[Any]) -> str:
        """sync. Prompt block text."""

    @abc.abstractmethod
    async def observe_messages(self, thread_id: str, chat_type: str, lines: Sequence[ChatLine]) -> None:
        """Feed expression/jargon learners (batched, async). Must return quickly."""

    @abc.abstractmethod
    async def run_sleep(self, now: float, *, stages: Optional[Sequence[str]] = None) -> dict:
        """Full sleep pipeline (docs/06 §5) with per-stage checkpoints; resumable after wake-ups.
        Calls memory.consolidate, selfm.sleep_update, motivation.set_day_plan. Returns report."""

    @abc.abstractmethod
    async def skills(self) -> list:
        """Installed skills metadata [{name, description, status, author}]."""

    def interrupt_sleep(self) -> None:
        """Pause a running sleep pipeline at the next stage boundary (checkpointed; run_sleep resumes)."""

    async def forget_person(self, person_key: str) -> dict:
        """Privacy: redact the person from learned artifacts."""
        return {}

    async def stats(self) -> dict:
        return {}


# =============================================================================
# Social: channels, inbound shaping, reply decision, embodiment, relations, web (docs/07)
# =============================================================================
@dataclass
class ThreadInfo:
    thread_id: str
    channel: str
    chat_type: str
    title: str = ""
    last_in: float = 0.0
    last_out: float = 0.0
    pending: int = 0              # unanswered incoming messages
    participants: list = field(default_factory=list)


class SocialSystem(Subsystem):
    @abc.abstractmethod
    async def submit_incoming(self, msg: IncomingMessage) -> None:
        """Entry point for channel adapters. Dedupe -> persist percept -> per-thread actor."""

    @abc.abstractmethod
    async def send(self, thread_id: str, segments: Sequence[str], *, kind: str = "reply",
                   reply_to: Optional[str] = None, style: Optional[dict] = None, person_key: Optional[str] = None,
                   abort_on_new: bool = True, plan_id: Optional[str] = None) -> dict:
        """Embodied send: governance pre_send per message, typing indicator, delays, abort remaining
        segments on new incoming correction. ``plan_id`` links an initiative to its OutreachPlan; the social
        layer is the single owner of outreach-result reporting (replies, warmth, ignored).
        Returns {"sent": n, "aborted": bool, "msg_ids": [...]}"""

    @abc.abstractmethod
    def threads(self) -> list:
        """sync. list[ThreadInfo]."""

    @abc.abstractmethod
    async def history(self, thread_id: str, limit: int = 30) -> list:
        """Recent ChatLines (oldest first) for a thread."""

    @abc.abstractmethod
    async def relationship_context(self, person_key: str) -> str:
        """Prompt block about a person: names, closeness, recent warmth, key facts, open loops, sensitivities."""

    @abc.abstractmethod
    async def outreach_candidates(self, now: float, limit: int = 10) -> list:
        """[{person_key, thread_id, name, closeness, warmth, last_contact, last_initiated_by,
        reciprocity, active_now_prob, recent_initiatives, proactive_ok, contact_gap_s}]"""

    @abc.abstractmethod
    async def record_interaction(self, person_key: str, *, quality: float, initiated_by: str,
                                 thread_id: Optional[str] = None, note: str = "") -> None:
        """Update relationship dynamics after an exchange. quality -1..1."""

    @abc.abstractmethod
    async def person_name(self, person_key: str) -> str:
        """Preferred display name."""

    @abc.abstractmethod
    async def read_url(self, url: str, purpose: str = "curiosity") -> dict:
        """Reader-isolated fetch+summary (docs/07 §7.2). Returns {"title","summary","points","url","trust":0}."""

    @abc.abstractmethod
    async def poll_feeds(self) -> list:
        """New feed items [{title, url, summary, feed}] since last poll."""


# =============================================================================
# Self model (docs/08)
# =============================================================================
class SelfModelSystem(Subsystem):
    @abc.abstractmethod
    def identity_digest(self) -> str:
        """sync. <=400 token identity summary (stable prefix)."""

    @abc.abstractmethod
    def name(self) -> str:
        """sync."""

    @abc.abstractmethod
    def voice_samples(self, n: int = 6) -> list:
        """sync."""

    @abc.abstractmethod
    def state_text(self) -> str:
        """sync. Current self-state block (mood/energy labels, dominant intention, today digest, open loops)."""

    @abc.abstractmethod
    async def record_outcome(self, summary: str, event_id: str = "", important: bool = False) -> None:
        """Append a real outcome to today's digest (only real outcomes; docs/08 §3)."""

    @abc.abstractmethod
    async def add_self_notes(self, notes: Sequence[str], source_event: str = "") -> None:
        """"""

    @abc.abstractmethod
    async def sleep_update(self, ctx: dict) -> dict:
        """Sleep stage 'self': update narrative/voice samples/journal under gates. ctx: reflections, day events."""


# =============================================================================
# Mind (cognitive core, docs/02)
# =============================================================================
class MindSystem(Subsystem):
    @abc.abstractmethod
    async def compose_reply(self, task: ReplyTask) -> ComposeResult:
        """S2 reply composition with INV-1 context, coherence filter and side effects."""

    async def speak(self, space: str, *, intent: str = "", chat_type: Optional[str] = None, title: str = "",
                    plan: Optional[OutreachPlan] = None, intention=None) -> bool:
        """The one unprompted speech act (docs/22 §4): consider speaking in a space on my own initiative (follow-up,
        outreach, later intent, own topic) with the same composer as replies; may decide not to. Returns sent?"""
        return False

    @abc.abstractmethod
    async def quick_read(self, task: ReplyTask) -> dict:
        """System-1 read of a batch (fast tier, JSON): sentiment, intent, urgency, praised/insulted/...,
        crisis flags, injection suspicion. Applies quick appraisal. Used by social before/after deciding."""

    @abc.abstractmethod
    def mode(self) -> str:
        """sync. Current Mode value."""
