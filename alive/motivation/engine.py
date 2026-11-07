"""MotivationEngine: the MotivationSystem implementation (docs/05).

needs -> urges -> intentions -> initiative. Needs live in memory (write-behind to kv "motivation"/"needs");
intentions and outreach plans live in ``mot_intentions`` / ``mot_outreach`` with an in-memory cache of the
open ones. All sync methods are cheap; async methods may touch the db / LLM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from alive.core.types import EV, Event, OutreachPlan, new_id
from alive.interfaces import AffectSnapshot, Intention, MotivationSystem, Urge
from alive.motivation import prompts as PR
from alive.motivation._bg import Background, WriteBehind
from alive.motivation.needs import NeedModel, clamp, logistic_threshold
from alive.runtime import chance as C
from alive.runtime.store import register_schema
from alive.social.latency import EVIDENCE_STAKES, reply_survival as prior_reply_survival
from alive.world.rekey import merge_annoyance, rekey_dict, rekey_obj

log = logging.getLogger("alive.motivation")

register_schema("motivation", 1, [
    """CREATE TABLE IF NOT EXISTS mot_intentions (
        id TEXT PRIMARY KEY, level TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
        priority REAL, kind TEXT, target_person TEXT, due_at REAL, trigger TEXT, plan TEXT, progress TEXT,
        created_from TEXT, created_at REAL, updated_at REAL, suspensions INTEGER DEFAULT 0,
        is_commitment INTEGER DEFAULT 0, parent_id TEXT, day TEXT, affect_tag TEXT,
        max_suspensions INTEGER, stale_after REAL, last_active REAL, meta TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_mot_int_status ON mot_intentions(status, level)",
    "CREATE INDEX IF NOT EXISTS ix_mot_int_day ON mot_intentions(day)",
    """CREATE TABLE IF NOT EXISTS mot_outreach (
        id TEXT PRIMARY KEY, person_key TEXT, thread_id TEXT, reason_type TEXT, content_hint TEXT,
        score REAL, source_event_ids TEXT, created_at REAL, sent_at REAL, outcome TEXT, latency_s REAL,
        resolved_at REAL, meta TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_mot_out_person ON mot_outreach(person_key, created_at)",
])

KV_NS = "motivation"
LEVELS = ("life", "project", "day", "now")
KINDS = ("reply", "outreach", "read", "learn", "reflect", "rest", "general")
STATUSES = ("dormant", "active", "suspended", "done", "abandoned")
OPEN = ("dormant", "active", "suspended")
TERMINAL = ("done", "abandoned")
KIND_NEED = {"reply": "social", "outreach": "social", "read": "curiosity", "learn": "curiosity",
             "reflect": "coherence", "rest": "rest", "general": "autonomy"}
AUTONOMOUS_KINDS = {"read", "learn", "reflect", "rest", "general"}
# Intentions are not dropped by a clock or a counter: they are forgotten, or given up on, by chance (docs/05 §4.3).
# STALE_AFTER = the TYPICAL (median) time an untouched intention survives in mind before it slips - one day for
# "now"/"day" things, a month for projects; life goals are identity, not memory. Each intention draws its own
# "forget after" once ~ LogNormal(median x (0.5 + priority) x (0.7 + 0.6 closeness of the person it concerns) x
# today's focus, sigma 0.6: some things slip within hours, others linger for days).
STALE_AFTER = {"now": 86400.0, "day": 86400.0, "project": 30 * 86400.0, "life": None}
# MAX_SUSPENSIONS = the TYPICAL number of interruptions I put up with before giving up (the old fixed cut-off was
# "abandon at the 4th", i.e. a median of 3.5): each intention draws its own patience ~ LogNormal(median (n + 0.5) x
# (0.5 + priority) x focus, sigma 0.5) - some things I drop after one interruption, some survive many.
MAX_SUSPENSIONS = {"now": 3, "day": 3, "project": 8, "life": None}
FORGET_SIGMA = 0.6
PATIENCE_SIGMA = 0.5
DEFAULT_PRIORITY = {"life": 0.7, "project": 0.6, "day": 0.5, "now": 0.5}
REASON_TYPES = ("care", "share", "promise", "continue", "greet", "confide")
CONTENT_VALUE = {"promise": 0.9, "care": 0.75, "share": 0.6, "continue": 0.6, "greet": 0.3, "confide": 0.5}
OUTCOMES = {"replied_warm": "replied_warm", "warm": "replied_warm", "replied_flat": "replied_flat",
            "flat": "replied_flat", "ignored": "ignored", "no_reply": "ignored", "none": "ignored"}


@dataclass
class IntentionRecord(Intention):
    """Intention + bookkeeping fields (docs/05 §4.2). Returned wherever the interface returns Intention."""
    parent_id: Optional[str] = None
    day: Optional[str] = None
    affect_tag: dict = field(default_factory=dict)       # {"valence": -1..1, "arousal": 0..1}
    max_suspensions: Optional[int] = None
    stale_after: Optional[float] = None                  # seconds of inactivity before abandonment
    last_active: float = 0.0
    meta: dict = field(default_factory=dict)             # window, free, autonomous, reason_type, triggered_at...
    score: float = 0.0                                   # last ranking score (not persisted)

    def row(self) -> tuple:
        j = lambda x: json.dumps(x, ensure_ascii=False)  # noqa: E731
        return (self.id, self.level, self.goal, self.status, self.priority, self.kind, self.target_person,
                self.due_at, j(self.trigger) if self.trigger else None, j(self.plan), j(self.progress),
                self.created_from, self.created_at, self.updated_at, self.suspensions, int(self.is_commitment),
                self.parent_id, self.day, j(self.affect_tag), self.max_suspensions, self.stale_after,
                self.last_active, j(self.meta))

    @classmethod
    def from_row(cls, r) -> "IntentionRecord":
        ld = lambda x, d: json.loads(x) if x else d  # noqa: E731
        return cls(id=r["id"], level=r["level"], goal=r["goal"], status=r["status"], priority=r["priority"] or 0.5,
                   kind=r["kind"] or "general", target_person=r["target_person"], due_at=r["due_at"],
                   trigger=ld(r["trigger"], None), plan=ld(r["plan"], []), progress=ld(r["progress"], []),
                   created_from=r["created_from"] or "", created_at=r["created_at"] or 0.0,
                   updated_at=r["updated_at"] or 0.0, suspensions=r["suspensions"] or 0,
                   is_commitment=bool(r["is_commitment"]), parent_id=r["parent_id"], day=r["day"],
                   affect_tag=ld(r["affect_tag"], {}), max_suspensions=r["max_suspensions"],
                   stale_after=r["stale_after"], last_active=r["last_active"] or 0.0, meta=ld(r["meta"], {}))


_INSERT_INT = ("INSERT OR REPLACE INTO mot_intentions (id,level,goal,status,priority,kind,target_person,due_at,"
               "trigger,plan,progress,created_from,created_at,updated_at,suspensions,is_commitment,parent_id,day,"
               "affect_tag,max_suspensions,stale_after,last_active,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
               "?,?,?,?,?)")
_INSERT_OUT = ("INSERT OR REPLACE INTO mot_outreach (id,person_key,thread_id,reason_type,content_hint,score,"
               "source_event_ids,created_at,sent_at,outcome,latency_s,resolved_at,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _load_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _norm_goal(s: str) -> str:
    return re.sub(r"[\s，。,.!！?？~～、:：;；\"'“”‘’（）()]+", "", s or "").lower()


def _parse_hhmm(s: str) -> Optional[float]:
    try:
        hh, mm = str(s).strip().split(":")[:2]
        return int(hh) + int(mm) / 60.0
    except Exception:
        return None


def _parse_window(w: Any) -> Optional[Tuple[float, float]]:
    if isinstance(w, (list, tuple)) and len(w) == 2:
        a, b = _parse_hhmm(w[0]), _parse_hhmm(w[1])
    elif isinstance(w, str) and "-" in w:
        a, b = (_parse_hhmm(x) for x in w.split("-", 1))
    else:
        return None
    if a is None or b is None:
        return None
    if b <= a:
        b += 24.0
    return a, b


class MotivationEngine(MotivationSystem):
    def __init__(self, kernel):
        super().__init__(kernel)
        self.cfg = kernel.cfg.section("motivation")
        pdir = kernel.persona_dir or ""
        self.temperament = _load_yaml(os.path.join(pdir, "temperament.yaml")) if pdir else {}
        self.rhythm = _load_yaml(os.path.join(pdir, "rhythm.yaml")) if pdir else {}
        self.model = NeedModel.from_temperament(self.temperament, self.cfg.as_dict())
        self.impulsivity = clamp(float(self.cfg.get("impulsivity", self.temperament.get("impulsivity", 0.3))))
        seed = self.cfg.get("seed")
        # an explicit seed (tests, experiments) gets its own generator; otherwise draws come from the life's RNG
        self.rng: Optional[random.Random] = random.Random(seed) if seed is not None else None
        self.big5 = {k: float(v) for k, v in (self.temperament.get("big_five") or {}).items()}
        # tunables (technical: LLM rate limit, event granularity, persistence)
        self.concretize_every_s = float(self.cfg.get("concretize_every_s", 1800))
        self.meaningful = float(self.cfg.get("meaningful_change", 0.05))
        self.save_every_s = float(self.cfg.get("save_every_s", 900))
        # needs: temperament values are the long-run centre; each day draws its own setpoint/decay (_daily_needs)
        self._need_base = {n: (x.setpoint, x.decay) for n, x in self.model.needs.items()}
        self._need_day = ""
        self._urge_th: Dict[str, float] = {}       # need -> this episode's latent onset threshold (logistic draw)
        self._wait_t: Optional[float] = None
        self._closeness: Dict[str, float] = {}     # person -> closeness last seen among candidates (for forgetting)
        ww = {"curiosity": 1.15, "social": 1.15, "competence": 0.85}
        ww.update(self.rhythm.get("weekend_weights") or {})
        self.weekend_weights = ww
        # state
        self.t: Optional[float] = None
        self._energy: Optional[float] = None
        self._asleep = False
        self._mood = (0.0, 0.0)
        self._stress = 0.0
        self._boredom = 0.0
        bp = self.cfg.get("baseline_p")
        self._p_base: Optional[float] = float(bp) if bp is not None else None
        self._urge_since: Dict[str, float] = {}
        self._urge_event: Dict[str, str] = {}
        self._concrete: Dict[str, dict] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._ints: Dict[str, IntentionRecord] = {}
        self._closed: deque = deque(maxlen=100)
        self._event_index: Dict[str, set] = {}
        self._plans: Dict[str, dict] = {}
        self._recent_plan_at: Dict[str, float] = {}
        self._annoy: Dict[str, dict] = {}
        self._greeted: Dict[str, float] = {}       # person -> when I last greeted them (refractory, not a daily ban)
        self._handles: list = []
        self._sub = None
        self.bg = Background(log)
        self._wb_needs = WriteBehind(self.bg, self._persist_needs)
        self._wb_annoy = WriteBehind(self.bg, self._persist_annoy)

    # ------------------------------------------------------------------ lifecycle & persistence
    async def start(self) -> None:
        now = self.k.clock.now()
        self._daily_needs(now)
        try:
            st = await self.k.db.kv_get(KV_NS, "needs")
        except Exception:
            log.exception("failed to load needs")
            st = None
        if isinstance(st, dict):
            self.model.load_state(st.get("needs", {}))
            self.t = st.get("t")
            if self.t is not None and now > self.t:
                self._advance(now, catch_up=True)
        self.t = now
        self._annoy = (await self.k.db.kv_get(KV_NS, "annoyance", {})) or {}
        rows = await self.k.db.fetchall("SELECT * FROM mot_intentions WHERE status IN ('dormant','active','suspended')")
        for r in rows:
            it = IntentionRecord.from_row(r)
            self._ints[it.id] = it
            self._index(it)
        rows = await self.k.db.fetchall("SELECT * FROM mot_outreach WHERE created_at >= ?", (now - 3 * 86400,))
        for r in rows:
            self._plans[r["id"]] = {k: r[k] for k in r.keys()}
            self._plans[r["id"]]["source_event_ids"] = json.loads(r["source_event_ids"] or "[]")
            self._plans[r["id"]]["meta"] = json.loads(r["meta"] or "{}")
        try:
            self._sub = self.k.bus.subscribe("*", self._on_event, name="motivation.triggers")
        except RuntimeError:  # pragma: no cover
            self._sub = None
        self._handles.append(self.k.scheduler.every(self.save_every_s, self._periodic, "motivation.save"))

    async def stop(self) -> None:
        for h in self._handles:
            h.cancel()
        self._handles.clear()
        if self._sub is not None:
            self._sub.cancel()
        try:
            await self._persist_needs()
            await self._persist_annoy()
        except Exception:
            log.exception("motivation persist on stop failed")
        await self.bg.drain(2.0)

    async def _persist_needs(self) -> None:
        await self.k.db.kv_set(KV_NS, "needs", {"t": self.t, "needs": self.model.to_state()}, ts=self.k.clock.now())

    async def _persist_annoy(self) -> None:
        await self.k.db.kv_set(KV_NS, "annoyance", self._annoy, ts=self.k.clock.now())

    async def _periodic(self) -> None:
        now = self.k.clock.now()
        self._advance(now)
        await self._persist_needs()
        await self._housekeep(now)

    def _emit(self, etype: str, payload: dict, *, causal_parent: Optional[str] = None,
              event_id: Optional[str] = None) -> str:
        eid = event_id or new_id("e")
        ev = Event(type=etype, payload=payload, source="motivation", causal_parent=causal_parent, id=eid)
        self.bg.spawn(self.k.bus.publish(ev))
        return eid

    # ------------------------------------------------------------------ chance helpers (alive/runtime/chance.py)
    def _rand(self) -> random.Random:
        return self.rng if self.rng is not None else C.rng_of(self.k)

    def _tex(self, trait: str, strength: float = 0.8) -> float:
        tex = getattr(self.k, "texture", None)
        try:
            return float(tex.multiplier(trait, strength)) if tex is not None else 1.0
        except Exception:
            return 1.0

    def _arousal(self) -> float:
        try:
            return float(self.k.affect.snapshot().A)
        except Exception:
            return 0.0

    # today's version of each need (docs/05 §2.2): the temperament value is the long-run centre; a day draws its
    # own setpoint (offset ~N(0, 0.03): a slightly more or less sociable day) and decay rate (x LogNormal(1, 0.2):
    # some days the wish for company or novelty builds faster). Today's texture shifts the matching need: sociability
    # -> social, curiosity -> curiosity, focus -> competence (multiplier 0.5..1.5 -> setpoint +-0.05, decay x^0.5).
    _NEED_TRAIT = {"social": "sociability", "curiosity": "curiosity", "competence": "focus"}

    def _daily_needs(self, now: float) -> None:
        day = self.k.clock.today(now)
        if day == self._need_day:
            return
        self._need_day = day
        for name, n in self.model.needs.items():
            sp0, d0 = self._need_base.get(name, (n.setpoint, n.decay))
            off = C.daily(self.k, f"need.{name}.sp", lambda r: r.gauss(0.0, 0.03), ts=now)
            fac = C.daily(self.k, f"need.{name}.decay", lambda r: C.lognormal(r, 1.0, 0.2), ts=now)
            trait = self._NEED_TRAIT.get(name)
            mult = self._tex(trait, 0.8) if trait else 1.0
            n.setpoint = clamp(sp0 + float(off) + 0.1 * (mult - 1.0), 0.05, 0.95)
            n.decay = d0 * float(fac) * (mult ** 0.5 if d0 < 0 else 1.0)

    # ------------------------------------------------------------------ needs
    def _advance(self, now: float, catch_up: bool = False) -> None:
        if self.t is None:
            self.t = now
            return
        gap = now - self.t
        if gap <= 0:
            return
        if catch_up and self.k.affect is not None:
            t = self.t
            while t < now:
                step = min(3600.0, now - t)
                asleep = bool(self.k.affect.is_sleep_time(t + step / 2))
                self.model.step(step / 3600, energy=self._energy, asleep=asleep)
                t += step
        else:
            self.model.step(gap / 3600, energy=self._energy, asleep=self._asleep)
        self.t = now
        self._wb_needs.dirty = True

    def _weights(self, now: float) -> Dict[str, float]:
        if self.k.clock.local(now).weekday() >= 5:
            return self.weekend_weights
        return {}

    def _pressures(self, now: Optional[float] = None) -> Dict[str, float]:
        now = self.t if now is None else now
        return self.model.pressures(mood_dev=self._mood, stress=self._stress, boredom=self._boredom,
                                    weights=self._weights(now or self.k.clock.now()))

    def pressure(self, need: str) -> float:
        """Current (coupled) pressure of one need. Extra public method."""
        self._advance(self.k.clock.now())
        return self._pressures().get(need, 0.0)

    def _absorb(self, affect: Optional[AffectSnapshot], dt_s: float = 0.0) -> None:
        if affect is None:
            return
        P = float(affect.P)
        if self._p_base is None:
            self._p_base = P
        elif dt_s > 0:  # slow EMA of observed pleasure = "my usual mood" (tau 72 h)
            a = 1 - math.exp(-dt_s / (72 * 3600))
            self._p_base += (P - self._p_base) * a
        self._energy = float(affect.energy)
        self._asleep = bool(affect.asleep)
        self._mood = (float(affect.P), float(affect.A))
        self._stress = float(affect.stress)
        self._boredom = float(affect.boredom)

    def tick(self, dt: float, now: float, affect: AffectSnapshot) -> list:
        if not now:
            now = (self.t if self.t is not None else self.k.clock.now()) + max(0.0, float(dt or 0.0))
        dt_s = now - self.t if self.t is not None else 0.0
        self._advance(now)        # elapsed interval with the previous affect context
        self._daily_needs(now)
        self._absorb(affect, dt_s)
        pressures = self._pressures(now)
        urges: List[Urge] = []
        for name, p in pressures.items():
            n = self.model.needs[name]
            # logistic onset (docs/05 §3.1): each urge episode has its own latent threshold drawn around the need's
            # centre, so P(urge | pressure) is a logistic curve, not a step; it is redrawn when the urge subsides
            th = self._urge_th.get(name)
            if th is None:
                th = self._urge_th[name] = logistic_threshold(self._rand(), n.threshold)
            if p >= th and not self._asleep:
                if name not in self._urge_since:
                    self._urge_since[name] = now
                    self._urge_event[name] = self._emit(EV.URGE, {"need": name, "pressure": round(p, 4),
                                                                  "level": round(n.level, 4)})
                    self._emit(EV.NEED_EVENT, {"op": "threshold", "need": name, "pressure": round(p, 4),
                                               "level": round(n.level, 4), "onset": round(th, 4)})
                c = self._concrete.get(name)
                fresh = c is not None and now - c["ts"] < self.concretize_every_s
                urges.append(Urge(need=name, pressure=round(p, 4),
                                  suggestion=c["suggestion"] if fresh else "",
                                  target_person=c.get("target_person") if fresh else None,
                                  created_at=self._urge_since[name]))
            elif name in self._urge_since and p < 0.8 * th:
                del self._urge_since[name]
                self._urge_event.pop(name, None)
                self._urge_th.pop(name, None)          # the next episode draws its own onset
        self._check_waiting(now)
        urges.sort(key=lambda u: -u.pressure)
        return urges

    def needs(self) -> dict:
        self._advance(self.k.clock.now())
        pr = self._pressures()
        return {name: {"level": round(n.level, 4), "setpoint": n.setpoint, "pressure": round(pr.get(name, 0.0), 4),
                       "threshold": n.threshold, "onset": self._urge_th.get(name), "decay": n.decay,
                       "weight": n.weight}
                for name, n in self.model.needs.items()}

    def _change(self, need: str, amount: float, cause: str, op: str) -> None:
        now = self.k.clock.now()
        self._advance(now)
        n = self.model.needs.get(need)
        if n is None:
            log.warning("unknown need %r", need)
            return
        before = n.level
        n.level = clamp(n.level + amount)
        if amount > 0:
            n.last_satisfied = now
        delta = n.level - before
        if self.k.affect is not None and abs(amount) > 0:
            try:
                self.k.affect.on_need_event(need, amount)
            except Exception:  # pragma: no cover
                log.exception("affect.on_need_event failed")
        if abs(delta) >= self.meaningful:
            self._emit(EV.NEED_EVENT, {"op": op, "need": need, "amount": round(amount, 4),
                                       "level": round(n.level, 4), "cause": cause})
        self._wb_needs.mark()

    def satisfy(self, need: str, amount: float, cause: str = "") -> None:
        self._change(need, abs(float(amount)), cause, "satisfy")

    def deplete(self, need: str, amount: float, cause: str = "") -> None:
        self._change(need, -abs(float(amount)), cause, "deplete")

    # ------------------------------------------------------------------ concretize
    async def concretize(self, urge: Urge) -> Urge:
        now = self.k.clock.now()
        c = self._concrete.get(urge.need)
        if c is not None and now - c["ts"] < self.concretize_every_s:
            return replace(urge, suggestion=c["suggestion"], target_person=c.get("target_person"))
        lock = self._locks.setdefault(urge.need, asyncio.Lock())
        async with lock:
            c = self._concrete.get(urge.need)
            if c is not None and self.k.clock.now() - c["ts"] < self.concretize_every_s:
                return replace(urge, suggestion=c["suggestion"], target_person=c.get("target_person"))
            hits: list = []
            people: list = []
            try:
                if self.k.memory is not None:
                    hits = await self.k.memory.retrieve(PR.NEED_CUE.get(urge.need, urge.need), tier="fast", limit=4)
            except Exception:
                log.debug("memory retrieve failed", exc_info=True)
            if urge.need in ("social", "curiosity", "coherence") and self.k.social is not None:
                try:
                    people = list(await self.k.social.outreach_candidates(now, limit=6))
                except Exception:
                    log.debug("outreach_candidates failed", exc_info=True)
            loops = await self.open_loops(4)
            lab = self.k.affect.labels() if self.k.affect is not None else {"mood": "", "energy": ""}
            mem_txt = "\n".join(f"- {getattr(h, 'content', h)}"[:120] for h in hits[:4]) or "（无）"
            ppl_txt = "\n".join(
                f"- {p.get('person_key')}｜{p.get('name', '')}｜{float(p.get('closeness', 0) or 0):.1f}｜"
                f"{self._ago(now, p.get('last_contact'))}" for p in people[:6]) or "（无）"
            data_block = PR.CONCRETIZE_DATA.format(memories=mem_txt, people=ppl_txt)
            if self.k.gov is not None:   # INV-1/INV-14: external text (memories, display names) last, as data
                data_block = self.k.gov.wrap_untrusted(data_block, "memory+relations")
            prompt = PR.CONCRETIZE.format(now=self.k.clock.fmt(now), mood=lab.get("mood", ""),
                                          energy=lab.get("energy", ""), need_zh=PR.NEED_ZH.get(urge.need, urge.need),
                                          pressure=urge.pressure, open_loops="\n".join(f"- {x}" for x in loops) or "（无）",
                                          data=data_block)
            data: dict = {}
            try:
                data = await self.k.llm.json("mot.concretize", prompt, tier="fast", system=PR.CONCRETIZE_SYSTEM,
                                             schema=PR.CONCRETIZE_SCHEMA)
            except Exception as e:  # BudgetDenied / LLMError
                log.info("concretize LLM unavailable (%r); using fallback", e)
            sugg = str(data.get("suggestion") or "").strip()[:80] or PR.FALLBACK_SUGGESTION.get(urge.need, "想做点什么")
            keys = {p.get("person_key") for p in people}
            target = str(data.get("target_person") or "").strip() or None
            if target not in keys:
                target = None
            kind = data.get("kind") if data.get("kind") in KINDS else PR.NEED_KIND.get(urge.need, "general")
            self._concrete[urge.need] = {"ts": self.k.clock.now(), "suggestion": sugg, "target_person": target,
                                         "kind": kind, "reason": str(data.get("reason") or "")[:120],
                                         "memory_ids": [getattr(h, "id", "") for h in hits[:4] if getattr(h, "id", "")]}
            return replace(urge, suggestion=sugg, target_person=target)

    @staticmethod
    def _ago(now: float, ts: Any) -> str:
        try:
            d = now - float(ts)
        except (TypeError, ValueError):
            return "未知"
        if d < 3600:
            return "刚刚"
        if d < 86400:
            return f"{int(d // 3600)} 小时前"
        return f"{int(d // 86400)} 天前"

    # ------------------------------------------------------------------ intentions
    def _index(self, it: IntentionRecord) -> None:
        trig = it.trigger or {}
        if trig.get("type") in ("event", "online") and it.status in OPEN:
            et = trig.get("event_type") or (EV.MESSAGE_IN if trig.get("type") == "online" else "")
            if et:
                self._event_index.setdefault(et, set()).add(it.id)

    def _unindex(self, it: IntentionRecord) -> None:
        for ids in self._event_index.values():
            ids.discard(it.id)

    @staticmethod
    def _norm_trigger(t: Any) -> Optional[dict]:
        if not isinstance(t, dict) or not t.get("type"):
            return None
        t = dict(t)
        typ = str(t["type"])
        if typ == "time":
            if "at" not in t and "in_s" in t:
                t["at"] = None  # resolved by caller
        elif typ == "need":
            t.setdefault("threshold", 0.08)
        elif typ == "online":
            t["event_type"] = EV.MESSAGE_IN
        elif typ == "event":
            t.setdefault("match", {})
        else:
            return None
        return t

    async def _save_int(self, it: IntentionRecord) -> None:
        await self.k.db.execute(_INSERT_INT, it.row())

    def _find_duplicate(self, goal: str, target: Optional[str], level: str) -> Optional[IntentionRecord]:
        ng = _norm_goal(goal)
        for it in self._ints.values():
            if it.status in OPEN and it.level == level and it.target_person == target and _norm_goal(it.goal) == ng:
                return it
        return None

    async def add_intention(self, draft: dict, source_event_id: str = "") -> Intention:
        now = self.k.clock.now()
        goal = str(draft.get("goal") or "").strip()[:300]
        if not goal:
            raise ValueError("intention needs a goal")
        level = draft.get("level") if draft.get("level") in LEVELS else "now"
        kind = draft.get("kind") if draft.get("kind") in KINDS else "general"
        target = draft.get("target_person") or None
        due_at = draft.get("due_at")
        if due_at is None and draft.get("due_in_s") is not None:
            due_at = now + float(draft["due_in_s"])
        due_at = float(due_at) if due_at is not None else None
        try:
            prio = clamp(float(draft.get("priority", DEFAULT_PRIORITY[level])))
        except (TypeError, ValueError):
            prio = DEFAULT_PRIORITY[level]
        commit = bool(draft.get("is_commitment", False))
        trig = self._norm_trigger(draft.get("trigger"))
        if trig and trig["type"] == "time" and trig.get("at") is None:
            trig["at"] = now + float((draft.get("trigger") or {}).get("in_s", 0))
        dup = self._find_duplicate(goal, target, level)
        if dup is not None:
            dup.priority = max(dup.priority, prio)
            if due_at is not None:
                dup.due_at = min(dup.due_at, due_at) if dup.due_at else due_at
            dup.is_commitment = dup.is_commitment or commit
            if draft.get("origins"):
                dup.meta["origins"] = sorted(set(dup.meta.get("origins") or []) | {str(o) for o in draft["origins"] if o})
            if dup.is_commitment:
                dup.max_suspensions = None
            dup.updated_at = now
            await self._save_int(dup)
            return dup
        meta = dict(draft.get("meta") or {})
        if draft.get("origins"):        # the spaces it came from: its audience (INV-34, alive/world/audience.py)
            meta["origins"] = [str(o) for o in draft["origins"] if o]
        for k in ("reason_type", "window", "free", "autonomous", "flexibility", "requested_by"):
            if k in draft and draft[k] is not None:
                meta[k] = draft[k]
        # how memorable / how robust to interruptions this one is: latent draws, scaled by state when used
        rng = self._rand()
        meta.setdefault("forget_z", round(rng.gauss(0.0, 1.0), 4))
        meta.setdefault("patience_z", round(rng.gauss(0.0, 1.0), 4))
        it = IntentionRecord(
            id=new_id("int"), level=level, goal=goal, status="active" if draft.get("status") == "active" else "dormant",
            priority=prio, kind=kind, target_person=target, due_at=due_at, trigger=trig,
            plan=list(draft.get("plan") or []), progress=[], created_from=source_event_id or "", created_at=now,
            updated_at=now, suspensions=0, is_commitment=commit, parent_id=draft.get("parent") or draft.get("parent_id"),
            day=draft.get("day") or (self.k.clock.today(now) if level == "day" else None),
            affect_tag=dict(draft.get("affect_tag") or {}),
            max_suspensions=None if commit else MAX_SUSPENSIONS[level],
            stale_after=float(draft["stale_after_s"]) if draft.get("stale_after_s") else STALE_AFTER[level],
            last_active=now, meta=meta)
        self._ints[it.id] = it
        self._index(it)
        await self._save_int(it)
        self._emit(EV.INTENTION, {"op": "created", "id": it.id, "goal": it.goal, "level": level, "kind": kind,
                                  "target_person": target, "is_commitment": commit},
                   causal_parent=source_event_id or None)
        return it

    def get_intention(self, intention_id: str) -> Optional[IntentionRecord]:
        """Cached intention by id (open or recently closed). Extra public method."""
        it = self._ints.get(intention_id)
        if it is None:
            for c in self._closed:
                if c.id == intention_id:
                    return c
        return it

    async def update_intention(self, intention_id: str, *, status: Optional[str] = None,
                               note: Optional[str] = None, priority: Optional[float] = None) -> None:
        now = self.k.clock.now()
        it = self.get_intention(intention_id)
        if it is None:
            r = await self.k.db.fetchone("SELECT * FROM mot_intentions WHERE id=?", (intention_id,))
            if r is None:
                log.warning("update of unknown intention %s", intention_id)
                return
            it = IntentionRecord.from_row(r)
            if it.status in OPEN:
                self._ints[it.id] = it
        if note:
            it.progress.append({"ts": now, "note": str(note)[:300]})
            del it.progress[:-50]
        if priority is not None:
            it.priority = clamp(float(priority))
        if status and status != it.status:
            if status not in STATUSES:
                raise ValueError(f"bad status {status!r}")
            if it.status in TERMINAL:
                log.warning("intention %s is %s; ignoring transition to %s", it.id, it.status, status)
            else:
                await self._transition(it, status, reason=note or "", now=now)
                return
        it.updated_at = now
        it.last_active = now
        await self._save_int(it)

    async def _transition(self, it: IntentionRecord, status: str, *, reason: str, now: float,
                          quiet: bool = False) -> None:
        prev = it.status
        it.status = status
        it.updated_at = now
        it.last_active = now
        if status == "suspended":
            it.suspensions += 1
            if it.suspensions >= 2:
                self.deplete("coherence", 0.02, cause=f"intention:{it.id}")  # "该做又没做"
            if it.max_suspensions is not None and not it.is_commitment and it.suspensions > self._patience(it):
                await self._save_int(it)
                await self._transition(it, "abandoned", reason="gave_up", now=now)
                return
        elif status == "done":
            self.satisfy("competence", 0.06 + 0.06 * it.priority, cause=f"intention:{it.id}")
            autonomous = it.meta.get("autonomous")
            if autonomous is None:
                autonomous = it.kind in AUTONOMOUS_KINDS and not it.target_person and not it.meta.get("requested_by")
            if autonomous:
                self.satisfy("autonomy", 0.05, cause=f"intention:{it.id}")
            extra = {"read": ("curiosity", 0.08), "learn": ("curiosity", 0.08), "reflect": ("coherence", 0.06),
                     "rest": ("rest", 0.05)}.get(it.kind)
            if extra:
                self.satisfy(extra[0], extra[1], cause=f"intention:{it.id}")
        elif status == "abandoned" and not quiet:
            self._abandon_affect(it, reason)
            if self.k.memory is not None:
                self.bg.spawn(self._note_abandon(it, reason))
        await self._save_int(it)
        if status in TERMINAL:
            self._ints.pop(it.id, None)
            self._unindex(it)
            self._closed.append(it)
        self._emit(EV.INTENTION, {"op": status, "id": it.id, "goal": it.goal, "prev": prev, "reason": reason,
                                  "level": it.level, "kind": it.kind, "is_commitment": it.is_commitment,
                                  "suspensions": it.suspensions}, causal_parent=it.created_from or None)

    def _abandon_affect(self, it: IntentionRecord, reason: str) -> None:
        if self.k.affect is None:
            return
        val = float((it.affect_tag or {}).get("valence", 0.0) or 0.0)
        if it.is_commitment:
            d = {"type": "guilt", "intensity": 0.3, "cause": f"没做到答应的事：{it.goal}"[:40], "target": it.target_person}
        elif val < -0.2:
            d = {"type": "relief", "intensity": 0.2, "cause": f"放下了「{it.goal}」"[:40]}
        else:
            d = {"type": "disappointment", "intensity": 0.15, "cause": f"没能做完「{it.goal}」"[:40]}
        try:
            self.k.affect.apply_deltas([d], cause_event_id="", target_weight=0.5)
        except Exception:  # pragma: no cover
            log.exception("affect on abandon failed")

    async def _note_abandon(self, it: IntentionRecord, reason: str) -> None:
        try:
            await self.k.memory.note(kind="episodic", content=f"我放弃了「{it.goal}」（{reason or '不了了之'}）",
                                     importance=2, source="experienced",
                                     person_keys=[it.target_person] if it.target_person else ())
        except Exception:
            log.debug("memory note on abandon failed", exc_info=True)

    def _trigger_fired(self, it: IntentionRecord, now: float, pressures: Dict[str, float]) -> bool:
        trig = it.trigger or {}
        typ = trig.get("type")
        if typ == "time":
            return trig.get("at") is not None and now >= float(trig["at"])
        if typ == "need":
            # "when that need gets high": a latent onset drawn once around the declared level (logistic, as urges)
            onset = it.meta.get("onset")
            if onset is None:
                onset = it.meta["onset"] = round(logistic_threshold(self._rand(), float(trig.get("threshold", 0.08))), 5)
            return pressures.get(trig.get("need", ""), 0.0) >= float(onset)
        if typ in ("event", "online"):
            return bool(it.meta.get("triggered_at"))
        return False

    def _rank(self, it: IntentionRecord, now: float, pressures: Dict[str, float]) -> float:
        s = it.priority
        parent = self._ints.get(it.parent_id) if it.parent_id else None
        if parent is not None:
            s += 0.3 * parent.priority
        s += min(0.3, pressures.get(KIND_NEED.get(it.kind, ""), 0.0))
        if it.due_at is not None:
            rem = it.due_at - now
            if rem <= 0:
                s += 0.3
            elif rem < 3600:
                s += 0.3 * (1 - rem / 3600)
            elif rem < 6 * 3600:
                s += 0.05
        if it.is_commitment:
            s += 0.1
        s += 0.1 * max(-1.0, min(1.0, float((it.affect_tag or {}).get("valence", 0.0) or 0.0)))
        if it.status == "suspended":
            s -= 0.1
        return round(s, 4)

    def _eligible(self, it: IntentionRecord, now: float, pressures: Dict[str, float]) -> Tuple[bool, bool]:
        """-> (eligible, trigger_fired)."""
        if it.meta.get("free"):
            return False, False
        fired = self._trigger_fired(it, now, pressures)
        if it.trigger and not fired and it.status != "active":
            return False, False
        if it.status == "active" or fired:
            return True, fired
        due_soon = it.due_at is not None and it.due_at - now <= 3600
        if it.status == "suspended":
            return due_soon, False
        if it.level == "now":
            return True, False
        if it.level == "day":
            return it.day == self.k.clock.today(now), False
        return due_soon, False

    async def candidates(self, now: float, limit: int = 5) -> list:
        await self._housekeep(now)
        self._advance(now)
        pressures = self._pressures(now)
        out: List[IntentionRecord] = []
        for it in list(self._ints.values()):
            ok, fired = self._eligible(it, now, pressures)
            if not ok:
                continue
            if fired and it.status == "dormant":
                await self._transition(it, "active", reason="triggered", now=now)
            it.score = self._rank(it, now, pressures)
            out.append(it)
        if len(out) > limit:          # which ones are on my mind: a Plackett-Luce sample, not a fixed top-k
            keep = set(C.gumbel_topk(self._rand(), {i: x.score for i, x in enumerate(out)}, limit,
                                     0.1 / self._tex("focus", 0.6)))
            out = [x for i, x in enumerate(out) if i in keep]
        out.sort(key=lambda x: -x.score)
        return out

    async def active_intentions(self, levels: Optional[Sequence[str]] = None, limit: int = 20) -> list:
        its = [i for i in self._ints.values() if i.status in OPEN and (levels is None or i.level in levels)]
        its.sort(key=lambda i: (LEVELS.index(i.level), -i.priority, i.created_at))
        return its[:limit]

    async def _housekeep(self, now: float) -> None:
        today = self.k.clock.today(now)
        for it in list(self._ints.values()):
            if it.status not in OPEN or it.is_commitment:
                continue
            if it.level == "day" and it.day and it.day < today:
                await self._transition(it, "abandoned", reason="day_passed", now=now, quiet=True)
                continue
            last = max(it.last_active or 0.0, it.updated_at or 0.0, it.created_at or 0.0, it.due_at or 0.0,
                       float((it.trigger or {}).get("at") or 0.0))
            if it.stale_after and now - last > self._forget_after(it):
                await self._transition(it, "abandoned", reason="forgotten", now=now)

    def _forget_after(self, it: IntentionRecord) -> float:
        """How long this intention survives untouched before it slips my mind (see STALE_AFTER): its own draw,
        made once (the latent normal ``forget_z``), scaled by the current priority, closeness and focus."""
        z = it.meta.get("forget_z")
        if z is None:
            z = it.meta["forget_z"] = round(self._rand().gauss(0.0, 1.0), 4)
        close = self._closeness.get(it.target_person, 0.5) if it.target_person else 0.5
        med = float(it.stale_after) * (0.5 + it.priority) * (0.7 + 0.6 * close) * self._tex("focus", 0.6)
        return med * math.exp(FORGET_SIGMA * float(z))

    def _patience(self, it: IntentionRecord) -> float:
        """How many interruptions I'll put up with for this one (see MAX_SUSPENSIONS)."""
        z = it.meta.get("patience_z")
        if z is None:
            z = it.meta["patience_z"] = round(self._rand().gauss(0.0, 1.0), 4)
        n_med = (float(it.max_suspensions) + 0.5) * (0.5 + it.priority) * self._tex("focus", 0.6)
        return n_med * math.exp(PATIENCE_SIGMA * float(z))

    async def _on_event(self, ev) -> None:
        if ev.type == EV.OUTREACH_PLAN and (ev.payload or {}).get("status") == "sent":
            await self._observe_sent(ev)
        elif ev.type == EV.OUTREACH_PLAN and (ev.payload or {}).get("status") in ("decided_silent", "blocked"):
            pid = ((ev.payload or {}).get("plan") or {}).get("id")      # the core let it go: no longer in flight
            if pid in self._plans:
                self._plans[pid].setdefault("meta", {})["dropped"] = (ev.payload or {}).get("status")
        ids = self._event_index.get(ev.type)
        if not ids:
            return
        for iid in list(ids):
            it = self._ints.get(iid)
            if it is None or it.status not in OPEN:
                ids.discard(iid)
                continue
            trig = it.trigger or {}
            p = ev.payload or {}
            if trig.get("type") == "online":
                pk = p.get("person_key") or (f"{p.get('channel')}:{p.get('sender_id')}" if p.get("sender_id") else "")
                if pk != trig.get("person", it.target_person):
                    continue
            else:
                match = trig.get("match") or {}
                if any(p.get(k) != v and getattr(ev, k, None) != v for k, v in match.items()):
                    continue
            it.meta["triggered_at"] = ev.ts
            it.meta["trigger_event"] = ev.id
            ids.discard(iid)
            if it.status == "dormant":
                await self._transition(it, "active", reason=f"triggered:{ev.type}", now=self.k.clock.now())
            else:
                await self._save_int(it)

    async def open_loops(self, limit: int = 7) -> list:
        now = self.k.clock.now()
        names: Dict[str, str] = {}

        async def name(pk: Optional[str]) -> str:
            if not pk:
                return ""
            if pk not in names:
                try:
                    names[pk] = await self.k.social.person_name(pk) if self.k.social is not None else pk
                except Exception:
                    names[pk] = pk
            return names[pk]

        out: List[str] = []
        commits = sorted((i for i in self._ints.values() if i.is_commitment and i.status in OPEN),
                         key=lambda i: (i.due_at or 1e18))
        for i in commits:
            who = await name(i.target_person)
            late = "（已经过了约定时间）" if i.due_at is not None and now > i.due_at else ""
            out.append((f"答应了{who}：{i.goal}" if who else f"答应过的事：{i.goal}") + late)
        sens = self._tex("sensitivity", 0.8)
        for p in sorted(self._plans.values(), key=lambda x: x.get("sent_at") or 0):
            if not p.get("sent_at") or p.get("outcome"):
                continue
            meta = p.get("meta") or {}
            # still on my mind? it stays for its own once-drawn span after I wrote (or last worried about it):
            # tau x Exp(1), tau = 1 d x (0.5 + closeness) x sensitivity - a median of ~23 h for a close friend
            close = clamp(float(meta.get("closeness") or 0.3))
            ref = max(float(p["sent_at"]), float(meta.get("last_pang") or 0.0))
            tau = 86400.0 * (0.5 + close) * sens
            if meta.get("loop_e") is None:
                meta["loop_e"] = round(-math.log(max(1e-12, self._rand().random())), 4)
            if now - ref < tau * float(meta["loop_e"]):
                out.append(f"在等{await name(p.get('person_key'))}回复（{p.get('content_hint', '')[:30]}）")
        rest = sorted((i for i in self._ints.values() if not i.is_commitment and i.status in ("active", "suspended")
                       and i.level in ("now", "day", "project") and not i.meta.get("free")),
                      key=lambda i: (-(i.status == "active"), -i.priority))
        for i in rest:
            out.append(("项目：" if i.level == "project" else "没做完：") + i.goal)
        return out[:limit]

    # ------------------------------------------------------------------ day plan (INV-6)
    def _day_ts(self, date: str, hours: float) -> float:
        d = datetime.strptime(date, "%Y-%m-%d")
        base = datetime(d.year, d.month, d.day, tzinfo=self.k.clock.tz)
        return (base + timedelta(hours=hours)).timestamp()

    def _find_gap(self, windows: List[Tuple[float, float]], minutes: float = 60.0) -> Optional[Tuple[float, float]]:
        """Where an added free / autonomous slot goes (hours of the day): sampled over the waking span I expect
        (the affect system's sampled wake and bed times) in proportion to expected energy^2, with the habitual
        active_hours as a mild prior (x1.5), never overlapping planned windows. Not in the first half hour after
        waking (still groggy) nor the last quarter hour before bed. Without an affect system that can predict
        this: uniformly over the habitual active hours."""
        active = [w for w in (_parse_window(x) for x in (self.rhythm.get("active_hours") or ["09:00-12:00"])) if w]
        step = 0.25
        dur = minutes / 60.0

        def free(t: float) -> bool:
            return all(t + dur <= s or t >= e for s, e in windows)

        cands: List[float] = []
        weights: List[float] = []
        aff = self.k.affect
        span = getattr(aff, "waking_span", None) if aff is not None else None
        energy_at = getattr(aff, "expected_energy", None) if aff is not None else None
        if callable(span) and callable(energy_at):
            try:
                w_ts, b_ts = span(self.k.clock.now())
                loc = datetime.fromtimestamp(w_ts, self.k.clock.tz)
                base = datetime(loc.year, loc.month, loc.day, tzinfo=self.k.clock.tz).timestamp()
                lo, hi = (w_ts - base) / 3600.0 + 0.5, (b_ts - base) / 3600.0 - 0.25 - dur
                t = math.ceil(lo / step) * step
                while t <= hi + 1e-9:
                    if free(t):
                        mid = t + dur / 2
                        w = max(0.0, float(energy_at(base + mid * 3600.0))) ** 2
                        w *= 1.5 if any(a <= mid < b for a, b in active) else 1.0
                        if w > 0:
                            cands.append(t)
                            weights.append(w)
                    t += step
            except Exception:
                log.debug("energy-weighted slot sampling failed", exc_info=True)
                cands, weights = [], []
        if not cands:
            for a, b in active:
                t = a
                while t + dur <= b + 1e-9:
                    if free(t):
                        cands.append(t)
                        weights.append(1.0)
                    t += step
        if not cands:
            return None
        t = C.weighted_sample(C.rng_of(self.k), cands, weights)
        return t, t + dur

    @staticmethod
    def _is_autonomous(item: dict) -> bool:
        if item.get("autonomous") is not None:
            return bool(item["autonomous"])
        return (item.get("kind", "general") in AUTONOMOUS_KINDS and not item.get("target_person")
                and not item.get("free") and not item.get("requested_by"))

    async def set_day_plan(self, date: str, items: Sequence[dict]) -> None:
        now = self.k.clock.now()
        norm_items: List[dict] = []
        for raw in items or []:
            if not isinstance(raw, dict) or not str(raw.get("goal") or "").strip():
                continue
            it = dict(raw)
            if it.get("kind") == "free":
                it["kind"], it["free"] = "rest", True
            it["free"] = bool(it.get("free", False))
            w = _parse_window(it.get("window")) or (_parse_window([it["start"], it["end"]])
                                                     if it.get("start") and it.get("end") else None)
            it["_w"] = w
            norm_items.append(it)
        added: List[str] = []
        windows = [i["_w"] for i in norm_items if i["_w"]]
        if not any(self._is_autonomous(i) for i in norm_items):
            gap = self._find_gap(windows)
            norm_items.append({"goal": "做一件自己想做的事：读点感兴趣的东西", "kind": "read", "autonomous": True,
                               "free": False, "_w": gap, "added_by": "INV-6", "flexibility": 0.8})
            if gap:
                windows.append(gap)
            added.append("autonomous")
        if not any(i.get("free") for i in norm_items):
            gap = self._find_gap(windows)
            norm_items.append({"goal": "空档：不安排任何事，留给自己", "kind": "rest", "free": True, "_w": gap,
                               "added_by": "INV-6", "flexibility": 1.0})
            added.append("free")
        # replace previous day-level intentions for that date
        for old in [i for i in self._ints.values() if i.level == "day" and i.day == date and i.status in OPEN]:
            await self._transition(old, "abandoned", reason="replaced", now=now, quiet=True)
        created = []
        for item in norm_items:
            w = item.pop("_w", None)
            draft = {"goal": item["goal"], "level": "day", "kind": item.get("kind", "general"), "day": date,
                     "priority": item.get("priority", 0.5), "target_person": item.get("target_person"),
                     "is_commitment": item.get("is_commitment", False), "plan": item.get("plan"),
                     "meta": {"free": bool(item.get("free")), "autonomous": self._is_autonomous(item),
                              "flexibility": item.get("flexibility", 0.5), "added_by": item.get("added_by"),
                              "requested_by": item.get("requested_by")}}
            if w:
                draft["trigger"] = {"type": "time", "at": self._day_ts(date, w[0])}
                draft["due_at"] = self._day_ts(date, w[1])
                draft["meta"]["window"] = [w[0], w[1]]
            it = await self.add_intention(draft, source_event_id=item.get("source_event_id", ""))
            created.append(it.id)
        self._emit(EV.INTENTION, {"op": "day_plan", "date": date, "count": len(created), "ids": created,
                                  "inv6_added": added})

    # ------------------------------------------------------------------ entity merge (docs/22 §3.3)
    async def rekey_entity(self, old: str, new: str) -> None:
        """``old`` was merged into ``new``: mot_* rows and the annoyance kv were rewritten by alive/world/rekey.py;
        move the live state (open intentions, outreach plans, annoyance, greeting / plan bookkeeping)."""
        for it in self._ints.values():
            if it.target_person == old:
                it.target_person = new
            if it.trigger:
                it.trigger = rekey_obj(it.trigger, old, new)
            if it.meta:
                it.meta = rekey_obj(it.meta, old, new)
        for p in self._plans.values():
            for k in ("person_key", "thread_id"):
                if p.get(k) == old:
                    p[k] = new
            if p.get("meta"):
                p["meta"] = rekey_obj(p["meta"], old, new)
        for c in self._concrete.values():
            if c.get("target_person") == old:
                c["target_person"] = new
        rekey_dict(self._recent_plan_at, old, new, max)
        rekey_dict(self._greeted, old, new, max)
        if rekey_dict(self._annoy, old, new, merge_annoyance):
            self._wb_annoy.mark()

    # ------------------------------------------------------------------ initiative (docs/05 §5, docs/07 §5)
    def _annoy_state(self, pk: str) -> dict:
        """How my initiatives to this person were received: decayed counts of warm / flat / ignored outcomes (the
        Beta-like reception posterior alive/core/initiative.py reads), and ``pending``: the provisional "passed
        over" evidence of waits still open (already included in ``ignored``, withdrawn if they answer)."""
        st = self._annoy.setdefault(pk, {"warm": 0.0, "flat": 0.0, "ignored": 0.0, "last": 0.0, "pending": {}})
        for legacy in ("ignored_streak", "backoff_until"):    # fields of the removed streak/backoff rule
            st.pop(legacy, None)
        if not isinstance(st.get("pending"), dict):
            st["pending"] = {}
        return st

    def reception(self, person_key: str) -> dict:
        """Extra public method: decayed reception counts for a person (warm / flat / ignored incl. open waits)."""
        st = self._annoy.get(person_key) or {}
        return {x: round(float(st.get(x, 0) or 0), 4) for x in ("warm", "flat", "ignored")}

    def _reception_update(self, pk: str, oc: str, key: Optional[str] = None, now: Optional[float] = None) -> dict:
        st = self._annoy_state(pk)
        pend = st["pending"]
        prov = float(pend.pop(key, 0.0) or 0.0) if key else 0.0
        for x in ("warm", "flat", "ignored"):     # older outcomes fade: recent reception matters more
            st[x] = round(float(st.get(x, 0) or 0) * 0.85, 4)
        for kk in list(pend):
            pend[kk] = round(float(pend[kk] or 0) * 0.85, 4)
        st["ignored"] = round(max(0.0, st["ignored"] - 0.85 * prov), 4)     # this wait's provisional share
        st[{"replied_warm": "warm", "replied_flat": "flat", "ignored": "ignored"}[oc]] += 1.0
        st["last"] = self.k.clock.now() if now is None else now
        self._wb_annoy.mark()
        return st

    def note_ignore_evidence(self, person_key: str, key: str, evidence: float, final: bool = False) -> None:
        """Extra public method (called by social, docs/05 §5.4): the evidence that my initiative ``key`` is being
        passed over, as it grows with the survival of their reply-latency distribution. It replaces the previous
        value for that wait, so the reception posterior follows it smoothly; ``final`` settles the wait."""
        st = self._annoy_state(person_key)
        pend = st["pending"]
        e = clamp(float(evidence or 0.0))
        old = float(pend.get(key, 0.0) or 0.0)
        st["ignored"] = round(max(0.0, float(st.get("ignored", 0) or 0) + e - old), 4)
        if final:
            pend.pop(key, None)
        else:
            pend[key] = round(e, 4)
            while len(pend) > 20:                  # technical bound on open waits per person
                pend.pop(next(iter(pend)))
        self._wb_annoy.mark()

    def record_initiative_outcome(self, person_key: str, outcome: str, *, key: Optional[str] = None) -> None:
        """Extra public method: the outcome of an initiative that had no motivation plan (e.g. bringing up my own
        topic): it still shapes how I expect my reaching out to be received."""
        oc = OUTCOMES.get(str(outcome).lower())
        if oc is None:
            raise ValueError(f"bad outcome {outcome!r}")
        self._reception_update(person_key, oc, key)

    def outreach_dropped(self, plan_id: str, reason: str = "not_sent") -> None:
        """Extra public method: the core let a proposal go (not sent): no longer in flight."""
        p = self._plans.get(plan_id)
        if p is not None and not p.get("sent_at"):
            p.setdefault("meta", {})["dropped"] = reason

    def _waiting_on(self, pk: str, now: float) -> bool:
        """True while I'm still waiting on an initiative to this person (sent, no outcome yet) or one is in flight."""
        return self._awaiting(pk, now) > 0 or self._in_flight(pk, now)

    def _in_flight(self, pk: str, now: float) -> bool:
        """A proposal for this person that the core is acting on right now (proposed, not yet sent/dropped).
        Technical de-duplication of concurrent proposals, not pacing."""
        for p in self._plans.values():
            if (p.get("person_key") == pk and not p.get("outcome") and not p.get("sent_at")
                    and not (p.get("meta") or {}).get("dropped") and now - float(p.get("created_at") or 0) < 1800):
                return True
        return False

    def _awaiting(self, pk: str, now: float) -> int:
        """How many of my initiatives to this person are still unanswered (inhibits the next draw, never bans)."""
        return sum(1 for p in self._plans.values() if p.get("person_key") == pk and p.get("sent_at")
                   and not p.get("outcome") and now - p["sent_at"] < 7 * 86400)

    @staticmethod
    def _contact_gap(c: dict, closeness: float) -> float:
        """Typical time between contacts with this person (s). Prior by closeness from the Dunbar layers (timing
        note §4: the innermost ~5 people are in touch at least weekly - the very closest most days -, the ~15
        layer monthly, the ~50 layer every one to three months): median 1 d at closeness 0.85, ~1 week at 0.5,
        ~5 weeks at 0.2. The observed gap between our contact days (social, last 60 days) is blended in on the log
        scale, weighted by the number of contact days it rests on (n / (n + 3))."""
        prior = 86400.0 * math.exp(5.6 * (0.85 - closeness))
        try:
            obs = float(c.get("contact_gap_s") or 0.0)
        except (TypeError, ValueError):
            obs = 0.0
        if obs <= 0:
            return prior
        n = min(60.0, 60 * 86400.0 / obs)
        w = n / (n + 3.0)
        return math.exp(w * math.log(obs) + (1 - w) * math.log(prior))

    def _day_anchors(self, now: float) -> Tuple[float, float]:
        """Today's wake and bed time as local hours (bed may exceed 24), from the sleep rhythm the affect system
        follows (alive/affect SleepRhythm, possibly drawn per night), else rhythm.yaml."""
        wake_h = _parse_hhmm(self.rhythm.get("wake", "07:40"))
        bed_h = _parse_hhmm(self.rhythm.get("sleep", "23:40"))
        wake_h = 7.67 if wake_h is None else wake_h
        bed_h = 23.67 if bed_h is None else bed_h
        rh = getattr(self.k.affect, "rhythm", None) if self.k.affect is not None else None
        try:
            if rh is not None and hasattr(rh, "last_wake") and hasattr(rh, "next_window"):
                w_ts = float(rh.last_wake(now))
                b_ts = float(rh.next_window(w_ts + 3600.0)[0])
                if 0 <= now - w_ts < 30 * 3600 and b_ts > w_ts:
                    wake_h = self.k.clock.hour_of_day(w_ts)
                    bed_h = wake_h + (b_ts - w_ts) / 3600.0
        except Exception:
            log.debug("sleep rhythm unavailable", exc_info=True)
        if bed_h < wake_h:
            bed_h += 24.0
        return wake_h, bed_h

    def _greet_strength(self, pk: str, now: float, closeness: float, since: float) -> Tuple[float, str]:
        """Greeting rituals (docs/05 §5.2): a bump shortly after I wake ("早安", ~0.85 h after waking) and one in the
        last stretch before bed ("晚安", ~1.6 h before) - the old fixed 8:30 / 22:00 for a 07:40-23:40 day - each
        moved by a per-day jitter N(0, 0.4 h); stronger for close people (closeness^1.5); unlikely right after we
        talked; and a refractory since my last greeting to them instead of a once-a-day ban: a second "good
        morning" within a few hours is odd, the evening one ~13 h later or the next morning's ~9 h later is normal
        (smooth step at 6 h, width 1.5 h)."""
        hour = self.k.clock.hour_of_day(now)
        wake_h, bed_h = self._day_anchors(now)
        jm = float(C.daily(self.k, "greet.morning_h", lambda r: r.gauss(0.0, 0.4), ts=now))
        je = float(C.daily(self.k, "greet.evening_h", lambda r: r.gauss(0.0, 0.4), ts=now))

        def bump(center: float, width: float) -> float:
            d = ((hour - center + 12.0) % 24.0) - 12.0
            return math.exp(-(d / width) ** 2)
        morning, evening = bump(wake_h + 0.85 + jm, 1.3), bump(bed_h - 1.6 + je, 1.0)
        last = self._greeted.get(pk)
        refractory = C.soft_step((now - float(last)) / 3600.0, 6.0, 1.5) if last else 1.0
        g = max(morning, evening) * closeness ** 1.5 * (1 - math.exp(-since / (8 * 3600.0))) * refractory
        return g, ("早安问候" if morning >= evening else "晚安问候")

    async def propose_outreach(self, urges: Sequence[Urge], now: float) -> list:
        """Who comes to mind and why (docs/05 §5.2), then whether NOW is the moment for each (the initiative draw).
        Per person: every reason I have (a promise, an intention, an urge, missing them, a greeting ritual,
        wanting to confide) gets a strength; what I'd say is a softmax draw over them and the reason strength is
        their noisy-or. People are considered in a Plackett-Luce order (Gumbel-top-k) and each gets one draw from
        alive/core/initiative.py - no sorted top-k, no per-call cap, no thresholds."""
        if self.k.social is None:
            return []
        aff = self.k.affect
        snap = aff.snapshot() if aff is not None else None
        if (snap is not None and snap.asleep) or (aff is not None and aff.is_sleep_time(now)):
            return []                                # someone asleep doesn't act (a fact, not a quota)
        try:
            # everyone I could reach out to (the limit is a technical bound, not a shortlist)
            cands = list(await self.k.social.outreach_candidates(now, limit=int(self.cfg.get("candidates", 200))))
        except Exception:
            log.warning("outreach_candidates failed", exc_info=True)
            return []
        P = snap.P if snap is not None else 0.0
        energy = snap.energy if snap is not None else 0.7
        battery = snap.social_battery if snap is not None else 0.8
        arousal = float(snap.A) if snap is not None else 0.0
        neg_emotion = max([e.get("intensity", 0.0) for e in (snap.emotions if snap else [])
                           if e.get("type") in ("sadness", "distress", "anxiety", "fear", "disappointment")] or [0.0])
        self._advance(now)
        sp = max([u.pressure for u in urges if u.need == "social"] or [self._pressures(now).get("social", 0.0)])
        social_n = 1 - math.exp(-sp / 0.12)
        urge_ev = self._urge_event.get("social")
        P0 = self._p_base if self._p_base is not None else P
        rng = self._rand()
        sociable = self._tex("sociability", 0.8)
        sens = self._tex("sensitivity", 0.8)
        focus = self._tex("focus", 0.6)
        # temperatures: what comes to mind is more scattered on an unfocused, aroused day
        t_content = 0.08 * (1 + 0.25 * arousal) / focus
        t_order = 0.1 * (1 + 0.25 * arousal) / focus
        low_mood = max(clamp((P0 - P) / 0.3) if P < P0 else 0.0, neg_emotion)
        scored = []
        for c in cands:
            pk = c.get("person_key")
            if not pk or c.get("proactive_ok", True) is False:
                continue                             # opted out: never (consent, not pacing)
            if self._in_flight(pk, now):
                continue
            # still waiting on my last initiative to them: not a ban - it inhibits the draw (double-texting)
            waiting = max(self._awaiting(pk, now), 1 if c.get("pending_initiative") else 0)
            name = c.get("name") or pk
            closeness = clamp(float(c.get("closeness", 0.0) or 0.0))
            self._closeness[pk] = closeness
            recip = max(-1.0, min(1.0, float(c.get("reciprocity", 0.0) or 0.0)))
            warmth = max(-1.0, min(1.0, float(c.get("warmth", 0.0) or 0.0)))
            fit = clamp((0.3 + 0.7 * closeness) * (1 + 0.25 * recip) * (0.85 + 0.15 * warmth))
            last = c.get("last_contact")
            since = now - float(last) if last else 1e9
            gap = self._contact_gap(c, closeness)
            # we just talked -> less reason to start again; on the scale of OUR usual rhythm (a tenth of the typical
            # gap: ~2.4 h for someone I talk to daily, days for a monthly friend), smooth
            recency = 1 - math.exp(-min(since, 30 * 86400) / max(600.0, 0.1 * gap))
            contents: List[Tuple[str, float, float, str, list]] = []
            for it in self._ints.values():
                if it.target_person != pk or it.status not in OPEN:
                    continue
                trig = it.trigger or {}
                if trig.get("type") == "time" and trig.get("at") is not None and now < float(trig["at"]) \
                        and it.status == "dormant":
                    continue                     # its own time hasn't come (e.g. "ask how the interview went")
                if it.is_commitment:
                    urg = 0.3 if (it.due_at is not None and it.due_at <= now + 3600) else 0.0
                    contents.append(("promise", CONTENT_VALUE["promise"], min(1.0, max(0.6, social_n) + urg),
                                     f"履行答应{name}的事：{it.goal}", [it.created_from, f"intention:{it.id}"]))
                elif it.kind == "outreach":
                    rt = it.meta.get("reason_type") if it.meta.get("reason_type") in REASON_TYPES else "continue"
                    contents.append((rt, CONTENT_VALUE[rt], max(social_n, 0.4), it.goal,
                                     [it.created_from, f"intention:{it.id}"]))
            for u in urges:
                if u.target_person == pk and u.suggestion:
                    rt = "share" if u.need == "curiosity" else "care"
                    cc = self._concrete.get(u.need, {})
                    contents.append((rt, 0.6, max(social_n, 1 - math.exp(-u.pressure / 0.12)), u.suggestion,
                                     [self._urge_event.get(u.need, "")] + [f"mem:{m}" for m in cc.get("memory_ids", [])]))
            if since < 1e9:
                # missing someone grows with time apart, on the scale of our usual contact gap (x1.5; ~1.5 d for a
                # daily friend, the old 2 d), sooner on a sociable day; stronger for close people
                miss = 1 - math.exp(-since / (1.5 * gap / sociable))
                contents.append(("care", 0.45 * miss, social_n, f"好久没和{name}聊了，问问近况", [urge_ev or ""]))
            g, g_hint = self._greet_strength(pk, now, closeness, since)
            if g > 1e-4:
                contents.append(("greet", CONTENT_VALUE["greet"] * g, social_n, g_hint, [urge_ev or ""]))
            cw = confide_weight(closeness, sens, low_mood)
            if cw > 1e-4:
                contents.append(("confide", CONTENT_VALUE["confide"] * cw, social_n,
                                 f"心里有点乱，想和{name}说说", [urge_ev or ""]))
            # how much battery matters fades smoothly for the closest people (no "closeness < 0.8" switch)
            battery_w = 1 - C.soft_step(closeness, 0.8, 0.06)
            options = []
            for rt, value, pressure, hint, srcs in contents:
                amod = (1 + 0.6 * max(0.0, P0 - P)) if rt == "confide" else (1 + 0.4 * (P - P0))
                amod *= 0.5 + 0.5 * clamp(energy / 0.5)
                amod *= 1 - battery_w * 0.5 * (1 - clamp(battery))
                # the REASON (what I want to say and how much); whether now is the moment is the initiative draw
                score = clamp(pressure * value * recency * max(0.0, amod))
                factors = {"pressure": round(pressure, 3), "content_value": round(value, 3),
                           "recency": round(recency, 3), "affect_modulation": round(amod, 3),
                           "relationship_fit": round(fit, 3)}
                options.append((score, rt, hint, srcs, factors))
            options = [o for o in options if o[0] > 0]
            if not options:
                continue
            options.sort(key=lambda o: -o[0])
            chosen = C.softmax_choice(rng, options, [o[0] for o in options], t_content)
            reason = 1.0 - math.prod(1.0 - o[0] for o in options)        # several reasons add up (noisy-or)
            alts = [(o[1], round(o[0], 4)) for o in options[:5]]
            scored.append((reason, c, chosen, waiting, alts))
        if not scored:
            return []
        order = C.gumbel_topk(rng, {i: s[0] for i, s in enumerate(scored)}, len(scored), t_order)
        plans: List[OutreachPlan] = []
        from alive.core.initiative import get_initiative
        ini = get_initiative(self.k)
        for i in order:
            raw, c, (sc, rt, hint, srcs, factors), waiting, alts = scored[i]
            pk = c["person_key"]
            ann = self._annoy.get(pk) or {}
            # whether NOW is the moment (docs/05 §5.6): a state-dependent draw - texture, mood, energy, their
            # rhythm, bond, how earlier initiatives were received, time since I last reached out, whether my last
            # message is still unanswered, day/night - with impulsivity widening it. The plan's score IS that reason.
            reason = clamp(raw) ** (1.5 - self.impulsivity)
            ctx = {"closeness": c.get("closeness"), "reciprocity": c.get("reciprocity"),
                   "active_now_prob": c.get("active_now_prob"), "my_unanswered": waiting,
                   "warm": ann.get("warm", 0), "flat": ann.get("flat", 0), "ignored": ann.get("ignored", 0)}
            if rt == "promise":
                ctx["commitment"] = 1.0
            go, p_send, ifac = await ini.decide(pk, shared=False, reason=reason, ctx=ctx, now=now, rng=rng)
            factors = {**factors, "initiative": ifac}
            if not go:
                continue
            pid = new_id("op")
            plan_ev = new_id("e")
            # unsure they're free -> a light check-in first; likelier the less likely they are around (sampled)
            negotiate = rt not in ("greet", "promise") and \
                rng.random() < 1 - clamp(float(c.get("active_now_prob", 0.5) or 0.0)) ** 0.5
            if negotiate:   # docs/07 §5 negotiated interruption: unsure they're free -> a light check-in first
                hint = f"{hint}（不确定对方方不方便，先轻轻问一句）"
            sources = [s for s in dict.fromkeys(srcs) if s and not s.startswith(("intention:", "mem:"))] + [plan_ev]
            plan = OutreachPlan(id=pid, person_key=c["person_key"], thread_id=c.get("thread_id") or "",
                                reason_type=rt, content_hint=hint[:200], source_event_ids=sources,
                                score=round(reason, 4), created_at=now)
            refs = [s for s in srcs if s and s.startswith(("intention:", "mem:"))]
            rec = {"id": pid, "person_key": plan.person_key, "thread_id": plan.thread_id, "reason_type": rt,
                   "content_hint": plan.content_hint, "score": plan.score, "source_event_ids": sources,
                   "created_at": now, "sent_at": None, "outcome": None, "latency_s": None, "resolved_at": None,
                   "meta": {"factors": factors, "p_send": round(p_send, 4), "refs": refs, "negotiate": negotiate,
                            "alternatives": alts, "closeness": c.get("closeness"), "name": c.get("name")}}
            self._plans[pid] = rec
            self._recent_plan_at[plan.person_key] = now
            await self.k.db.execute(_INSERT_OUT, self._out_row(rec))
            self._emit(EV.OUTREACH_PLAN, {"op": "proposed", "plan_id": pid, "person_key": plan.person_key,
                                          "reason_type": rt, "content_hint": plan.content_hint, "score": plan.score,
                                          "factors": factors, "refs": refs, "negotiate": negotiate,
                                          "alternatives": alts, "source_event_ids": sources}, event_id=plan_ev)
            plans.append(plan)
        return plans

    @staticmethod
    def _out_row(r: dict) -> tuple:
        return (r["id"], r["person_key"], r["thread_id"], r["reason_type"], r["content_hint"], r["score"],
                json.dumps(r["source_event_ids"], ensure_ascii=False), r["created_at"], r["sent_at"], r["outcome"],
                r["latency_s"], r["resolved_at"], json.dumps(r.get("meta") or {}, ensure_ascii=False))

    async def _observe_sent(self, ev) -> None:
        """The core announces a sent initiative (``mot.outreach_plan`` with status "sent" and the plan dict):
        start the waiting loop; adopt plans built outside propose_outreach so results still feed the
        annoyance model."""
        plan = (ev.payload or {}).get("plan") or {}
        pid = plan.get("id")
        if not pid or not plan.get("person_key"):
            return
        if pid not in self._plans:
            rec = {"id": pid, "person_key": plan["person_key"], "thread_id": plan.get("thread_id") or "",
                   "reason_type": plan.get("reason_type") if plan.get("reason_type") in REASON_TYPES else "continue",
                   "content_hint": str(plan.get("content_hint") or "")[:200], "score": float(plan.get("score") or 0.0),
                   "source_event_ids": list(plan.get("source_event_ids") or []),
                   "created_at": float(plan.get("created_at") or ev.ts), "sent_at": None, "outcome": None,
                   "latency_s": None, "resolved_at": None, "meta": {"adopted_from": ev.source}}
            self._plans[pid] = rec
        if not self._plans[pid].get("sent_at"):
            self.outreach_sent(pid, ev.ts)

    def outreach_sent(self, plan_id: str, now: Optional[float] = None) -> None:
        """Mark a plan as actually sent (starts the "waiting for a reply" open loop). Extra public method."""
        p = self._plans.get(plan_id)
        if p is None:
            return
        p["sent_at"] = self.k.clock.now() if now is None else now
        if p.get("reason_type") == "greet":           # the greeting refractory starts when I actually greeted
            self._greeted[p["person_key"]] = p["sent_at"]
        self.bg.spawn(self.k.db.execute(_INSERT_OUT, self._out_row(p)))

    def _survival(self, pk: str, since: float, now: float, closeness: float) -> float:
        """P(they'd still not have answered by now | they answer as they usually do): social's per-person estimate
        (their own latency distribution, availability-weighted clock), else the prior for their closeness."""
        fn = getattr(self.k.social, "reply_survival", None) if self.k.social is not None else None
        if fn is not None:
            try:
                return clamp(float(fn(pk, since, now)))
            except Exception:
                log.debug("reply_survival failed", exc_info=True)
        return prior_reply_survival(max(0.0, now - since), closeness)

    # Waiting for an answer (docs/05 §5.4): no fixed "6 h" timeout firing once. Worry is a point process whose
    # intensity rises as THEIR survival curve falls (they would usually have answered by now):
    #     rate = 0.4 / h x (1 - S(t)) x closeness x sensitivity(texture) x exp(-pangs / 1.2)   (habituation)
    # sampled by time-rescaling (chance.HazardClock: a pang when the integrated intensity passes a once-drawn Exp(1)
    # threshold, then a new one), so how often this is checked does not matter. Prior 0.4 / h: for a close friend the first pang typically comes a
    # couple of hours into an unusual silence (timing note §1: LINE survey, 30 min - 3 h for partners, 1 h - 1 day
    # for friends; Kato et al. 2020: unanswered-but-read messages raise anxiety faster in the more dependent).
    def _check_waiting(self, now: float) -> None:
        if self.k.affect is None:
            return
        if self._wait_t is not None and now - self._wait_t < 60.0:
            return                                   # technical: at most once a minute (the clock keeps the time)
        last = self._wait_t if self._wait_t is not None else now
        self._wait_t = now
        if now <= last:
            return
        sens = self._tex("sensitivity", 0.8)
        for p in self._plans.values():
            if not p.get("sent_at") or p.get("outcome") or now - float(p["sent_at"]) > 14 * 86400:
                continue                             # (a wait older than two weeks is out of bookkeeping range)
            meta = p.setdefault("meta", {})
            sent = float(p["sent_at"])
            t0 = max(last, sent)
            if now <= t0:
                continue
            close = clamp(float(meta.get("closeness") or 0.3))
            n = int(meta.get("worry_n", 0) or 0)
            mid = 0.5 * (t0 + now)
            s_mid, s_end = self._survival(p["person_key"], sent, mid, close), self._survival(p["person_key"], sent, now, close)
            rate = 0.4 * (1.0 - 0.5 * (s_mid + s_end)) * close * sens * math.exp(-n / 1.2)
            clock = C.HazardClock.from_dict(meta.get("worry_hz"))
            fired = clock.step(self._rand(), rate, (now - t0) / 3600.0)
            meta["worry_hz"] = clock.to_dict()
            if not fired:
                continue
            meta.update({"worry_n": n + 1, "last_pang": now})
            name = meta.get("name") or ""
            etype = "anxiety" if p["reason_type"] in ("care", "confide") else "disappointment"
            intensity = min(0.35, (0.1 + 0.2 * close) * (0.5 + 0.5 * (1.0 - s_end)) * sens)
            try:
                self.k.affect.apply_deltas([{"type": etype, "intensity": round(intensity, 3), "target": p["person_key"],
                                             "target_name": name or None,
                                             "cause": f"{name or '对方'}一直没回消息"}], target_weight=close)
            except Exception:  # pragma: no cover
                log.exception("waiting affect failed")
            self.bg.spawn(self.k.db.execute(_INSERT_OUT, self._out_row(p)))

    async def record_outreach_result(self, plan_id: str, outcome: str, latency_s: Optional[float] = None, *,
                                     key: Optional[str] = None) -> None:
        """outcome of a plan: the reception posterior (``key``: social's id for the wait, whose provisional evidence
        is withdrawn first), needs, the plan row, and the growth evidence - recorded once, here (social records it
        only for initiatives without a plan). An unanswered low-stakes initiative (share, greet) is weak evidence."""
        now = self.k.clock.now()
        oc = OUTCOMES.get(str(outcome).lower())
        if oc is None:
            raise ValueError(f"bad outcome {outcome!r}")
        p = self._plans.get(plan_id)
        if p is None:
            r = await self.k.db.fetchone("SELECT * FROM mot_outreach WHERE id=?", (plan_id,))
            if r is None:
                log.warning("result for unknown outreach plan %s", plan_id)
                return
            p = {k: r[k] for k in r.keys()}
            p["source_event_ids"] = json.loads(r["source_event_ids"] or "[]")
            p["meta"] = json.loads(r["meta"] or "{}")
            self._plans[plan_id] = p
        pk = p["person_key"]
        st = self._reception_update(pk, oc, key, now)
        if oc == "ignored":
            self.deplete("social", 0.05, cause=f"outreach:{plan_id}")
        elif oc == "replied_warm":
            self.satisfy("social", 0.15, cause=f"outreach:{plan_id}")
        else:
            self.satisfy("social", 0.05, cause=f"outreach:{plan_id}")
        p["outcome"], p["latency_s"], p["resolved_at"] = oc, latency_s, now
        if not p.get("sent_at"):
            p["sent_at"] = p["created_at"]
        await self.k.db.execute(_INSERT_OUT, self._out_row(p))
        self._emit(EV.OUTREACH_PLAN, {"op": "result", "plan_id": plan_id, "person_key": pk, "outcome": oc,
                                      "latency_s": latency_s, "reason_type": p["reason_type"],
                                      "reception": {x: st.get(x) for x in ("warm", "flat", "ignored")}})
        paused = bool(getattr(self.k.gov, "learning_paused", False)) if self.k.gov is not None else False
        if self.k.growth is not None and not paused:
            pol = {"replied_warm": "helpful", "replied_flat": "neutral", "ignored": "harmful"}[oc]
            conf = {"replied_warm": 0.6, "replied_flat": 0.4, "ignored": 0.6}[oc]
            if oc == "ignored":
                conf *= EVIDENCE_STAKES.get(p["reason_type"], 0.6)
            try:
                await self.k.growth.record_evidence(
                    level="S3", polarity=pol, about=f"outreach.{p['reason_type']}",
                    context_refs=[plan_id] + list(p.get("source_event_ids") or []), subject=pk,
                    confidence=round(conf, 3),
                    note=f"{oc}" + (f" latency={int(latency_s)}s" if latency_s is not None else ""))
            except Exception:
                log.warning("record_evidence failed", exc_info=True)

    # ------------------------------------------------------------------ inspector
    def status(self) -> dict:
        n = self.needs()
        return {"needs": {k: (v["level"], v["pressure"]) for k, v in n.items()},
                "urges": sorted(self._urge_since), "open_intentions": len(self._ints),
                "plans_pending": sum(1 for p in self._plans.values() if p.get("sent_at") and not p.get("outcome")),
                "reception": {pk: self.reception(pk) for pk in list(self._annoy)[:50]}}


def confide_weight(closeness: float, sensitivity: float = 1.0, low_mood: float = 0.0) -> float:
    """How much I'd want to confide in someone (docs/05 §5.2): closeness^2 (one confides in the inner circle; the
    timing note §6 "hedonic flexibility": feeling low raises initiation toward intimates x1.2-1.5 and lowers it
    toward weak ties) x today's sensitivity (texture multiplier) x how low I feel. Smooth everywhere: no knee, no
    minimum closeness (the removed 0.75 cut-off)."""
    return clamp(closeness) ** 2 * max(0.0, float(sensitivity)) * clamp(low_mood)
