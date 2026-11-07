"""Core data types shared by every subsystem.

Rules:
- All timestamps are float epoch seconds taken from ``kernel.clock.now()`` (never time.time()),
  so that simulated/accelerated time works everywhere.
- Dataclasses here are plain data. Behaviour lives in subsystems.
"""
from __future__ import annotations

import enum
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------
_id_lock = threading.Lock()
_last_ms = 0
_counter = 0


def new_id(prefix: str = "") -> str:
    """Time-ordered, collision-resistant id (ULID-like, hex). Sortable lexicographically.

    Uses wall-clock milliseconds for ordering across restarts plus a counter and randomness.
    """
    global _last_ms, _counter
    with _id_lock:
        ms = int(time.time() * 1000)
        if ms == _last_ms:
            _counter += 1
        else:
            _last_ms, _counter = ms, 0
        c = _counter
    rnd = os.urandom(4).hex()
    return f"{prefix}{ms:012x}{c:04x}{rnd}"


# ---------------------------------------------------------------------------
# Trust levels (docs/09 §5.1)
# ---------------------------------------------------------------------------
class Trust(enum.IntEnum):
    UNTRUSTED = 0  # web pages, strangers, tool output, other AI individuals, skill output
    PUBLIC = 1     # group members, public platforms
    CONTACT = 2    # paired acquaintances
    OWNER = 3      # the owner
    SYSTEM = 4     # constitution, framework code

    @classmethod
    def parse(cls, v: Any) -> "Trust":
        if isinstance(v, Trust):
            return v
        if isinstance(v, int):
            return cls(v)
        return cls[str(v).upper()]


class Tier(str, enum.Enum):
    FAST = "fast"
    DELIBERATE = "deliberate"
    DEEP = "deep"


class Mode(str, enum.Enum):
    """Operating modes (docs/02 §8)."""
    ACTIVE = "active"
    IDLE = "idle"
    FOCUS = "focus"
    DROWSY = "drowsy"
    ASLEEP = "asleep"
    OVERWHELMED = "overwhelmed"


class PermTier(enum.IntEnum):
    """Action permission tiers (docs/09 §3.1)."""
    T0_READ = 0
    T1_INTERNAL_WRITE = 1
    T2_EXTERNAL_COMM = 2
    T3_IRREVERSIBLE = 3
    T4_SELF_MODIFY = 4


# ---------------------------------------------------------------------------
# Events (docs/10 §2)
# ---------------------------------------------------------------------------
@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    source: str = "system"
    trust: Trust = Trust.SYSTEM
    thread_id: Optional[str] = None
    causal_parent: Optional[str] = None
    ts: float = 0.0
    id: str = ""
    cost: Optional[dict] = None
    schema_v: int = 1


class EV:
    """Canonical event type names. Namespaced by layer. Add new ones here (with a comment)."""
    # perception
    MESSAGE_IN = "percept.message"            # payload: IncomingMessage as dict
    MESSAGE_BATCH = "percept.batch"           # a debounced batch handed to decision
    WEB_READ = "percept.web"                  # reader-isolated summary of a page/feed item
    TIMER = "percept.timer"
    # cognition
    THOUGHT = "core.thought"                  # inner monologue / S2 hidden thought
    DECISION = "core.decision"                # reply decision (silent/react/respond/wait) with features
    WORKSPACE = "core.workspace"              # workspace changed (spotlight etc.)
    MODE = "core.mode"                        # mode switch
    COHERENCE_FAIL = "core.coherence_fail"
    # affect / motivation
    AFFECT_DELTA = "aff.delta"                # emotion events created
    AFFECT_SNAPSHOT = "aff.snapshot"          # periodic state snapshot
    NEED_EVENT = "mot.need"                   # need satisfied / depleted
    URGE = "mot.urge"
    INTENTION = "mot.intention"               # created/updated/done/abandoned
    OUTREACH_PLAN = "mot.outreach_plan"
    # acts / outcomes
    ACT_SEND = "act.send"                     # about to send (after governance)
    OUTCOME_SENT = "outcome.sent"
    OUTCOME_REACT = "outcome.react"
    OUTCOME_ABORTED = "outcome.aborted"
    # memory / growth / self
    MEMORY_WRITE = "mem.write"
    MEMORY_INVALIDATE = "mem.invalidate"
    MEMORY_FORGET = "mem.forget"              # tombstone (privacy)
    MEMORY_EXTRACT = "mem.extract"            # a conversation segment was processed
    EVIDENCE = "grow.evidence"
    LEARN_CHANGE = "grow.change"              # L2/L3 change proposed/adopted/rolled back
    SLEEP_STAGE = "grow.sleep_stage"
    SELF_NOTE = "self.note"
    SELF_UPDATE = "self.update"
    # governance / runtime
    GOV_DECISION = "gov.decision"
    GOV_VIOLATION = "gov.violation"
    GOV_CRISIS = "gov.crisis"
    GOV_DEPENDENCY = "gov.dependency"         # dependency risk crossed a threshold
    SOC_PAIRING_REQUESTED = "soc.pairing_requested"
    SOC_PAIRED = "soc.paired"
    SOC_INITIATIVE_RESOLVED = "soc.initiative_resolved"
    RT_MUTATION = "rt.mutation"               # the stochastic life-texture system made a jump (docs/21)
    LLM_CALL = "rt.llm_call"
    BUDGET_THROTTLED = "rt.budget_throttled"
    RT_ERROR = "rt.error"
    RT_START = "rt.start"
    RT_STOP = "rt.stop"


# ---------------------------------------------------------------------------
# Messaging (docs/07)
# ---------------------------------------------------------------------------
@dataclass
class IncomingMessage:
    channel: str            # adapter instance name, e.g. "telegram", "onebot", "cli", "sim"
    chat_id: str            # platform chat id (private chat id or group id)
    chat_type: str          # "private" | "group"
    sender_id: str          # platform user id
    sender_name: str
    text: str
    msg_id: str
    ts: float
    mentions_me: bool = False        # @me or name-mention resolved by adapter where possible
    reply_to_me: bool = False        # a reply/quote of one of my messages
    reply_to_msg_id: Optional[str] = None
    mentions_others: bool = False
    media: list = field(default_factory=list)   # [{"type": "image"|"sticker"|"file"|..., "url"?: str}]
    is_bot: bool = False
    raw: Optional[dict] = None

    # Entity resolution (docs/22): set by the social layer on intake. Above the transport layer everything is
    # keyed by entities, never by channel or conversation.
    sender_entity: Optional[str] = None   # the person (or agent) entity who said it
    space_entity: Optional[str] = None    # where it was said: the group entity, or the person entity for a DM

    @property
    def handle(self) -> str:
        """Transport-level sender handle (channel:native id)."""
        return f"{self.channel}:{self.sender_id}"

    @property
    def route_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"

    @property
    def thread_id(self) -> str:
        """The space entity id (group, or the person for a DM); falls back to the route before resolution."""
        return self.space_entity or f"{self.channel}:{self.chat_id}"

    @property
    def person_key(self) -> str:
        """The sender entity id; falls back to the transport handle before resolution."""
        return self.sender_entity or f"{self.channel}:{self.sender_id}"

    @property
    def media_only(self) -> bool:
        return bool(self.media) and not self.text.strip()


@dataclass
class ChatLine:
    """One line of conversation context handed to cognition."""
    sender: str             # display name
    person_key: str         # "" for me
    text: str
    ts: float
    mine: bool = False
    msg_id: str = ""


@dataclass
class ComposeResult:
    """What cognition returns to the interaction layer."""
    segments: list = field(default_factory=list)   # list[str] messages to send in order (already split)
    reaction: Optional[str] = None                 # emoji / micro-reaction instead of / besides text
    silent: bool = False                           # decided not to say anything after all
    reply_to_msg_id: Optional[str] = None
    style: dict = field(default_factory=dict)      # from affect.style(): length/emoji/delay factors
    reason: str = ""
    event_id: str = ""                             # the core.thought / decision event that produced it
    # privacy follows the information (INV-34): did what I composed from hold private data (rule of two)? None =
    # not reported (treated as yes). ``withheld``: contents left out because their audience isn't here; the
    # outbound check makes sure they are not said anyway (governance pre_send, private_from_others).
    private_used: Optional[bool] = None
    withheld: list = field(default_factory=list)


@dataclass
class ReplyTask:
    thread_id: str
    channel: str
    chat_type: str
    chat_title: str
    participants: list            # person_keys involved in the new messages
    history: list                 # list[ChatLine] recent context (oldest first)
    new_lines: list               # list[ChatLine] the batch to respond to
    mentioned: bool = False
    decision_reason: str = ""
    max_segments: int = 3
    trust: Trust = Trust.CONTACT
    cues: list = field(default_factory=list)   # zero-LLM reading cues computed by the social layer


@dataclass
class OutreachPlan:
    id: str
    person_key: str
    thread_id: str
    reason_type: str              # care | share | promise | continue | greet | confide (docs/05 §5.3)
    content_hint: str
    source_event_ids: list = field(default_factory=list)
    score: float = 0.0
    created_at: float = 0.0


def utc_now() -> float:  # only for ids/logging; use kernel.clock for logic
    return time.time()
