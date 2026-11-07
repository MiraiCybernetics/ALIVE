"""Relationship model (docs/07 §6): person profiles, relationship dynamics, aspect attitudes, opinions, pairing.

Dynamics (all lazily evaluated at read time from (value, ts)):
- closeness 0..1, slow. Decays toward ``closeness_floor`` with a half-life that grows with the relationship's
  maturity (age and interaction count): ~30 d for a new acquaintance -> ~365 d for an old, rich one, times a
  stable per-person factor (lognormal, sigma 0.3). A quality interaction q>0 adds ``gain·q·(1−c)``; q<0 removes
  ``loss·|q|·c``.
- warmth_recent −1..1, fast. Decays toward 0 with a half-life around ``warmth_half_life_days`` (median 3 d; a bit
  longer in mature relationships, and per person); each interaction moves it ``warmth_rate`` of the way toward q.
- reciprocity −1..1: EWMA of +1 (they initiated) / −1 (I initiated). Positive = they reach out more.
- rhythm: 24-bin active-hours histogram from their message times (local tz), typical reply latency (EWMA of
  the gap between my message and their next message in a private chat, < 12 h).
- aspect attitudes (DAM-LLM): C_new = (C·W + S·P)/(W + S), W_new = W + S; entropy H (bits) of C;
  stable if H < 0.8; an observation P with H(P) > 1.4 is discarded as uninformative.
- opinions (Hindsight): confidence +α supports, −α weakens, −2α contradicts; < 0.1 -> retracted.

Pairing (docs/07 §2.1, OpenClaw): 8-char codes from an alphabet without 0/O/1/I, valid 1 h, at most 3 pending
per channel, one live code per sender. The owner approves with ``approve_pairing(code)``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from alive.core.types import Trust, new_id
from alive.social import prompts
from alive.social.store import WriteBehind, dumps, loads

log = logging.getLogger("alive.social")

PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DAY = 86400.0


def _default_names() -> dict:
    return {"display": "", "nick": "", "preferred": "", "aliases": []}


def _default_consent() -> dict:
    return {"remember": True, "train_on_my_words": True, "proactive_ok": False}


def _default_mind() -> dict:
    return {"recent_mood_guess": "", "current_concerns": [], "preferences": [], "sensitivities": []}


@dataclass
class Person:
    person_key: str
    kind: str = "human"                      # human | agent
    names: dict = field(default_factory=_default_names)
    handles: dict = field(default_factory=dict)
    consent: dict = field(default_factory=_default_consent)
    common_ground: list = field(default_factory=list)
    private_from_others: list = field(default_factory=list)
    mind_model: dict = field(default_factory=_default_mind)
    paired: bool = False
    blocked: bool = False
    first_seen: float = 0.0
    meta: dict = field(default_factory=dict)  # dm_thread, owner, last_trust

    def name(self) -> str:
        n = self.names or {}
        return (n.get("preferred") or n.get("nick") or n.get("display")
                or self.person_key.split(":", 1)[-1])


@dataclass
class Relationship:
    person_key: str
    closeness: float = 0.1
    closeness_ts: float = 0.0
    warmth: float = 0.0
    warmth_ts: float = 0.0
    trust: float = 0.5
    reciprocity: float = 0.0
    hours: list = field(default_factory=lambda: [0.0] * 24)
    reply_latency: Optional[float] = None
    session_start: float = 0.0
    session_len: Optional[float] = None
    last_contact: float = 0.0
    last_in: float = 0.0
    last_out: float = 0.0
    last_initiated_by: str = ""
    ignored_streak: int = 0
    initiatives: list = field(default_factory=list)   # timestamps of my initiatives
    interactions: int = 0
    milestones: list = field(default_factory=list)    # [{"ts","kind","note","event_id"}]
    open_loops: list = field(default_factory=list)    # short strings


@dataclass
class RelParams:
    # closeness half-life = min * (max/min)^maturity * person factor. maturity = 0.5 (1 - e^{-age/240 d})
    # + 0.5 (1 - e^{-interactions/120}): a 2-month, 50-interaction relationship ~60 d; the old fixed 90 d sits at
    # maturity ~0.44 (about 4 months and 80 interactions); a year and 300 interactions ~250 d.
    closeness_half_life_min_days: float = 30.0
    closeness_half_life_max_days: float = 365.0
    maturity_age_days: float = 240.0
    maturity_interactions: float = 120.0
    person_half_life_sigma: float = 0.3     # stable per-person factor (some people fade from my mind faster)
    closeness_floor: float = 0.05
    warmth_half_life_days: float = 3.0      # median; x (0.8 + 0.6 maturity) / 1.0 and x person factor^0.5
    gain: float = 0.03
    loss: float = 0.02
    warmth_rate: float = 0.35
    reciprocity_rate: float = 0.15
    session_gap_s: float = 1800.0
    latency_max_s: float = 12 * 3600.0
    owner_closeness: float = 0.6
    owner_trust: float = 0.9
    contact_trust: float = 0.5
    stranger_trust: float = 0.2
    aspect_stable_entropy: float = 0.8
    aspect_discard_entropy: float = 1.4
    opinion_alpha: float = 0.1
    pairing_ttl_s: float = 3600.0
    pairing_max_pending: int = 3

    @classmethod
    def from_cfg(cls, cfg) -> "RelParams":
        p = cls()
        for k, f in p.__dataclass_fields__.items():
            v = cfg.get(k) if cfg is not None else None
            if v is not None:
                setattr(p, k, type(getattr(p, k))(v))
        return p


def entropy3(p: Sequence[float]) -> float:
    """Shannon entropy in bits of a (pos, neg, neu) distribution. Max log2(3) ≈ 1.585."""
    return -sum(x * math.log2(x) for x in p if x > 0)


def _norm3(probs: Any) -> tuple:
    if isinstance(probs, dict):
        v = (float(probs.get("pos", 0)), float(probs.get("neg", 0)), float(probs.get("neu", 0)))
    else:
        v = tuple(float(x) for x in probs)
        if len(v) != 3:
            raise ValueError("aspect observation needs (pos, neg, neu)")
    v = tuple(max(0.0, x) for x in v)
    s = sum(v)
    if s <= 0:
        return (1 / 3, 1 / 3, 1 / 3)
    return tuple(x / s for x in v)


def closeness_word(c: float) -> str:
    for th, w in prompts.CLOSENESS_WORDS:
        if c < th:
            return w
    return prompts.CLOSENESS_WORDS[-1][1]


def warmth_word(w: float) -> str:
    for th, word in prompts.WARMTH_WORDS:
        if w < th:
            return word
    return prompts.WARMTH_WORDS[-1][1]


class RelationshipStore:
    def __init__(self, kernel, writer: WriteBehind, params: Optional[RelParams] = None, *, cache_size: int = 5000):
        self.k = kernel
        self.w = writer
        self.p = params or RelParams()
        self.cache_size = cache_size
        self._persons: "OrderedDict[str, Person]" = OrderedDict()
        self._rels: "OrderedDict[str, Relationship]" = OrderedDict()
        self._loading: dict = {}
        self._aspects: dict = {}           # (person, aspect) -> dict
        self._pairing: dict = {}           # code -> dict (pending only)
        self._paired_keys: set = set()
        self._agents: set = set()
        self._owner_keys: set = set(kernel.cfg.get("owner.person_keys") or [])
        self._hl_factor: dict = {}         # person -> stable half-life factor (see person_factor)

    # ------------------------------------------------------------------ lifecycle
    async def load(self) -> None:
        db = self.k.db
        rows = await db.fetchall("SELECT person_key, kind, paired FROM soc_persons WHERE paired=1 OR kind='agent'")
        for r in rows:
            if r["paired"]:
                self._paired_keys.add(r["person_key"])
            if r["kind"] == "agent":
                self._agents.add(r["person_key"])
        now = self.k.clock.now()
        rows = await db.fetchall("SELECT * FROM soc_pairing WHERE status='pending'")
        for r in rows:
            if r["expires"] > now:
                self._pairing[r["code"]] = dict(r)
        for key in self.k.cfg.get("social.agents") or []:
            self._agents.add(str(key))

    # ------------------------------------------------------------------ cache
    def _put(self, p: Person, r: Relationship) -> None:
        self._persons[p.person_key] = p
        self._persons.move_to_end(p.person_key)
        self._rels[p.person_key] = r
        self._rels.move_to_end(p.person_key)
        while len(self._persons) > self.cache_size:
            k, _ = self._persons.popitem(last=False)
            self._rels.pop(k, None)

    def peek(self, person_key: str) -> Optional[Person]:
        return self._persons.get(person_key)

    def peek_rel(self, person_key: str) -> Optional[Relationship]:
        return self._rels.get(person_key)

    def is_owner(self, person_key: str) -> bool:
        return person_key in self._owner_keys

    def is_paired(self, person_key: str) -> bool:
        return person_key in self._paired_keys or self.is_owner(person_key)

    def is_agent(self, person_key: str) -> bool:
        p = self._persons.get(person_key)
        return person_key in self._agents or (p is not None and p.kind == "agent")

    async def get(self, person_key: str, *, create: bool = True) -> tuple:
        """(Person, Relationship) from cache or DB; creates defaults if missing and ``create``."""
        p = self._persons.get(person_key)
        if p is not None:
            self._persons.move_to_end(person_key)
            return p, self._rels[person_key]
        fut = self._loading.get(person_key)
        if fut is not None:
            await fut
            p = self._persons.get(person_key)
            if p is not None:
                return p, self._rels[person_key]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._loading[person_key] = fut
        try:
            def q(c):
                return (c.execute("SELECT * FROM soc_persons WHERE person_key=?", (person_key,)).fetchone(),
                        c.execute("SELECT * FROM soc_relationships WHERE person_key=?", (person_key,)).fetchone())
            await self.w.flush()
            prow, rrow = await self.k.db.read(q)
            if prow is None and not create:
                return None, None
            p = self._person_from_row(person_key, prow)
            r = self._rel_from_row(person_key, rrow)
            if prow is None:
                self._init_new(p, r)
            self._put(p, r)
            return p, r
        finally:
            self._loading.pop(person_key, None)
            if not fut.done():
                fut.set_result(None)

    def _init_new(self, p: Person, r: Relationship) -> None:
        now = self.k.clock.now()
        p.first_seen = now
        r.closeness_ts = r.warmth_ts = now
        if self.is_owner(p.person_key):
            p.meta["owner"] = True
            p.consent["proactive_ok"] = True
            p.paired = True
            r.closeness = self.p.owner_closeness
            r.trust = self.p.owner_trust
        if p.person_key in self._agents:
            p.kind = "agent"
        r.milestones.append({"ts": now, "kind": "first_seen", "note": "第一次遇到"})
        self.save(p, r)

    @staticmethod
    def _person_from_row(key: str, row) -> Person:
        if row is None:
            return Person(key)
        return Person(key, kind=row["kind"] or "human", names={**_default_names(), **loads(row["names"], {})},
                      handles=loads(row["handles"], {}), consent={**_default_consent(), **loads(row["consent"], {})},
                      common_ground=loads(row["common_ground"], []),
                      private_from_others=loads(row["private_from_others"], []),
                      mind_model={**_default_mind(), **loads(row["mind_model"], {})}, paired=bool(row["paired"]),
                      blocked=bool(row["blocked"]), first_seen=row["first_seen"] or 0.0, meta=loads(row["meta"], {}))

    @staticmethod
    def _rel_from_row(key: str, row) -> Relationship:
        if row is None:
            return Relationship(key)
        rh = loads(row["rhythm"], {}) or {}
        hours = rh.get("hours") or [0.0] * 24
        return Relationship(key, closeness=row["closeness"] or 0.0, closeness_ts=row["closeness_ts"] or 0.0,
                            warmth=row["warmth"] or 0.0, warmth_ts=row["warmth_ts"] or 0.0,
                            trust=row["trust"] if row["trust"] is not None else 0.5,
                            reciprocity=row["reciprocity"] or 0.0, hours=list(hours)[:24] + [0.0] * (24 - len(hours)),
                            reply_latency=rh.get("reply_latency"), session_start=rh.get("session_start", 0.0),
                            session_len=rh.get("session_len"), last_contact=row["last_contact"] or 0.0,
                            last_in=row["last_in"] or 0.0, last_out=row["last_out"] or 0.0,
                            last_initiated_by=row["last_initiated_by"] or "", ignored_streak=row["ignored_streak"] or 0,
                            initiatives=loads(row["initiatives"], []), interactions=row["interactions"] or 0,
                            milestones=loads(row["milestones"], []), open_loops=loads(row["open_loops"], []))

    def save(self, p: Optional[Person], r: Optional[Relationship]) -> None:
        now = self.k.clock.now()
        if p is not None:
            self.w.add("INSERT OR REPLACE INTO soc_persons (person_key,kind,names,handles,consent,common_ground,"
                       "private_from_others,mind_model,paired,blocked,first_seen,meta,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (p.person_key, p.kind, dumps(p.names), dumps(p.handles), dumps(p.consent), dumps(p.common_ground),
                        dumps(p.private_from_others), dumps(p.mind_model), int(p.paired), int(p.blocked), p.first_seen,
                        dumps(p.meta), now))
        if r is not None:
            rhythm = {"hours": [round(h, 3) for h in r.hours], "reply_latency": r.reply_latency,
                      "session_start": r.session_start, "session_len": r.session_len}
            self.w.add("INSERT OR REPLACE INTO soc_relationships (person_key,closeness,closeness_ts,warmth,warmth_ts,trust,"
                       "reciprocity,rhythm,last_contact,last_in,last_out,last_initiated_by,ignored_streak,initiatives,"
                       "interactions,milestones,open_loops,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (r.person_key, r.closeness, r.closeness_ts, r.warmth, r.warmth_ts, r.trust, r.reciprocity,
                        dumps(rhythm), r.last_contact, r.last_in, r.last_out, r.last_initiated_by, r.ignored_streak,
                        dumps(r.initiatives[-50:]), r.interactions, dumps(r.milestones[-50:]), dumps(r.open_loops[-20:]),
                        now))

    # ------------------------------------------------------------------ dynamics (pure, lazy decay)
    def _first_seen(self, r: Relationship) -> float:
        p = self._persons.get(r.person_key)
        if p is not None and p.first_seen:
            return float(p.first_seen)
        for m in r.milestones or []:
            if isinstance(m, dict) and m.get("kind") == "first_seen":
                return float(m.get("ts") or 0.0)
        return 0.0

    def maturity(self, r: Relationship, at: float) -> float:
        """0 (just met) .. 1 (years and hundreds of interactions)."""
        fs = self._first_seen(r)
        age_d = max(0.0, at - fs) / DAY if fs else 0.0
        return (0.5 * (1.0 - math.exp(-age_d / max(1e-6, self.p.maturity_age_days)))
                + 0.5 * (1.0 - math.exp(-max(0, r.interactions) / max(1e-6, self.p.maturity_interactions))))

    def person_factor(self, person_key: str) -> float:
        """A stable per-person draw (lognormal, median 1): derived from the key, so it needs no storage."""
        f = self._hl_factor.get(person_key)
        if f is None:
            import random as _random
            z = _random.Random(f"rel.half_life|{person_key}").gauss(0.0, 1.0)
            f = max(0.4, min(2.5, math.exp(self.p.person_half_life_sigma * z)))
            if len(self._hl_factor) > 20000:
                self._hl_factor.clear()
            self._hl_factor[person_key] = f
        return f

    def closeness_half_life_days(self, r: Relationship, at: Optional[float] = None) -> float:
        at = r.closeness_ts if at is None else at
        lo, hi = self.p.closeness_half_life_min_days, self.p.closeness_half_life_max_days
        m = self.maturity(r, at or 0.0)
        return lo * (hi / max(1e-6, lo)) ** m * self.person_factor(r.person_key)

    def warmth_half_life_days(self, r: Relationship, at: Optional[float] = None) -> float:
        at = r.warmth_ts if at is None else at
        m = self.maturity(r, at or 0.0)
        return self.p.warmth_half_life_days * (0.8 + 0.6 * m) * math.sqrt(self.person_factor(r.person_key))

    def closeness_at(self, r: Relationship, now: float) -> float:
        dt = max(0.0, now - (r.closeness_ts or now))
        f = self.p.closeness_floor
        if r.closeness <= f:
            return r.closeness
        return f + (r.closeness - f) * 0.5 ** (dt / (self.closeness_half_life_days(r) * DAY))

    def warmth_at(self, r: Relationship, now: float) -> float:
        dt = max(0.0, now - (r.warmth_ts or now))
        return r.warmth * 0.5 ** (dt / (self.warmth_half_life_days(r) * DAY))

    def cached_closeness(self, person_key: str, now: Optional[float] = None) -> Optional[float]:
        """Sync, no I/O: closeness of a person whose relationship is in the cache, else None (affect's battery)."""
        r = self._rels.get(person_key)
        if r is None:
            return None
        return self.closeness_at(r, self.k.clock.now() if now is None else now)

    def _materialize(self, r: Relationship, now: float) -> None:
        r.closeness = self.closeness_at(r, now)
        r.closeness_ts = now
        r.warmth = self.warmth_at(r, now)
        r.warmth_ts = now

    def active_prob(self, r: Relationship, hour: float) -> float:
        """How likely the person is active at local ``hour`` (0..1), from the smoothed histogram."""
        total = sum(r.hours)
        if total < 5:
            return 0.5
        h = int(hour) % 24
        sm = [(r.hours[(i - 1) % 24] + 2 * r.hours[i] + r.hours[(i + 1) % 24]) / 4 for i in range(24)]
        mx = max(sm) or 1.0
        return round(min(1.0, 0.05 + 0.95 * sm[h] / mx), 3)

    # ------------------------------------------------------------------ inbound/outbound bookkeeping
    async def ensure(self, msg, trust: Trust, *, thread_id: str, chat_type: str, chat_last_out: float = 0.0,
                     last_from_me: bool = False) -> tuple:
        """Create/refresh a person from an inbound message and update rhythm. Returns (Person, Relationship)."""
        p, r = await self.get(msg.person_key)
        now = self.k.clock.now()
        changed = False
        if msg.sender_name and msg.sender_name != p.names.get("display"):
            old = p.names.get("display")
            if old and old not in p.names["aliases"]:
                p.names["aliases"] = (p.names.get("aliases") or [])[-4:] + [old]
            p.names["display"] = msg.sender_name
            changed = True
        if p.handles.get(msg.channel) != msg.sender_id:
            p.handles[msg.channel] = msg.sender_id
            changed = True
        if msg.is_bot and p.kind != "agent":
            p.kind = "agent"
            self._agents.add(p.person_key)
            changed = True
        if chat_type == "private":
            if p.meta.get("dm_thread") != thread_id:
                p.meta["dm_thread"] = thread_id
                changed = True
            if int(trust) >= int(Trust.CONTACT) and not p.consent.get("proactive_ok") and not p.meta.get("consent_set"):
                p.consent["proactive_ok"] = True
                changed = True
        if p.meta.get("last_trust") != int(trust):
            p.meta["last_trust"] = int(trust)
            changed = True
            if r.interactions == 0 and not p.meta.get("owner"):
                t = int(trust)
                r.trust = (self.p.owner_trust if t >= int(Trust.OWNER) else
                           self.p.contact_trust if t >= int(Trust.CONTACT) else self.p.stranger_trust)
        # rhythm
        hour = self.k.clock.hour_of_day(msg.ts or now)
        r.hours[int(hour) % 24] += 1.0
        if sum(r.hours) > 500:
            r.hours = [h * 0.5 for h in r.hours]
        if chat_type == "private" and last_from_me and chat_last_out and 0 < now - chat_last_out < self.p.latency_max_s:
            lat = now - chat_last_out
            r.reply_latency = lat if r.reply_latency is None else 0.7 * r.reply_latency + 0.3 * lat
        if not r.last_contact or now - r.last_contact > self.p.session_gap_s:
            if r.session_start and r.last_contact:
                sl = r.last_contact - r.session_start
                r.session_len = sl if r.session_len is None else 0.7 * r.session_len + 0.3 * sl
            r.session_start = now
            if chat_type == "private":
                r.last_initiated_by = "them"
        r.last_in = now
        r.last_contact = now
        self.save(p if changed else None, r)
        return p, r

    def note_outbound(self, person_key: str, now: float, *, initiative: bool) -> None:
        r = self._rels.get(person_key)
        if r is None:
            return
        if not r.last_contact or now - r.last_contact > self.p.session_gap_s:
            r.session_start = now
        if initiative:
            r.last_initiated_by = "me"
        if initiative:
            r.initiatives = [t for t in r.initiatives if now - t < 30 * DAY] + [now]
            r.reciprocity = (1 - self.p.reciprocity_rate) * r.reciprocity - self.p.reciprocity_rate
        r.last_out = now
        r.last_contact = now
        self.save(None, r)

    async def rekey_entity(self, old: str, new: str) -> None:
        """Entity merge: cached Person/Relationship objects mirror rows the SQL pass already merged - drop both
        ids so the next access loads the merged row (a stale old-id object must never be saved again)."""
        for d in (self._persons, self._rels):
            for key in (old, new):
                d.pop(key, None)
        for key in [k_ for k_ in list(self._aspects) if isinstance(k_, tuple) and k_ and k_[0] in (old, new)]:
            self._aspects.pop(key, None)
        for coll in (self._paired_keys, self._agents, self._owner_keys):
            if isinstance(coll, set) and old in coll:
                coll.discard(old)
                coll.add(new)
        for k_, v in list(self._pairing.items()):
            if isinstance(v, dict) and v.get("person_key") == old:
                v["person_key"] = new

    async def record_interaction(self, person_key: str, *, quality: float, initiated_by: str,
                                 thread_id: Optional[str] = None, note: str = "") -> dict:
        p, r = await self.get(person_key)
        now = self.k.clock.now()
        q = max(-1.0, min(1.0, float(quality)))
        before = self.closeness_at(r, now)
        self._materialize(r, now)
        if q > 0:
            r.closeness += self.p.gain * q * (1 - r.closeness)
        elif q < 0:
            r.closeness -= self.p.loss * (-q) * r.closeness
        r.closeness = max(0.0, min(1.0, r.closeness))
        r.warmth += self.p.warmth_rate * (q - r.warmth)
        r.warmth = max(-1.0, min(1.0, r.warmth))
        r.interactions += 1
        if initiated_by == "them":  # "me" is counted by note_outbound(initiative=True) when the message goes out
            r.reciprocity = (1 - self.p.reciprocity_rate) * r.reciprocity + self.p.reciprocity_rate
        r.last_contact = max(r.last_contact, now)
        for th in (0.3, 0.6, 0.8):
            if before < th <= r.closeness:
                r.milestones.append({"ts": now, "kind": "closeness", "note": f"亲密度升到 {th}"
                                     + (f"（{note[:40]}）" if note else "")})
        self.save(None, r)
        return {"closeness": r.closeness, "warmth": r.warmth, "reciprocity": r.reciprocity}

    async def adjust_trust(self, person_key: str, delta: float, reason: str = "") -> float:
        p, r = await self.get(person_key)
        r.trust = max(0.0, min(1.0, r.trust + float(delta)))
        if abs(delta) >= 0.2:
            r.milestones.append({"ts": self.k.clock.now(), "kind": "trust", "note": reason[:60]})
        self.save(None, r)
        return r.trust

    async def add_milestone(self, person_key: str, note: str, event_id: str = "", kind: str = "event") -> None:
        p, r = await self.get(person_key)
        r.milestones.append({"ts": self.k.clock.now(), "kind": kind, "note": note[:120], "event_id": event_id})
        self.save(None, r)

    async def set_open_loops(self, person_key: str, loops: Sequence[str]) -> None:
        p, r = await self.get(person_key)
        r.open_loops = [str(x)[:80] for x in loops][-20:]
        self.save(None, r)

    async def update_person(self, person_key: str, *, preferred: Optional[str] = None, nick: Optional[str] = None,
                            kind: Optional[str] = None, consent: Optional[dict] = None, blocked: Optional[bool] = None,
                            mind_model: Optional[dict] = None, common_ground: Optional[Sequence[str]] = None,
                            private_from_others: Optional[Sequence[str]] = None) -> Person:
        p, r = await self.get(person_key)
        if preferred is not None:
            p.names["preferred"] = preferred
        if nick is not None:
            p.names["nick"] = nick
        if kind in ("human", "agent"):
            p.kind = kind
            (self._agents.add if kind == "agent" else self._agents.discard)(person_key)
        if consent:
            p.consent.update({k: bool(v) for k, v in consent.items()
                              if k in ("remember", "train_on_my_words", "proactive_ok")})
            p.meta["consent_set"] = True
        if blocked is not None:
            p.blocked = bool(blocked)
        if mind_model:
            for k2, v in mind_model.items():
                if k2 in ("current_concerns", "preferences", "sensitivities") and isinstance(v, (list, tuple)):
                    p.mind_model[k2] = [str(x)[:80] for x in v][-10:]
                elif k2 == "recent_mood_guess":
                    p.mind_model[k2] = str(v)[:60]
        if common_ground is not None:
            p.common_ground = list(dict.fromkeys(list(p.common_ground) + list(common_ground)))[-200:]
        if private_from_others is not None:
            p.private_from_others = list(dict.fromkeys(list(p.private_from_others) + list(private_from_others)))[-200:]
        self.save(p, None)
        return p

    # ------------------------------------------------------------------ aspect attitudes (DAM-LLM)
    async def _aspect(self, person_key: str, aspect: str) -> dict:
        key = (person_key, aspect)
        a = self._aspects.get(key)
        if a is None:
            row = await self.k.db.fetchone("SELECT * FROM soc_aspects WHERE person_key=? AND aspect=?", key)
            if row is not None:
                a = {"pos": row["pos"], "neg": row["neg"], "neu": row["neu"], "weight": row["weight"],
                     "n": row["n"], "discarded": row["discarded"], "summary": row["summary"] or ""}
            else:
                a = {"pos": 1 / 3, "neg": 1 / 3, "neu": 1 / 3, "weight": 0.0, "n": 0, "discarded": 0, "summary": ""}
            self._aspects[key] = a
            if len(self._aspects) > 20000:
                self._aspects.pop(next(iter(self._aspects)))
        return a

    async def observe_aspect(self, person_key: str, aspect: str, probs: Any, strength: float = 1.0,
                             summary: str = "") -> dict:
        """One observation of my attitude toward ``person_key`` on ``aspect`` (e.g. "工作", "感情").
        probs: (pos, neg, neu) or {"pos","neg","neu"}; strength S > 0 (evidence weight)."""
        P = _norm3(probs)
        S = max(0.0, float(strength))
        h_obs = entropy3(P)
        a = await self._aspect(person_key, aspect)
        if h_obs > self.p.aspect_discard_entropy or S <= 0:
            a["discarded"] += 1
            accepted = False
        else:
            W = a["weight"]
            C = (a["pos"], a["neg"], a["neu"])
            new = tuple((c * W + S * p) / (W + S) for c, p in zip(C, P))
            a["pos"], a["neg"], a["neu"] = new
            a["weight"] = W + S
            a["n"] += 1
            if summary:
                a["summary"] = summary[:120]
            accepted = True
        C = (a["pos"], a["neg"], a["neu"])
        h = entropy3(C)
        self.w.add("INSERT OR REPLACE INTO soc_aspects (person_key,aspect,pos,neg,neu,weight,entropy,n,discarded,summary,"
                   "updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (person_key, aspect, a["pos"], a["neg"], a["neu"], a["weight"], h, a["n"], a["discarded"],
                    a["summary"], self.k.clock.now()))
        return {"accepted": accepted, "obs_entropy": round(h_obs, 4), "entropy": round(h, 4),
                "stable": h < self.p.aspect_stable_entropy and a["n"] > 0, "pos": a["pos"], "neg": a["neg"],
                "neu": a["neu"], "weight": a["weight"], "n": a["n"]}

    async def aspects(self, person_key: str) -> list:
        await self.w.flush()
        rows = await self.k.db.fetchall("SELECT * FROM soc_aspects WHERE person_key=? ORDER BY weight DESC", (person_key,))
        out = []
        for r in rows:
            out.append({"aspect": r["aspect"], "pos": r["pos"], "neg": r["neg"], "neu": r["neu"], "weight": r["weight"],
                        "entropy": r["entropy"], "stable": (r["entropy"] or 9) < self.p.aspect_stable_entropy and r["n"] > 0,
                        "n": r["n"], "summary": r["summary"] or ""})
        return out

    # ------------------------------------------------------------------ opinions (Hindsight)
    async def add_opinion(self, person_key: str, text: str, confidence: float = 0.5,
                          evidence: Sequence[str] = ()) -> str:
        oid = new_id("op")
        now = self.k.clock.now()
        await self.k.db.execute("INSERT INTO soc_opinions (id,person_key,text,confidence,evidence,status,created,updated)"
                                " VALUES (?,?,?,?,?,?,?,?)", (oid, person_key, text[:300], max(0.0, min(1.0, confidence)),
                                                             dumps(list(evidence)), "active", now, now))
        return oid

    async def update_opinion(self, opinion_id: str, relation: str, evidence_id: str = "",
                             alpha: Optional[float] = None) -> Optional[dict]:
        """relation: support (+α) | weaken (−α) | contradict (−2α)."""
        a = self.p.opinion_alpha if alpha is None else float(alpha)
        delta = {"support": a, "weaken": -a, "contradict": -2 * a}.get(relation)
        if delta is None:
            raise ValueError(f"unknown relation {relation}")
        row = await self.k.db.fetchone("SELECT * FROM soc_opinions WHERE id=?", (opinion_id,))
        if row is None:
            return None
        conf = max(0.0, min(1.0, (row["confidence"] or 0.0) + delta))
        status = "retracted" if conf < 0.1 else "active"
        ev = loads(row["evidence"], [])
        if evidence_id:
            ev = (ev + [{"id": evidence_id, "rel": relation}])[-30:]
        await self.k.db.execute("UPDATE soc_opinions SET confidence=?, status=?, evidence=?, updated=? WHERE id=?",
                                (conf, status, dumps(ev), self.k.clock.now(), opinion_id))
        return {"id": opinion_id, "confidence": conf, "status": status}

    async def opinions(self, person_key: str, include_retracted: bool = False) -> list:
        sql = "SELECT * FROM soc_opinions WHERE person_key=?" + ("" if include_retracted else " AND status='active'")
        rows = await self.k.db.fetchall(sql + " ORDER BY confidence DESC", (person_key,))
        return [{"id": r["id"], "text": r["text"], "confidence": r["confidence"], "status": r["status"],
                 "evidence": loads(r["evidence"], [])} for r in rows]

    # ------------------------------------------------------------------ pairing
    def _prune_pairing(self, now: float) -> None:
        for code, d in list(self._pairing.items()):
            if d["expires"] <= now:
                self._pairing.pop(code, None)
                self.w.add("UPDATE soc_pairing SET status='expired' WHERE code=? AND status='pending'", (code,))

    async def request_pairing(self, channel: str, person_key: str, sender_name: str, chat_id: str) -> tuple:
        """Returns (code, created) or (None, reason)."""
        now = self.k.clock.now()
        self._prune_pairing(now)
        for code, d in self._pairing.items():
            if d["person_key"] == person_key:
                return code, False
        if sum(1 for d in self._pairing.values() if d["channel"] == channel) >= self.p.pairing_max_pending:
            return None, "too_many_pending"
        while True:
            code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
            if code not in self._pairing:
                break
        d = {"code": code, "channel": channel, "person_key": person_key, "sender_name": sender_name,
             "chat_id": chat_id, "created": now, "expires": now + self.p.pairing_ttl_s, "status": "pending"}
        self._pairing[code] = d
        self.w.add("INSERT OR REPLACE INTO soc_pairing (code,channel,person_key,sender_name,chat_id,created,expires,status)"
                   " VALUES (?,?,?,?,?,?,?,?)", (code, channel, person_key, sender_name, chat_id, now, d["expires"],
                                                 "pending"))
        return code, True

    def list_pairings(self, channel: Optional[str] = None) -> list:
        self._prune_pairing(self.k.clock.now())
        return [dict(d) for d in sorted(self._pairing.values(), key=lambda x: x["created"])
                if channel is None or d["channel"] == channel]

    async def decide_pairing(self, code: str, approve: bool, decided_by: str = "owner") -> Optional[dict]:
        code = (code or "").strip().upper()
        now = self.k.clock.now()
        self._prune_pairing(now)
        d = self._pairing.pop(code, None)
        if d is None:
            return None
        status = "approved" if approve else "rejected"
        self.w.add("UPDATE soc_pairing SET status=?, decided_by=?, decided_at=? WHERE code=?",
                   (status, decided_by, now, code))
        if approve:
            p, r = await self.get(d["person_key"])
            p.paired = True
            p.consent["proactive_ok"] = True
            r.trust = max(r.trust, self.p.contact_trust)
            r.milestones.append({"ts": now, "kind": "paired", "note": f"由 {decided_by} 批准配对"})
            self._paired_keys.add(p.person_key)
            self.save(p, r)
        return {**d, "status": status}

    async def revoke_pairing(self, person_key: str) -> bool:
        p, r = await self.get(person_key)
        was = p.paired
        p.paired = False
        p.consent["proactive_ok"] = False
        self._paired_keys.discard(person_key)
        self.save(p, None)
        return was

    # ------------------------------------------------------------------ listing
    async def all_persons(self, limit: int = 2000) -> list:
        """[(Person, Relationship)] — DB rows merged with fresher cache entries."""
        await self.w.flush()

        def q(c):
            ps = c.execute("SELECT * FROM soc_persons ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
            keys = [r["person_key"] for r in ps]
            rs = {}
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                for r in c.execute("SELECT * FROM soc_relationships WHERE person_key IN (%s)" % ",".join("?" * len(chunk)),
                                   chunk).fetchall():
                    rs[r["person_key"]] = r
            return ps, rs
        prows, rrows = await self.k.db.read(q)
        out = []
        for row in prows:
            key = row["person_key"]
            if key in self._persons:
                out.append((self._persons[key], self._rels[key]))
            else:
                out.append((self._person_from_row(key, row), self._rel_from_row(key, rrows.get(key))))
        seen = {p.person_key for p, _ in out}
        for key, p in self._persons.items():
            if key not in seen:
                out.append((p, self._rels[key]))
        return out
