"""SocialManager: the interaction layer (docs/07) — implements ``SocialSystem``.

Pipeline per inbound message (hot path never awaits the database except on a cold cache miss):

    adapter -> submit_incoming: dedupe -> group admission -> trust -> person/rhythm -> percept.message (bg)
            -> history (write-behind) -> thread actor inbox
    actor   -> attention (when I look) -> debounce -> decide (safety stops, then one engagement draw: air.py, decision.py)
            -> [quick_read] -> compose_reply (steer / interrupt while composing) -> embodied send
            -> after-exchange side effects (fire-and-forget via kernel.scheduler)

Configuration: ``kernel.cfg.section("social")`` (defaults next to the code below), channels from
``kernel.cfg.get("channels")``, persona talk_value / interests from ``personas/<name>/temperament.yaml``.

Extra public API (beyond SocialSystem):
    adapter(name) · channel_quotas(channel) · channel_capabilities(channel)
    list_pairings(channel=None) · approve_pairing(code, approved_by) · reject_pairing(code) · revoke_pairing(pk)
    is_paired(pk) · consent_of(pk) · update_person(pk, preferred=, nick=, kind=, consent=, blocked=, mind_model=, ...)
    allow_group(channel, chat_id, allowed=True) · set_talk_value(thread_id, v) · mute_thread(thread_id, seconds)
    observe_aspect(pk, aspect, probs, strength, summary) · add_opinion / update_opinion · adjust_trust
    relationship(pk) (raw snapshot) · note_outreach(pk, plan_id, reason_type) · send(..., plan_id=None)
    reading_cues(thread_id) · person_baseline(pk) · group_context(thread_id)   (docs/17 level-0 cues)
    flush() (wait for background writes; tests/shutdown) · decide(...) / features(...) / talk_value(st)
Events in the soc.* namespace: soc.pairing_requested, soc.paired, soc.initiative_resolved.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from alive.core.types import (EV, ChatLine, ComposeResult, Event, IncomingMessage, PermTier, ReplyTask, Trust,
                              new_id)
from alive.interfaces import ActRequest, SocialSystem
from alive.social import prompts
from alive.social import shaping
from alive.social import text as T
from alive.social.actor import InFlight, Inbound, ThreadActor, ThreadState
from alive.social.channels import build_adapter
from alive.runtime import chance as C
from alive.social.air import Attention, ack_only, heat_of
from alive.social.decision import (ALIVE_WEIGHTS, MAIBOT_WEIGHTS, GateFeatures, LineView, engagement,
                                   followup_strength, subconversation_strength)
from alive.social.embodiment import DelayModel, DelayParams, normalize_segments
from alive.social.relations import RelationshipStore, RelParams, closeness_word, warmth_word
from alive.social.shaping import DebounceParams, Dedupe
from alive.social.store import WriteBehind, dumps, loads
from alive.social.understanding import PersonBaseline, deviation_cues, group_context, group_cues
from alive.social.web import WebReader

log = logging.getLogger("alive.social")

INITIATIVE_KINDS = ("initiative", "outreach")


@dataclass
class Verdict:
    outcome: str = "silent"          # silent | wait | compose | quick_read   (final: respond | react | silent | wait)
    stage: str = "rule"              # rule | ladder | gate | quick_read | mind | gov | asleep
    reason: str = ""
    items: list = field(default_factory=list)
    must: bool = False
    addressed: bool = False
    p: Optional[float] = None
    threshold: Optional[float] = None
    talk_value: Optional[float] = None
    features: Optional[dict] = None
    terms: Optional[dict] = None
    gate: Optional[dict] = None
    round: Any = None
    asleep: bool = False
    pairing: bool = False
    retry_after: float = 0.0
    idle: bool = False
    cues: list = field(default_factory=list)


class SocialManager(SocialSystem):
    def __init__(self, kernel):
        super().__init__(kernel)
        c = kernel.cfg.section("social")
        self.cfg = c
        self.rng = random.Random(c.get("seed"))
        self.debounce = DebounceParams.from_cfg(c.section("debounce"))
        self.delay = DelayModel(DelayParams.from_cfg(c.section("delay")), rng_fn=lambda: self.pace_rng(),
                                daily_fn=lambda key, sampler: C.daily(self.k, key, sampler))
        self.dedupe = Dedupe(float(c.get("dedupe.ttl_s", 1200.0)), int(c.get("dedupe.max", 5000)))
        self.idle_gc_s = float(c.get("idle_gc_s", 1800.0))
        self.inbox_max = int(c.get("inbox_max", 200))
        self.max_threads = int(c.get("max_threads", 5000))
        self.max_segments = int(c.get("max_segments", 8))      # technical ceiling; how much I say is drawn
        self.split_min_chars = int(c.get("split_min_chars", 24))
        self.split_max = int(c.get("split_max_segments", 3))   # one long draft -> at most this many pieces
        self.history_n = int(c.get("history_lines", 30))
        self.hop_limit = int(c.get("bot_hop_limit", 4))
        self.max_restarts = int(c.get("max_interrupts", 2))
        self.max_steers = int(c.get("max_steers", 1))
        self.use_alive_features = bool(c.get("gate.use_alive_features", True))
        self.maibot_w = {**MAIBOT_WEIGHTS, **(c.get("gate.maibot_weights") or {})}
        self.alive_w = {**ALIVE_WEIGHTS, **(c.get("gate.alive_weights") or {})}
        self.gate_cfg = c.section("gate")
        self.air_w = dict(c.get("gate.air_weights") or {})
        self.air = Attention(self)                  # when I look at a space (docs/07 §3, ADR-0007)
        self.asleep_recheck_s = float(c.get("asleep_recheck_s", 600.0))   # technical fallback only
        self.retention_days = float(c.get("history_retention_days", 90.0))
        # no "ignored after 24 h": the evidence that an initiative is being passed over grows smoothly with the
        # survival of THEIR reply-latency distribution (alive/social/latency.py, _check_ignored_initiatives)
        self.report_outreach = bool(c.get("report_outreach_results", True))
        self._contact_gaps: dict = {}           # person_key -> typical gap between contact days (s), cached
        self._contact_gaps_at = -1e18
        self.writer = WriteBehind(kernel.db)
        self.relations = RelationshipStore(kernel, self.writer, RelParams.from_cfg(c.section("relations")))
        self.web = WebReader(kernel, c.section("web"))
        self.adapters: dict = {}
        self._threads: "OrderedDict[str, ThreadState]" = OrderedDict()
        self._actors: dict = {}
        self._bg: set = set()
        self._after_tasks: set = set()
        self._dirty_threads: set = set()
        self._thread_flush_scheduled = False
        self.persona_talk_value = 1.0
        self._interest_sets: list = []
        self._names: list = []
        self._allowed_groups: set = set()
        self._talk_override: dict = {}
        self._recent_plans: dict = {}          # person_key -> {"plan_id","reason_type","ts"}
        self._pending_init: dict = {}          # person_key -> initiative dict
        self._init_counts: dict = {}           # (channel, chat_id) -> [ts]
        self._subs: list = []
        self._baselines: "OrderedDict[str, PersonBaseline]" = OrderedDict()
        self.counters: dict = {}
        self._started = False
        self._last_social = 0.0                # last time I sent or got a message anywhere (initiative momentum)

    # ================================================================== lifecycle
    async def start(self) -> None:
        k = self.k
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_persona)
        await self.relations.load()
        await self._load_threads()
        self._allowed_groups = set(await k.db.kv_get("social", "allowed_groups", []) or [])
        await self._load_initiatives()
        self._refresh_names()
        for spec in k.cfg.get("channels") or []:
            try:
                ad = build_adapter(k, spec)
            except Exception:
                log.exception("cannot build channel %r", spec)
                continue
            if ad.name in self.adapters:
                log.error("duplicate channel name %s", ad.name)
                continue
            self.adapters[ad.name] = ad
        for ad in self.adapters.values():
            try:
                await ad.start()
            except Exception:
                log.exception("channel %s failed to start", ad.name)
        try:
            self._subs.append(k.bus.subscribe(EV.OUTREACH_PLAN, self._on_outreach_plan, name="social.outreach_plans"))
        except Exception:
            log.debug("bus subscribe failed", exc_info=True)
        k.scheduler.every(6 * 3600.0, self._retention, "soc.retention", initial_delay=1800.0)
        k.scheduler.every(1800.0, self._check_ignored_initiatives, "soc.initiatives", initial_delay=900.0)
        self._started = True

    async def stop(self) -> None:
        self._started = False
        for ad in list(self.adapters.values()):
            try:
                await ad.stop()
            except Exception:
                log.exception("channel %s failed to stop", ad.name)
        for a in list(self._actors.values()):
            if a.task is not None and not a.task.done():
                a.task.cancel()
        tasks = [a.task for a in self._actors.values() if a.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for s in self._subs:
            try:
                s.cancel()
            except Exception:
                pass
        for pk, b in self._baselines.items():
            p = self.relations.peek(pk)
            if p is not None and b.n and (p.meta.get("baseline") or {}).get("n") != b.n:
                p.meta["baseline"] = b.to_dict()
                self.relations.save(p, None)
        await self.flush()
        await self.web.close()

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait for side-effect tasks, background event emits and write-behind rows (tests, shutdown)."""
        self._flush_threads()
        try:
            if self._after_tasks:
                await asyncio.wait_for(asyncio.gather(*list(self._after_tasks), return_exceptions=True), timeout)
        except asyncio.TimeoutError:
            log.warning("social: side-effect tasks still pending at flush")
        self._flush_threads()
        try:
            if self._bg:
                await asyncio.wait_for(asyncio.gather(*list(self._bg), return_exceptions=True), timeout)
        except asyncio.TimeoutError:
            log.warning("social: background tasks still pending at flush")
        await self.writer.flush()

    def _load_persona(self) -> None:
        import yaml
        pdir = self.k.persona_dir or ""
        temp = {}
        try:
            with open(os.path.join(pdir, "temperament.yaml"), "r", encoding="utf-8") as f:
                temp = yaml.safe_load(f) or {}
        except Exception:
            pass
        tv = self.cfg.get("talk_value")
        if tv is None:
            tv = temp.get("talk_value", 1.0)
        try:
            self.persona_talk_value = float(tv)
        except Exception:
            self.persona_talk_value = 1.0
        interests = list(self.cfg.get("interests") or temp.get("interests") or [])
        if not interests:
            try:
                with open(os.path.join(pdir, "identity", "narrative.md"), "r", encoding="utf-8") as f:
                    for line in f:
                        if "兴趣" in line or "喜欢" in line:
                            interests.append(line.strip("-# \n"))
            except Exception:
                pass
        self._interest_sets = [T.tokens(str(x)) for x in interests if str(x).strip()][:20]

    def _refresh_names(self) -> None:
        names = list(self.cfg.get("names") or [])
        try:
            if self.k.selfm is not None:
                names.insert(0, self.k.selfm.name())
        except Exception:
            pass
        self._names = [n for n in dict.fromkeys(names) if n]

    def status(self) -> dict:
        return {"threads": len(self._threads), "actors": len(self._actors),
                "busy": sum(1 for a in self._actors.values() if a.busy), "dedupe": len(self.dedupe),
                "bg_tasks": len(self._bg), "write_pending": self.writer.pending,
                "pending_pairings": len(self.relations.list_pairings()), "counters": dict(self.counters),
                "channels": {n: a.status() for n, a in self.adapters.items()}}

    def _count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    # ================================================================== background helpers
    def _spawn_bg(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg_done)
        if len(self._bg) > 5000 and len(self._bg) % 1000 == 0:
            log.warning("social: %d background tasks pending (database slow?)", len(self._bg))

    def _bg_done(self, t: asyncio.Task) -> None:
        self._bg.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("social background task failed: %r", t.exception())

    def _emit_bg(self, etype: str, payload: dict, *, trust: Trust = Trust.SYSTEM, thread_id: Optional[str] = None,
                 causal_parent: Optional[str] = None, source: str = "social") -> str:
        ev = Event(type=etype, payload=payload, source=source, trust=trust, thread_id=thread_id,
                   causal_parent=causal_parent, ts=self.k.clock.now(), id=new_id("e"))
        self._spawn_bg(self.k.bus.publish(ev))
        return ev.id

    def _after(self, coro_fn, name: str) -> None:
        """Fire-and-forget side effects off the latency-critical path (supervised by the scheduler)."""
        try:
            h = self.k.scheduler.spawn(coro_fn, name)
        except RuntimeError:
            return
        self._after_tasks.add(h.task)
        h.task.add_done_callback(self._after_tasks.discard)

    async def _wait_kernel_started(self) -> None:
        for _ in range(300):
            if self.k.started:
                return
            await self.k.clock.sleep(0.2)

    # ================================================================== cheap state probes
    def last_social_ts(self) -> float:
        return self._last_social

    def pace_rng(self):
        """RNG for timing draws: an injected one (kernel.chance_rng), else the configured social seed
        (simulations), else the life's RNG."""
        r = getattr(self.k, "chance_rng", None)
        if r is not None:
            return r
        return self.rng if self.cfg.get("seed") is not None else C.rng_of(self.k)

    def burst_gap(self, person_key: Optional[str]) -> Optional[float]:
        if not person_key:
            return None
        try:
            return self._baseline(person_key).gap_median
        except Exception:
            return None

    def patience(self) -> float:
        try:
            return float(self.k.affect.modulators().get("patience", 1.0)) if self.k.affect else 1.0
        except Exception:
            return 1.0

    def mind_mode(self) -> str:
        try:
            return str(self.k.mind.mode()) if self.k.mind is not None else "active"
        except Exception:
            return "active"

    def _asleep(self) -> bool:
        try:
            if self.k.affect is not None and self.k.affect.snapshot().asleep:
                return True
        except Exception:
            pass
        return self.mind_mode() == "asleep"

    def _frozen(self) -> bool:
        try:
            return bool(self.k.gov is not None and self.k.gov.frozen)
        except Exception:
            return False

    def _learning_paused(self) -> bool:
        try:
            return bool(self.k.gov is not None and self.k.gov.learning_paused)
        except Exception:
            return False

    # ================================================================== threads
    def _resolve_entities(self, msg: IncomingMessage, now: float) -> None:
        """docs/22: transport handles -> entities. Everything after this point is keyed by entity ids:
        the sender entity, and the space entity (the group, or the person for a direct message)."""
        w = getattr(self.k, "world", None)
        if w is None or msg.sender_entity is not None:
            return
        if self._is_self(msg):
            # my own message (another device / platform echo): I am the sender; a DM's space is the other party
            from alive.world import SELF
            msg.sender_entity = SELF
            if msg.chat_type == "group":
                msg.space_entity = w.group(msg.channel, str(msg.chat_id))
            else:
                msg.space_entity = w.person(msg.channel, str(msg.chat_id))
            return
        msg.sender_entity = w.person(msg.channel, str(msg.sender_id), msg.sender_name or "")
        if msg.chat_type == "group":
            title = ((msg.raw or {}).get("chat_title") or "") if msg.raw else ""
            msg.space_entity = w.group(msg.channel, str(msg.chat_id), title)
            w.note_member(msg.space_entity, msg.sender_entity, msg.ts or now)
        else:
            msg.space_entity = msg.sender_entity
        w.seen(msg.channel, str(msg.chat_id), msg.chat_type, msg.space_entity, msg.ts or now)

    def _thread_for(self, msg: IncomingMessage) -> ThreadState:
        tid = msg.thread_id
        st = self._threads.get(tid)
        if st is not None and (st.channel, st.chat_id) != (msg.channel, msg.chat_id):
            # same entity, different transport route: reply where they spoke last
            st.channel, st.chat_id = msg.channel, msg.chat_id
        if st is None:
            st = ThreadState(tid, msg.channel, msg.chat_id, msg.chat_type, lines_max=int(self.cfg.get("cache_lines", 100)))
            self._threads[tid] = st
            self._evict_threads()
        else:
            self._threads.move_to_end(tid)
        title = (msg.raw or {}).get("chat_title") if msg.raw else None
        if title and title != st.title:
            st.title = str(title)
        if not st.title and msg.chat_type == "private":
            st.title = msg.sender_name
        return st

    def _thread_by_id(self, thread_id: str) -> ThreadState:
        st = self._threads.get(thread_id)
        if st is not None:
            return st
        w = getattr(self.k, "world", None)
        route = w.route(thread_id) if w is not None and w.entity(thread_id) is not None else None
        if route is not None:                 # an entity id: reach it on its latest route
            channel, chat_id, ctype = route
        else:
            channel, _, chat_id = thread_id.partition(":")
            ad = self.adapters.get(channel)
            ctype = ad.chat_type_of(chat_id) if ad else "private"
        st = ThreadState(thread_id, channel, chat_id, ctype, lines_max=int(self.cfg.get("cache_lines", 100)))
        self._threads[thread_id] = st
        self._evict_threads()
        return st

    def one_to_one(self, st: ThreadState) -> bool:
        """A fact about the space: is there exactly one other person in it (so every line is to me)?"""
        w = getattr(self.k, "world", None)
        if w is not None:
            try:
                if w.entity(st.thread_id) is not None:      # kind() defaults to "person" for unknown ids
                    return w.kind(st.thread_id) == "person"
            except Exception:
                pass
        return st.chat_type != "group"

    def present_others(self, st: ThreadState, now: float) -> float:
        """How many other people are around in the space: members if the world knows them, else recent speakers."""
        if self.one_to_one(st):
            return 1.0
        active = {l.speaker for l in st.window(now, 1800.0) if not l.mine and l.speaker}
        n = len(active)
        w = getattr(self.k, "world", None)
        if w is not None:
            try:
                members = w.members_of(st.thread_id)
                if members:     # lurkers count a little: they can read, but most of them won't speak
                    n = max(n, n + 0.25 * max(0, len(members) - 1 - n))
            except Exception:
                pass
        return float(max(1.0, n))

    def _evict_threads(self) -> None:
        while len(self._threads) > self.max_threads:
            for tid in list(self._threads):
                if tid not in self._actors:
                    self._threads.pop(tid)
                    break
            else:
                return

    async def _ensure_lines(self, st: ThreadState) -> None:
        if st.lines is not None:
            return
        async with st.load_lock:
            if st.lines is not None:
                return
            await self.writer.flush()
            rows = await self.k.db.fetchall(
                "SELECT * FROM soc_messages WHERE thread_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                (st.thread_id, st.lines_max))
            lines = [ChatLine(sender=r["sender_name"] or "", person_key=r["sender_key"] or "", text=r["text"] or "",
                              ts=r["ts"], mine=bool(r["mine"]), msg_id=r["msg_id"] or "") for r in reversed(rows)]
            st.lines = deque(lines, maxlen=st.lines_max)
            if not st.recent:
                from alive.social.decision import LineView
                for l in lines:
                    st.recent.append(LineView(l.ts, l.person_key, l.mine))
                if lines:
                    st.last_activity = max(st.last_activity, lines[-1].ts)
                    st.last_from_me = lines[-1].mine
            for l in lines:
                if l.mine and l.msg_id:
                    st.add_my_id(l.msg_id)

    async def _load_threads(self) -> None:
        rows = await self.k.db.fetchall("SELECT * FROM soc_threads ORDER BY updated DESC LIMIT ?", (self.max_threads,))
        for r in reversed(rows):
            st = ThreadState(r["thread_id"], r["channel"], r["chat_id"], r["chat_type"] or "private", r["title"] or "",
                             lines_max=int(self.cfg.get("cache_lines", 100)))
            st.last_in, st.last_out = r["last_in"] or 0.0, r["last_out"] or 0.0
            st.last_activity = max(st.last_in, st.last_out)
            for pk in loads(r["participants"], []):
                st.participants.append(pk)
            meta = loads(r["meta"], {}) or {}
            st.muted_until = float(meta.get("muted_until", 0.0))
            self._threads[st.thread_id] = st

    def _save_thread(self, st: ThreadState) -> None:
        self._dirty_threads.add(st.thread_id)
        if not self._thread_flush_scheduled:
            self._thread_flush_scheduled = True
            try:
                asyncio.get_running_loop().call_soon(self._flush_threads)
            except RuntimeError:
                self._thread_flush_scheduled = False

    def _flush_threads(self) -> None:
        self._thread_flush_scheduled = False
        now = self.k.clock.now()
        for tid in list(self._dirty_threads):
            st = self._threads.get(tid)
            if st is None:
                continue
            self.writer.add("INSERT OR REPLACE INTO soc_threads (thread_id,channel,chat_id,chat_type,title,last_in,last_out,"
                            "participants,meta,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (st.thread_id, st.channel, st.chat_id, st.chat_type, st.title, st.last_in, st.last_out,
                             dumps(list(st.participants)), dumps({"muted_until": st.muted_until}), now))
        self._dirty_threads.clear()

    def _save_line(self, st: ThreadState, line: ChatLine, *, kind: str, reply_to: Optional[str] = None,
                   media: Optional[list] = None) -> None:
        self.writer.add("INSERT INTO soc_messages (thread_id,msg_id,sender_key,sender_name,text,ts,mine,reply_to,media,kind)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (st.thread_id, line.msg_id, line.person_key, line.sender, line.text, line.ts, int(line.mine),
                         reply_to, dumps(media) if media else None, kind))

    def _actor(self, st: ThreadState) -> ThreadActor:
        a = self._actors.get(st.thread_id)
        if a is None or a.task is None or a.task.done():
            a = ThreadActor(self, st, self.inbox_max)
            self._actors[st.thread_id] = a
            a.task = self.k.scheduler.spawn(a.run, f"soc.actor:{st.thread_id}").task
        return a

    def _actor_exited(self, a: ThreadActor) -> None:
        if self._actors.get(a.state.thread_id) is a:
            self._actors.pop(a.state.thread_id, None)
            if not a.state.load_lock.locked():
                a.state.lines = None   # reloaded from the database when the thread wakes up again

    # ================================================================== admission helpers
    def _group_cfg(self, ad, chat_id: str) -> dict:
        groups = (ad.cfg.get("groups") if ad is not None else None) or {}
        if isinstance(groups, (list, tuple)):
            groups = {str(g): {} for g in groups}
        cid = str(chat_id)
        for key in (cid, cid[1:] if cid[:1] in ("g", "-") else None, "g" + cid):
            if key is not None and key in {str(x) for x in groups}:
                v = next(v for x, v in groups.items() if str(x) == key)
                return v if isinstance(v, dict) else {}
        return {}

    def _group_allowed(self, ad, chat_id: str) -> bool:
        if ad is None:
            return True
        pol = ad.group_policy
        if pol == "disabled":
            return False
        if pol != "allowlist":
            return True
        cid = str(chat_id)
        allowed = {str(x) for x in (ad.cfg.get("allow_groups") or [])}
        groups = ad.cfg.get("groups") or {}
        allowed |= {str(x) for x in groups}
        allowed |= {t.partition(":")[2] for t in self._allowed_groups if t.startswith(ad.name + ":")}
        return cid in allowed or cid.lstrip("g") in allowed or ("g" + cid) in allowed

    def _is_self(self, msg: IncomingMessage) -> bool:
        ad = self.adapters.get(msg.channel)
        return bool(ad is not None and ad.identity.user_id and str(msg.sender_id) == str(ad.identity.user_id))

    def _blocked(self, person_key: str) -> bool:
        if person_key in set(self.cfg.get("blacklist") or []):
            return True
        p = self.relations.peek(person_key)
        return bool(p is not None and p.blocked)

    def _known_elsewhere(self, person_key: str) -> bool:
        """History with this person anywhere (docs/09 §5.1): seen in a shared space with me (world registry).
        A stranger's own one-to-one messages do not count, so pairing cannot be skipped by writing twice."""
        fn = getattr(self.k.gov, "has_history", None) if self.k.gov is not None else None
        if fn is not None:
            try:
                return bool(fn(person_key))
            except Exception:
                pass
        w = getattr(self.k, "world", None)
        try:
            return bool(w is not None and w.spaces_of(person_key))
        except Exception:
            return False

    def _is_stranger(self, item: Inbound) -> bool:
        """Pairing before one-to-one contact is a fixed safety rule, and only for people with no history with me
        anywhere: someone I know from a shared space is not a stranger when they write to me directly."""
        pk = item.msg.person_key
        if self.relations.is_owner(pk) or self.relations.is_paired(pk):
            return False
        if int(item.trust) <= int(Trust.UNTRUSTED):
            return True
        ad = self.adapters.get(item.msg.channel)
        if ad is not None and ad.dm_policy == "pairing":
            allow = {str(x) for x in (ad.cfg.get("allow_from") or [])}
            if item.msg.sender_id in allow or pk in allow:
                return False
            return not self._known_elsewhere(pk)
        return False

    # ================================================================== inbound
    async def submit_incoming(self, msg: IncomingMessage) -> None:
        k = self.k
        now = k.clock.now()
        if not msg.ts:
            msg.ts = now
        if self.dedupe.seen((msg.channel, str(msg.msg_id)), now):
            self._count("in.duplicate")
            return
        ad = self.adapters.get(msg.channel)
        if msg.chat_type == "group" and not self._group_allowed(ad, msg.chat_id):
            # before entity resolution: a group I'm not part of records no membership (membership is history,
            # and history is what makes someone known to me; docs/09 §5.1)
            self._count("in.group_not_allowed")
            return
        self._resolve_entities(msg, now)
        st = self._thread_for(msg)
        await self._ensure_lines(st)
        if self._is_self(msg):  # my own message echoed by the platform (or sent from another device)
            line = ChatLine(sender=self._names[0] if self._names else "我", person_key="", text=msg.text, ts=msg.ts,
                            mine=True, msg_id=str(msg.msg_id))
            if not st.is_mine(line.msg_id):
                st.add_line(line)
                st.last_out = now
                self._save_line(st, line, kind="self")
            return
        self._count("in.messages")
        try:
            trust = k.gov.trust_of(msg) if k.gov is not None else Trust.PUBLIC
        except Exception:
            trust = Trust.PUBLIC
        _p0, rel0 = await self.relations.get(msg.person_key)
        gap = (now - rel0.last_contact) if rel0.last_contact else None
        latency = (now - st.last_out) if (msg.chat_type == "private" and st.last_from_me and st.last_out
                                           and now - st.last_out < 12 * 3600) else None
        person, _rel = await self.relations.ensure(msg, trust, thread_id=st.thread_id, chat_type=msg.chat_type,
                                                   chat_last_out=st.last_out, last_from_me=st.last_from_me)
        names = self._names + (ad.all_names() if ad is not None else [])
        name_mentioned, vocative = T.name_mention(msg.text or "", list(dict.fromkeys(names)))
        if st.is_mine(msg.reply_to_msg_id):
            msg.reply_to_me = True
        addressed = bool(msg.mentions_me or msg.reply_to_me or vocative)
        text = msg.text or ""
        if msg.media:
            text = (text + " " + T.media_placeholder(msg.media)).strip()
        line = ChatLine(sender=person.name(), person_key=msg.person_key, text=text, ts=msg.ts, mine=False,
                        msg_id=str(msg.msg_id))
        prev_ts = st.last_activity or None
        st.seq += 1
        is_agent = bool(msg.is_bot or self.relations.is_agent(msg.person_key))
        item = Inbound(msg=msg, line=line, trust=trust, addressed=addressed, name_mentioned=name_mentioned,
                       prev_ts=prev_ts, seq=st.seq, is_agent=is_agent, latency=latency, gap=gap)
        if not is_agent:
            st.bot_hops = 0
        st.add_line(line)
        st.last_in = now
        self._last_social = max(self._last_social, now)
        st.pending += 1
        st.participants.append(msg.person_key)
        st.recent_in.append((st.seq, item))
        payload = {k2: v for k2, v in asdict(msg).items() if k2 != "raw"}
        payload.update({"person_key": msg.person_key, "thread_id": st.thread_id, "addressed": addressed,
                        "sender_display": person.name(),
                        "closeness": round(self.relations.closeness_at(rel0, now), 4) if rel0 is not None else 0.0})
        raw = msg.raw or {}
        if raw:
            payload["raw"] = {k2: raw[k2] for k2 in ("chat_title", "edited", "alive_hint") if k2 in raw}
        item.event_id = self._emit_bg(EV.MESSAGE_IN, payload, trust=trust, thread_id=st.thread_id,
                                      source=f"channel:{msg.channel}")
        self._save_line(st, line, kind="in", reply_to=msg.reply_to_msg_id, media=msg.media)
        self._save_thread(st)
        self._actor(st).deliver(item)

    # ================================================================== decision
    def features(self, st: ThreadState, items: Sequence[Inbound], now: float) -> GateFeatures:
        k = self.k
        texts = [i.msg.text or "" for i in items]
        f = GateFeatures()
        f.mentions_name = 1.0 if any(i.name_mentioned for i in items) else 0.0
        f.at_others = 1.0 if any(i.msg.mentions_others and not i.addressed for i in items) else 0.0
        f.has_question = 1.0 if any(T.has_question(t) for t in texts) else 0.0
        f.all_media = 1.0 if items and all(i.msg.media_only or T.is_emoji_only(i.msg.text) for i in items) else 0.0
        win = st.window(now, 300.0)
        f.msgs_5min = float(len(win))
        f.bot_share_5min = (sum(1 for l in win if l.mine) / len(win)) if win else 0.0
        f.secs_since_bot = (now - st.last_out) if st.last_out else 3600.0
        f.pending = float(len(items))
        closes = []
        for pk in dict.fromkeys(i.msg.person_key for i in items):
            r = self.relations.peek_rel(pk)
            if r is not None:
                closes.append(self.relations.closeness_at(r, now))
        f.closeness = sum(closes) / len(closes) if closes else 0.1
        f.interest = T.interest_similarity(" ".join(texts), self._interest_sets) if self._interest_sets else 0.0
        try:
            needs = k.motivation.needs() if k.motivation is not None else {}
            f.social_need = float((needs.get("social") or {}).get("pressure", 0.0))
        except Exception:
            pass
        try:
            snap = k.affect.snapshot() if k.affect is not None else None
            if snap is not None:
                f.social_battery, f.energy, f.arousal, f.pleasure = snap.social_battery, snap.energy, snap.A, snap.P
            f.willingness = float((k.affect.modulators() or {}).get("willingness", 0.0)) if k.affect else 0.0
        except Exception:
            pass
        f.mode = self.mind_mode()
        # air-reading facts (docs/07 §3): who the batch is to, who is around, who is talking to whom
        one = self.one_to_one(st)
        f.addressed = 1.0 if (one or any(i.addressed for i in items)) else 0.0
        f.question_to_me = 1.0 if (f.has_question and f.addressed) else 0.0
        f.addressed_other = 1.0 if (not one and any((i.msg.mentions_others and not i.addressed) or
                                                    (i.msg.reply_to_msg_id and not i.msg.reply_to_me)
                                                    for i in items)) else 0.0
        f.n_others = self.present_others(st, now)
        recent = list(st.recent)[-12:]
        speakers = [i.msg.person_key for i in items]
        f.followup = followup_strength(recent=recent, batch_speakers=speakers, now=now, my_last_out=st.last_out,
                                       my_last_targets=st.my_last_targets, closeness=f.closeness)
        f.subconversation = 0.0 if one else subconversation_strength(recent, speakers, now)
        f.ack_only = 1.0 if ack_only(items) and not f.has_question else 0.0
        f.heat = heat_of(items)
        win30 = st.window(now, 1800.0)
        f.my_share = (sum(1 for l in win30 if l.mine) / len(win30)) if win30 else 0.0
        f.talk_value = self.talk_value(st, now)
        return f

    def talk_value(self, st: ThreadState, now: Optional[float] = None) -> float:
        """How readily I chime in unasked: persona temperament × the owner's per-space setting × today's chattiness.
        It only weighs lines that are not to me (decision.air_terms); 0 makes a space read-only (owner setting).
        No per-platform or per-space-type factor and no hour windows: time of day acts through state (energy,
        drowsiness, sleep) and the people present (docs/07 §3, ADR-0007)."""
        now = self.k.clock.now() if now is None else now
        ad = self.adapters.get(st.channel)
        if st.thread_id in self._talk_override:
            tv = self._talk_override[st.thread_id]
        else:
            tv = self.persona_talk_value
            g = self._group_cfg(ad, st.chat_id)
            tv *= float(g.get("talk_value", 1.0))
        tex = getattr(self.k, "texture", None)   # docs/21 §4: today's chattiness
        if tex is not None:
            tv *= tex.multiplier("chattiness", 0.8)
        return max(0.0, tv)

    def decide(self, st: ThreadState, batch: Sequence[Inbound], now: float, *, deferred: bool = False,
               recheck: bool = False, rng=None) -> Verdict:
        """How I attend to a batch I'm looking at (sync, no I/O). One path for every space (docs/07 §3, ADR-0007):
        hard stops only for safety / ownership (kill switch, blacklist, strangers, owner mute, read-only, bot hops);
        everything else is one draw on the engagement logit -> deliberate look | glance | take in."""
        v = Verdict(items=[])
        if self._frozen():          # kill switch: no acts and no learning side effects
            v.reason = "frozen"
            return v
        items = [i for i in batch if not self._blocked(i.msg.person_key)]
        v.items = list(items)       # blacklisted senders never reach memory / learners
        if not items:
            v.reason = "blacklist"
            return v
        humans = [i for i in items if not i.is_agent]
        bots = [i for i in items if i.is_agent]
        bots_ok = [i for i in bots if i.addressed and st.bot_hops < self.hop_limit]
        if not humans and not bots_ok:
            v.reason = "bot_hop_limit" if any(i.addressed for i in bots) else "bots_only"
            return v
        items = sorted(humans + bots_ok, key=lambda i: i.seq)
        v.items = items
        one = self.one_to_one(st)
        v.addressed = one or any(i.addressed for i in items)
        if one and self._is_stranger(items[-1]):
            v.reason, v.pairing = "stranger", True
            return v
        if st.muted_until > now:
            v.reason = "cooldown"
            return v
        f = self.features(st, items, now)
        v.talk_value = round(f.talk_value, 4)
        if f.talk_value <= 0 and not one:   # owner setting: a shared space where I only read. (talk_value weighs
            v.reason = "talk_value_zero"    # lines not addressed to me; in a one-to-one space every line is to me)
            return v
        if self._asleep():
            v.asleep = True
            v.stage = "asleep"
            if recheck:
                v.outcome, v.reason = "wait", "asleep"
                v.retry_after = self.air.notice_delay(st, items[-1], now) or self.asleep_recheck_s
            elif v.addressed:       # something to me woke me up: a sleepy glance first
                v.outcome, v.reason = "quick_read", "asleep"
            else:
                v.reason = "asleep"
            return v
        kind, p, p_glance, logit, terms = engagement(f, rng or C.rng_of(self.k), maibot_w=self.maibot_w,
                                                     alive_w=self.alive_w, air_w=self.air_w,
                                                     use_alive=self.use_alive_features)
        v.p, v.features, v.terms = round(p, 4), f.as_dict(), terms
        v.gate = {"p_glance": round(p_glance, 4), "logit": round(logit, 4), "draw": kind}
        v.stage = "air"
        v.must = False              # nothing obliges me to speak (INV-28); being addressed weighs, it doesn't force
        if deferred and kind == "take_in":
            kind = "glance"         # I came back to it on purpose: at least read it
        if kind == "look":
            v.outcome = "compose"
            v.reason = ("deferred" if deferred else "addressed_to_me" if f.addressed and not one else
                        "question" if f.question_to_me else "followup" if f.followup > 0.5 else
                        "to_me" if one else "look")
        elif kind == "glance":
            v.outcome, v.reason = "quick_read", ("ack_glance" if f.ack_only else "glance")
        else:
            v.reason = ("closing" if f.ack_only else "subconversation" if f.subconversation > 0.5 else
                        "addressed_to_other" if f.addressed_other else "take_in")
        return v

    def _emit_decision(self, st: ThreadState, v: Verdict, batch: Sequence[Inbound], outcome: str) -> str:
        payload = {"outcome": outcome, "stage": v.stage, "reason": v.reason, "must": v.must, "mentioned": v.addressed,
                   "p": v.p, "talk_value": v.talk_value, "features": v.features,
                   "terms": v.terms, "gate": v.gate, "chat_type": st.chat_type, "n": len(batch),
                   "batch": [i.msg.msg_id for i in batch][-20:], "asleep": v.asleep,
                   "cues": list(v.cues or [])}
        self._count(f"decision.{outcome}")
        return self._emit_bg(EV.DECISION, payload, thread_id=st.thread_id,
                             causal_parent=batch[-1].event_id if batch else None)

    def build_task(self, st: ThreadState, items: Sequence[Inbound], v: Verdict) -> ReplyTask:
        ids = {i.line.msg_id for i in items}
        hist = [l for l in (st.lines or []) if l.mine or l.msg_id not in ids][-self.history_n:]
        participants = list(dict.fromkeys(i.msg.person_key for i in items))
        trust = min((i.trust for i in items), default=Trust.PUBLIC)
        task = ReplyTask(thread_id=st.thread_id, channel=st.channel, chat_type=st.chat_type, chat_title=st.title,
                         participants=participants, history=hist, new_lines=[i.line for i in items],
                         mentioned=v.addressed, decision_reason=prompts.REASON.get(v.reason, v.reason),
                         max_segments=self.max_segments, trust=Trust(int(trust)))
        if "cues" in getattr(ReplyTask, "__dataclass_fields__", {}):  # forward compatible (requested field)
            task.cues = list(v.cues or [])
        return task

    # ================================================================== level-0 understanding cues (docs/17)
    def _baseline(self, person_key: str) -> PersonBaseline:
        b = self._baselines.get(person_key)
        if b is None:
            p = self.relations.peek(person_key)
            b = PersonBaseline((p.meta.get("baseline") if p is not None else None) or None)
            self._baselines[person_key] = b
            while len(self._baselines) > 5000:
                self._baselines.popitem(last=False)
        else:
            self._baselines.move_to_end(person_key)
        return b

    def _observe_baselines(self, batch: Sequence[Inbound]) -> None:
        per: dict = {}
        last_ts: dict = {}
        for i in batch:
            if i.observed or i.is_agent:
                continue
            i.observed = True
            b = self._baseline(i.msg.person_key)
            b.observe(i.msg.text or "", i.msg.ts)
            if i.latency is not None:
                b.observe_latency(i.latency)
            if i.msg.person_key in last_ts:           # how they pace the pieces of one utterance
                b.observe_gap(i.msg.ts - last_ts[i.msg.person_key])
            last_ts[i.msg.person_key] = i.msg.ts
            per[i.msg.person_key] = per.get(i.msg.person_key, 0) + 1
        for pk, n in per.items():
            b = self._baseline(pk)
            b.observe_burst(n)
            p = self.relations.peek(pk)
            if p is not None and (b.n % 5 == 0 or b.n < 5):
                p.meta["baseline"] = b.to_dict()
                self.relations.save(p, None)

    def _compute_cues(self, st: ThreadState, items: Sequence[Inbound], now: float) -> list:
        cues: list = []
        target = self._reply_target(st, items)
        if target is not None and not target.is_agent:
            pk = target.msg.person_key
            mine = [i for i in items if i.msg.person_key == pk]
            r = self.relations.peek_rel(pk)
            share = None
            if r is not None and sum(r.hours) > 0:
                share = r.hours[int(self.k.clock.hour_of_day(now)) % 24] / sum(r.hours)
            cues += deviation_cues(self._baseline(pk), [i.msg.text or "" for i in mine], name=target.line.sender,
                                   latency=mine[0].latency if mine else None, hour_share=share,
                                   gap_since_contact=mine[0].gap if mine else None)
        if st.chat_type == "group":
            names = {}
            for ln in list(st.lines or [])[-60:]:
                if ln.person_key:
                    names[ln.person_key] = ln.sender
            last = items[-1] if items else None
            addressed = ("我" if last and last.addressed else ("别人" if last and last.msg.mentions_others else ""))
            st.group_ctx = group_context(list(st.lines or []), now, names=names, last_addressed=addressed)
            tex = getattr(self.k, "texture", None)
            sens = tex.multiplier("sensitivity", 0.8) if tex is not None else 1.0
            cues += group_cues(st.group_ctx, rng=C.rng_of(self.k), sensitivity=sens)
        return cues

    async def _quick_read(self, task: ReplyTask) -> dict:
        try:
            return await self.k.mind.quick_read(task) or {}
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("quick_read failed", exc_info=True)
            return {}

    @staticmethod
    def _qr_urgent(qr: dict) -> bool:
        return bool(qr.get("crisis") or qr.get("crisis_hint")) or float(qr.get("urgency") or 0) >= 0.8

    def _glance_finds(self, qr: dict, v: Verdict, rng=None) -> bool:
        """After a glance (S1 read): is there something here I want to take up? Urgent/crisis always (safety);
        otherwise a draw: questions to me, strong feeling, news, affection or an insult in it make it likely; a
        plain acknowledgement rarely does (research note: ~0.25 for a closing word with a close tie)."""
        if self._qr_urgent(qr):
            return True
        intent = str(qr.get("intent") or "").lower()
        f = v.features or {}
        z = -1.6
        z += 2.2 * (1.0 if (qr.get("question") or intent in ("question", "help", "request", "ask")) else 0.0)
        z += 1.4 * (1.0 if qr.get("addressed_to_me") else 0.0) * (1.0 - float(f.get("addressed", 0.0) or 0.0) * 0.5)
        z += 2.5 * max(0.0, float(qr.get("urgency") or 0.0) - 0.2)
        z += 1.6 * max(0.0, -float(qr.get("sentiment") or 0.0) - 0.2)
        z += 1.2 * (1.0 if (qr.get("bad_news") or qr.get("good_news") or qr.get("affectionate") or qr.get("insulted"))
                    else 0.0)
        z += 1.0 * (float(f.get("closeness", 0.3) or 0.3) - 0.3)
        z -= 1.2 * float(f.get("ack_only", 0.0) or 0.0)
        return C.bernoulli(rng or C.rng_of(self.k), C.sigmoid(z))

    # ================================================================== batch processing (called by actors)
    async def _process_batch(self, actor: ThreadActor, batch: list, *, deferred: bool = False) -> None:
        st = actor.state
        k = self.k
        await self._wait_kernel_started()
        now = k.clock.now()
        recheck = deferred and not any(i.seq > getattr(actor, "_deferred_seq", 0) for i in batch)
        v = self.decide(st, batch, now, deferred=deferred, recheck=recheck)
        if v.outcome != "wait" and v.items and not v.pairing:
            self.air.linger(st, now, spoke=False, person_key=v.items[-1].msg.person_key,
                            interest=float((v.features or {}).get("interest", 0.3) or 0.3))
        if v.outcome in ("compose", "quick_read"):
            try:
                st.last_cues = self._compute_cues(st, v.items, now)
            except Exception:
                log.debug("cue computation failed", exc_info=True)
                st.last_cues = []
            v.cues = st.last_cues
        self._observe_baselines(batch)
        if v.outcome == "wait":
            self._defer(actor, batch, v)
            return
        if v.outcome == "silent":
            self._emit_decision(st, v, batch, "silent")
            if v.pairing:
                await self._handle_stranger(st, v.items[-1])
            self._after_exchange(st, v.items, [], None, "silent")
            self._notify_observe(st, v)
            return
        items = v.items
        qr: Optional[dict] = None
        if v.outcome == "quick_read":
            qr = await self._quick_read(self.build_task(st, items, v))
            if v.asleep:
                if not self._qr_urgent(qr):
                    if v.addressed:       # to me: I'll deal with it after I wake up (sampled)
                        v.outcome, v.reason = "wait", "asleep"
                        v.retry_after = self.air.notice_delay(st, items[-1], now) or self.asleep_recheck_s
                        self._defer(actor, batch, v)
                    else:
                        self._emit_decision(st, v, batch, "silent")
                        self._after_exchange(st, items, [], qr, "silent")
                    return
                v.reason = "urgent_while_asleep"
            elif not self._glance_finds(qr, v):
                v.stage, v.reason = "quick_read", ("ack_let_pass" if v.reason == "ack_glance" else "glance_let_pass")
                self._emit_decision(st, v, batch, "silent")
                self._after_exchange(st, items, [], qr, "silent")
                self._notify_observe(st, v)
                return
            else:
                v.stage, v.reason = "quick_read", ("ack_glance" if v.reason == "ack_glance" else "quick_read")
        result, items, qr = await self._compose(actor, st, items, v, qr)
        await self._deliver_result(actor, st, items, v, qr, result, batch)

    def _notify_observe(self, st: ThreadState, v: Verdict, unsaid: str = "", damp: float = 1.0) -> None:
        """Non-dialogue principle (docs/22): what I did not attend to right now is still perceived. The mind
        files it in its workspace (no LLM) and may act on it later, or never."""
        obs = getattr(self.k.mind, "observe", None)
        if obs is None or not v.items or st.chat_type == "private" and v.reason in ("stranger", "blacklist"):
            return
        if v.reason in ("frozen", "blacklist", "stranger", "self", "bot_hop_limit"):
            return
        try:
            task = self.build_task(st, v.items, v)
        except Exception:
            return

        async def run():
            try:
                await obs(task, unsaid=unsaid, damp=damp)
            except TypeError:                     # minimal minds (fakes) take only the task
                await obs(task)
        self.k.scheduler.spawn(run, f"soc.observe:{st.thread_id[-12:]}")

    def _defer(self, actor: ThreadActor, batch: list, v: Verdict) -> None:
        actor.deferred = list(batch)
        actor._deferred_seq = max((i.seq for i in batch), default=0)
        actor.recheck_at = self.k.clock.now() + (v.retry_after or self.asleep_recheck_s)
        self._emit_decision(actor.state, v, batch, "wait")

    async def _compose(self, actor: ThreadActor, st: ThreadState, items: list, v: Verdict,
                       qr: Optional[dict]) -> tuple:
        """compose_reply with in-flight handling: interrupt (cancel + recompose) and steer (merge + recompose)."""
        k = self.k
        restarts = steers = 0
        qr_task = None
        comp = None
        need_qr = qr is None
        try:
            while True:
                task = self.build_task(st, items, v)
                fl = InFlight(participants={i.msg.person_key for i in items},
                              context_text=" ".join(i.msg.text or "" for i in items)[-600:], started=k.clock.now())
                actor.inflight = fl
                if need_qr and qr_task is None:
                    qr_task = asyncio.ensure_future(self._quick_read(task))
                comp = asyncio.ensure_future(k.mind.compose_reply(task))
                interrupted = await actor._wait_task_or_interrupt(comp, fl)
                if interrupted and restarts < self.max_restarts:
                    comp.cancel()
                    await asyncio.gather(comp, return_exceptions=True)
                    new = actor.take_from_inbox(fl.interrupts + fl.steer)
                    items = items + [i for i in new if i not in items]
                    restarts += 1
                    v.reason = "interrupt"
                    self._count("inflight.interrupt")
                    continue
                try:
                    result = await comp
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("compose_reply failed for %s", st.thread_id, exc_info=True)
                    result = ComposeResult(silent=True, reason="compose_error")
                if fl.steer and steers < self.max_steers:
                    new = actor.take_from_inbox(fl.steer)
                    if new:
                        items = items + [i for i in new if i not in items]
                        steers += 1
                        v.reason = "steer"
                        self._count("inflight.steer")
                        continue
                break
            if qr_task is not None:
                try:
                    qr = await qr_task
                except Exception:
                    qr = {}
        except BaseException:
            for t in (comp, qr_task):
                if t is not None and not t.done():
                    t.cancel()
            raise
        fl.phase = "sending"
        return result, items, qr

    def _reply_target(self, st: ThreadState, items: Sequence[Inbound]) -> Optional[Inbound]:
        for i in reversed(items):
            if i.addressed and not i.is_agent:
                return i
        humans = [i for i in items if not i.is_agent]
        return (humans or list(items))[-1] if items else None

    async def _deliver_result(self, actor: ThreadActor, st: ThreadState, items: list, v: Verdict,
                              qr: Optional[dict], result: ComposeResult, batch: list) -> None:
        k = self.k
        now = k.clock.now()
        target = self._reply_target(st, items)
        person_key = target.msg.person_key if target else None
        segs = [] if result.silent else [s for s in (result.segments or []) if isinstance(s, str) and s.strip()]
        ad = self.adapters.get(st.channel)
        reaction = result.reaction
        if reaction and not (ad is not None and ad.caps.reactions):
            # channel cannot react (e.g. OneBot): an emoji-only message is the human equivalent
            if not segs and bool(self.cfg.get("reaction_fallback_text", True)):
                segs = [reaction]
            reaction = None
        if not segs and not reaction:
            v.stage = "mind"
            v.reason = result.reason[:80] if result.reason else "mind_silent"
            self._emit_decision(st, v, batch, "silent")
            self._after_exchange(st, items, [], qr, "silent")
            if v.reason != "yield_floor":          # yield_floor already filed its own item
                self._notify_observe(st, v, damp=0.5)
            return
        if reaction and not segs:
            self._emit_decision(st, v, batch, "react")
            ok = await self._react(st, ad, target, reaction, cause=result.event_id)
            self._after_exchange(st, items, [], qr, "react" if ok else "silent", reaction=reaction if ok else None)
            if v.round is not None and ok:
                v.round.replied = True
            return
        caps = {"external"}
        if any(int(i.trust) <= int(Trust.UNTRUSTED) for i in items):
            caps.add("untrusted")
        if getattr(result, "private_used", None) is not False:
            caps.add("private")          # from what the composed context actually held (INV-34), not the space type
        try:
            act = await k.gov.check_action(ActRequest(kind="send_message", perm=int(PermTier.T2_EXTERNAL_COMM),
                                                      thread_id=st.thread_id, person_key=person_key, text=segs[0],
                                                      initiated_by="reactive", session_caps=caps,
                                                      meta={"chat_type": st.chat_type, "segments": len(segs)}))
        except Exception:  # fail closed: a broken governance check never lets a message out
            log.warning("check_action failed; staying silent", exc_info=True)
            act = None
        if act is None or not act.allow:
            v.stage, v.reason = "gov", (f"denied:{act.reason or act.rule}" if act is not None else "gov_error")
            self._emit_decision(st, v, batch, "silent")
            self._after_exchange(st, items, [], qr, "silent")
            if act is not None:
                self._notify_observe(st, v, unsaid=" ".join(segs)[:80])
            return
        dec_id = self._emit_decision(st, v, batch, "respond")
        reply_to = result.reply_to_msg_id
        if reply_to is None and target is not None and ad is not None and ad.caps.reply:
            later_others = any(lv.ts > target.msg.ts and lv.speaker != target.msg.person_key and not lv.mine
                               for lv in list(st.recent)[-10:])
            if st.chat_type == "group" and (target.addressed or later_others):
                reply_to = target.msg.msg_id
        context = " ".join(i.msg.text or "" for i in items) + " " + " ".join(segs)
        incoming = "\n".join(i.msg.text or "" for i in items)
        think_elapsed = max(0.0, now - max(i.msg.ts for i in items)) if items else 0.0
        res = await self._send_impl(st, segs, kind="reply", reply_to=reply_to, style=result.style,
                                    person_key=person_key, abort_on_new=True,
                                    participants={i.msg.person_key for i in items}, incoming=incoming,
                                    think_elapsed=think_elapsed, context_text=context,
                                    cause=result.event_id or dec_id, topic=(target.msg.text if target else ""),
                                    withheld=list(getattr(result, "withheld", None) or []))
        if reaction and res.get("sent"):
            await self._react(st, ad, target, reaction, cause=result.event_id)
        if res.get("sent"):
            st.my_last_targets = list(dict.fromkeys(i.msg.person_key for i in items))
            if any(i.is_agent for i in items):
                st.bot_hops += 1
        my_lines = [l for l in (st.lines or []) if l.mine and l.msg_id in set(res.get("msg_ids") or [])]
        self._after_exchange(st, items, my_lines, qr, "respond" if res.get("sent") else "silent")

    async def _react(self, st: ThreadState, ad, target: Optional[Inbound], emoji: str, cause: str = "") -> bool:
        if ad is None or target is None or not ad.caps.reactions:
            return False
        try:
            d = await self.k.gov.check_action(ActRequest(kind="react", perm=int(PermTier.T2_EXTERNAL_COMM),
                                                         thread_id=st.thread_id, person_key=target.msg.person_key,
                                                         text=emoji, initiated_by="reactive"))
            if not d.allow:
                return False
        except Exception:  # fail closed
            log.warning("check_action(react) failed", exc_info=True)
            return False
        try:
            ok = await ad.react(st.chat_id, target.msg.msg_id, emoji)
        except Exception:
            log.warning("react failed on %s", st.thread_id, exc_info=True)
            ok = False
        if ok:
            self._emit_bg(EV.OUTCOME_REACT, {"msg_id": target.msg.msg_id, "emoji": emoji, "person_key": target.msg.person_key},
                          thread_id=st.thread_id, causal_parent=cause or None)
            where = "私聊" if st.chat_type == "private" else f"群「{st.title or st.chat_id}」"
            summary = prompts.OUTCOME_REACT.format(where=where, who=target.line.sender, emoji=emoji)
            self._record_outcome(summary, cause)
        return ok

    async def _handle_stranger(self, st: ThreadState, item: Inbound) -> None:
        if not bool(self.cfg.get("pairing.notify", True)):
            return
        code, created = await self.relations.request_pairing(st.channel, item.msg.person_key, item.msg.sender_name,
                                                             st.chat_id)
        if code is None:
            self._count("pairing.rejected_full")
            return
        if not created:
            return
        self._emit_bg("soc.pairing_requested", {"code": code, "channel": st.channel, "person_key": item.msg.person_key,
                                                "name": item.msg.sender_name}, thread_id=st.thread_id,
                      trust=Trust.UNTRUSTED)
        log.info("pairing requested by %s (%s) on %s: code %s", item.msg.sender_name, item.msg.person_key, st.channel, code)
        await self._send_impl(st, [prompts.PAIRING_NOTICE.format(code=code)], kind="system", reply_to=None, style=None,
                              person_key=item.msg.person_key, abort_on_new=False, participants=set(),
                              incoming=item.msg.text or "", think_elapsed=0.0, context_text="", cause=item.event_id)

    # ================================================================== send pipeline
    async def send(self, thread_id: str, segments: Sequence[str], *, kind: str = "reply",
                   reply_to: Optional[str] = None, style: Optional[dict] = None, person_key: Optional[str] = None,
                   abort_on_new: bool = True, plan_id: Optional[str] = None,
                   withheld: Optional[Sequence[str]] = None) -> dict:
        """Embodied send (interface). Extra: ``plan_id`` links an initiative to its OutreachPlan.
        kind: reply | initiative | outreach | system | ... (initiative/outreach are tracked for reply/ignore).
        ``withheld``: what must not be said here (INV-34); by default what the mind left out of its most recent
        composition for this space (Mind.withheld_for)."""
        if withheld is None:
            fn = getattr(self.k.mind, "withheld_for", None) if self.k.mind is not None else None
            try:
                withheld = list(fn(thread_id) or []) if fn is not None else []
            except Exception:
                withheld = []
        st = self._thread_by_id(thread_id)
        await self._ensure_lines(st)
        participants = {person_key} if person_key else set(list(st.participants)[-3:])
        incoming = ""
        if kind not in INITIATIVE_KINDS and st.lines:
            tail = []
            for l in reversed(st.lines):
                if l.mine:
                    break
                tail.append(l.text)
            incoming = "\n".join(reversed(tail))
        think = max(0.0, self.k.clock.now() - st.last_in) if st.last_in else 0.0
        return await self._send_impl(st, list(segments or []), kind=kind, reply_to=reply_to, style=style,
                                     person_key=person_key, abort_on_new=abort_on_new, participants=participants,
                                     incoming=incoming, think_elapsed=think,
                                     context_text=" ".join(list(segments or [])), cause="", plan_id=plan_id,
                                     withheld=withheld)

    def _quota_ok(self, st: ThreadState, ad) -> tuple:
        q = ad.quotas
        now = self.k.clock.now()
        ts = [t for t in self._init_counts.get((st.channel, st.chat_id), []) if now - t < 31 * 86400]
        day = sum(1 for t in ts if now - t < 86400)
        if st.chat_type == "private" and q.proactive_private_per_day is not None and day >= q.proactive_private_per_day:
            return False, "quota_private_day"
        if st.chat_type == "group":
            if q.proactive_group_per_day is not None and day >= q.proactive_group_per_day:
                return False, "quota_group_day"
            month = sum(1 for t in ts if now - t < 30 * 86400)
            if q.proactive_group_per_month is not None and month >= q.proactive_group_per_month:
                return False, "quota_group_month"
        return True, ""

    def _should_abort(self, st: ThreadState, start_seq: int, participants: set, initiative: bool,
                      context_text: str) -> Optional[str]:
        for seq, it in list(st.recent_in):
            if seq <= start_seq:
                continue
            from_participant = it.msg.person_key in participants or it.addressed
            if initiative and (from_participant or st.chat_type == "private"):
                return "new_message"
            if from_participant and shaping.is_correction_like(it.msg, context_text):
                return "correction"
        return None

    async def _typing_wait(self, ad, chat_id: str, seconds: float) -> None:
        clock = self.k.clock
        if seconds <= 0:
            return
        if not ad.caps.typing or seconds < 0.4:
            await clock.sleep(seconds)
            return
        left = seconds
        while left > 1e-6:
            try:
                await ad.send_typing(chat_id)
            except Exception:
                log.debug("typing failed", exc_info=True)
            step = min(left, float(self.cfg.get("typing_refresh_s", 4.5)))
            await clock.sleep(step)
            left -= step

    async def _send_impl(self, st: ThreadState, segments: list, *, kind: str, reply_to: Optional[str],
                         style: Optional[dict], person_key: Optional[str], abort_on_new: bool, participants: set,
                         incoming: str, think_elapsed: float, context_text: str, cause: str = "",
                         plan_id: Optional[str] = None, topic: str = "", withheld: Sequence[str] = ()) -> dict:
        """``withheld``: what I know but must not say here (its audience isn't here, INV-34): governance's leak
        check gets it as ``private_from_others``."""
        k = self.k
        out = {"sent": 0, "aborted": False, "msg_ids": [], "reason": ""}
        ad = self.adapters.get(st.channel)
        if ad is None:
            out.update(aborted=True, reason="no_channel")
            return out
        if self._frozen():
            out.update(aborted=True, reason="frozen")
            return out
        if kind == "system":  # fixed notices are sent verbatim, never split
            segs = [s for s in segments if isinstance(s, str) and s.strip()]
        else:
            n_in = len([x for x in (segments or []) if isinstance(x, str) and x.strip()])
            segs = normalize_segments(segments, self.split_max if n_in <= 1 else self.max_segments, self.split_min_chars)
        if not segs:
            return out
        initiative = kind in INITIATIVE_KINDS
        if initiative:
            ok, why = self._quota_ok(st, ad)
            if not ok:
                out.update(aborted=True, reason=why)
                self._emit_bg(EV.OUTCOME_ABORTED, {"reason": why, "kind": kind, "remaining": len(segs)},
                              thread_id=st.thread_id, causal_parent=cause or None)
                return out
        disclosure = None
        inline_disclosure = False
        if kind != "system":
            try:
                disclosure = k.gov.disclosure_due(st.thread_id, k.clock.now()) if k.gov is not None else None
            except Exception:
                disclosure = None
            if disclosure:
                affirms = getattr(k.gov, "affirms_ai", None)
                if affirms is not None and any(affirms(s) for s in segs):
                    inline_disclosure = True          # said in my own words: that is the disclosure
                    disclosure = None
                else:
                    segs = segs + [disclosure]        # fallback: the standard line
        sty = dict(style or {})
        if "delay" not in sty and k.affect is not None:
            try:
                sty = {**k.affect.style(), **sty}
            except Exception:
                pass
        style_delay = float(sty.get("delay", 1.0) or 1.0)
        mode = self.mind_mode()
        try:
            energy = float(k.affect.snapshot().energy) if k.affect is not None else 0.8
        except Exception:
            energy = 0.8
        farewell = T.is_farewell(incoming) or any(T.is_farewell(s) for s in segs)
        lines_ctx = list(st.lines or [])[-8:]
        sent_texts: list = []
        spoken: list = []                 # my own lines, for memory (not the fixed disclosure notice)
        async with st.send_lock:
            start_seq = st.seq
            for idx, seg in enumerate(segs):
                is_disclosure = disclosure is not None and idx == len(segs) - 1
                ctx = {"chat_type": st.chat_type, "lines": lines_ctx, "farewell": farewell, "kind": kind,
                       "segment_index": idx, "segments": len(segs), "initiated_by": "initiative" if initiative else "reactive",
                       "disclosure": is_disclosure, "private_from_others": list(withheld or ())}
                text = seg
                if k.gov is not None and not is_disclosure:
                    try:
                        dec = await k.gov.pre_send(st.thread_id, person_key, seg, ctx)
                    except Exception:
                        log.warning("pre_send failed; not sending", exc_info=True)
                        out.update(aborted=True, reason="gov_error")
                        break
                    if not dec.allow:
                        out.update(aborted=True, reason=f"gov:{dec.reason or dec.rule or 'denied'}")
                        break
                    if dec.text:
                        text = dec.text
                if idx == 0:
                    clo = 0.4
                    if person_key:
                        r_ = self.relations.peek_rel(person_key)
                        clo = self.relations.closeness_at(r_, k.clock.now()) if r_ is not None else 0.2
                    d = self.delay.first_delay(text, incoming if kind != "system" else "", mode=mode, energy=energy,
                                               style_delay=style_delay, already_elapsed=think_elapsed, closeness=clo)
                else:
                    d = self.delay.next_delay(text, style_delay=style_delay, energy=energy)
                await self._typing_wait(ad, st.chat_id, d)
                if abort_on_new:
                    why = self._should_abort(st, start_seq, participants, initiative, context_text)
                    if why:
                        out.update(aborted=True, reason=why)
                        break
                if self._frozen():
                    out.update(aborted=True, reason="frozen")
                    break
                act_id = self._emit_bg(EV.ACT_SEND, {"text": text, "kind": kind, "segment": idx, "of": len(segs),
                                                     "person_key": person_key, "reply_to": reply_to if idx == 0 else None},
                                       thread_id=st.thread_id, causal_parent=cause or None)
                kw = {}
                if idx == 0 and person_key and st.chat_type == "group" and ad.cfg.get("at_on_reply"):
                    native = self._native_id(person_key, st.channel)
                    if native:
                        kw["at_user"] = native
                try:
                    mid = await ad.send_text(st.chat_id, text, reply_to=reply_to if idx == 0 else None, **kw)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("send on %s failed: %r", st.thread_id, e)
                    out.update(aborted=True, reason="send_error")
                    break
                mid = str(mid or new_id("m"))
                now = k.clock.now()
                line = ChatLine(sender=self._names[0] if self._names else "我", person_key="", text=text, ts=now,
                                mine=True, msg_id=mid)
                st.add_line(line)
                st.last_out = now
                self._last_social = max(self._last_social, now)
                st.pending = 0
                self._save_line(st, line, kind=kind)
                out["msg_ids"].append(mid)
                out["sent"] += 1
                sent_texts.append(text)
                if not is_disclosure:
                    spoken.append(line)
                self._emit_bg(EV.OUTCOME_SENT, {"msg_id": mid, "text": text, "kind": kind, "segment": idx,
                                                "person_key": person_key}, thread_id=st.thread_id, causal_parent=act_id)
                if (is_disclosure or (inline_disclosure and k.gov is not None and k.gov.affirms_ai(text)))                         and k.gov is not None:
                    try:
                        k.gov.disclosure_sent(st.thread_id, now)
                    except Exception:
                        pass
        if out["aborted"]:
            self._emit_bg(EV.OUTCOME_ABORTED, {"reason": out["reason"], "kind": kind, "sent": out["sent"],
                                               "remaining": len(segs) - out["sent"]},
                          thread_id=st.thread_id, causal_parent=cause or None)
            self._count("send.aborted")
        if out["sent"]:
            self._save_thread(st)
            if kind != "system":          # I stay with the space a while, waiting for the answer (air.py)
                self.air.linger(st, k.clock.now(), spoke=True, person_key=person_key)
            if person_key:
                if initiative and self.relations.peek_rel(person_key) is None:
                    await self.relations.get(person_key)          # cold cache (e.g. after a restart)
                self.relations.note_outbound(person_key, k.clock.now(), initiative=initiative)
            if initiative:
                self._note_initiative(st, person_key, plan_id, out["msg_ids"])
            if kind != "system":
                self._record_outcome(self._summary(st, kind, person_key, sent_texts, topic, out), cause)
                self._remember_spoken(st, spoken, participants)
        return out

    def _remember_spoken(self, st: ThreadState, lines: list, participants: set) -> None:
        """Everything I say in a space reaches that space's memory buffer, prompted or not (my own initiatives,
        late replies, things said in passing). Every send passes through here; replies that _after_exchange also
        records are the same lines (same msg ids) and the buffer keeps each line once."""
        mem = self.k.memory
        if mem is None or not lines:
            return
        tid = st.thread_id
        who = [p for p in (participants or ()) if p]
        lines = list(lines)

        async def job():
            try:
                await mem.observe_conversation(tid, lines, who)
            except Exception:
                log.warning("memory.observe_conversation (own lines) failed", exc_info=True)
        self._after(job, f"soc.spoken:{tid}")

    def _summary(self, st: ThreadState, kind: str, person_key: Optional[str], texts: list, topic: str,
                 out: dict) -> str:
        p = self.relations.peek(person_key) if person_key else None
        who = p.name() if p else (person_key.split(":", 1)[-1] if person_key else "大家")
        snip = T.snippet(" / ".join(texts), 40)
        top = T.snippet(topic or (texts[0] if texts else ""), 14)
        if kind in INITIATIVE_KINDS:
            s = prompts.OUTCOME_INITIATIVE.format(who=who, snippet=snip)
        elif st.chat_type == "group":
            title = st.title
            if not title:
                try:
                    title = self.k.world.name(st.thread_id)
                except Exception:
                    title = ""
            s = prompts.OUTCOME_GROUP.format(title=title or "群", who=who, n=len(texts), topic=top, snippet=snip)
        else:
            s = prompts.OUTCOME_PRIVATE.format(who=who, n=len(texts), topic=top, snippet=snip)
        if out.get("aborted"):
            s += prompts.OUTCOME_ABORTED.format(reason=out.get("reason", ""))
        return s

    def _record_outcome(self, summary: str, event_id: str = "") -> None:
        selfm = self.k.selfm
        if selfm is None:
            return

        async def job():
            await selfm.record_outcome(summary, event_id or "")
        self._after(job, "soc.outcome")

    # ================================================================== after an exchange
    @staticmethod
    def _quality(qr: Optional[dict], outcome: str) -> float:
        s = 0.0
        if qr:
            try:
                s = float(qr.get("sentiment") or 0.0)
            except Exception:
                s = 0.0
        q = 0.25 + 0.6 * s
        if qr:
            if qr.get("insulted"):
                q -= 0.6
            if qr.get("praised") or qr.get("thanked") or qr.get("affectionate"):
                q += 0.2
        if outcome == "react":
            q *= 0.5
        return max(-1.0, min(1.0, q))

    def _after_exchange(self, st: ThreadState, items: Sequence[Inbound], my_lines: list, qr: Optional[dict],
                        outcome: str, reaction: Optional[str] = None) -> None:
        k = self.k
        items = list(items)
        if not items and not my_lines:
            return
        consent = {}
        for i in items:
            p = self.relations.peek(i.msg.person_key)
            consent[i.msg.person_key] = p.consent if p else {}
        mem_lines = [i.line for i in items if consent.get(i.msg.person_key, {}).get("remember", True)] + list(my_lines)
        learn_lines = [i.line for i in items if consent.get(i.msg.person_key, {}).get("train_on_my_words", True)] + \
            list(my_lines)
        mem_lines.sort(key=lambda l: l.ts)
        learn_lines.sort(key=lambda l: l.ts)
        participants = list(dict.fromkeys(i.msg.person_key for i in items))
        quality = self._quality(qr, outcome)
        humans = list(dict.fromkeys(i.msg.person_key for i in items if not i.is_agent))
        targets = humans if st.chat_type == "private" else list(dict.fromkeys(
            i.msg.person_key for i in items if not i.is_agent and (i.addressed or outcome != "silent")))[:3]
        contacted = humans if st.chat_type == "private" else list(dict.fromkeys(
            i.msg.person_key for i in items if not i.is_agent and i.addressed))[:3]
        learning_paused = self._learning_paused()
        tid, ctype = st.thread_id, st.chat_type

        async def job():
            if k.memory is not None and mem_lines:
                try:
                    await k.memory.observe_conversation(tid, mem_lines, participants)
                except Exception:
                    log.warning("memory.observe_conversation failed", exc_info=True)
            if k.growth is not None and learn_lines and not learning_paused:
                try:
                    await k.growth.observe_messages(tid, ctype, learn_lines)
                except Exception:
                    log.warning("growth.observe_messages failed", exc_info=True)
            if outcome in ("respond", "react"):
                n = max(1, len(my_lines))
                spend = (0.012 + 0.008 * n) * (1.3 if ctype == "group" else 1.0) * (0.5 if outcome == "react" else 1.0)
                try:
                    if k.affect is not None:
                        k.affect.social_spend(spend)
                    if k.motivation is not None:
                        k.motivation.satisfy("social", 0.05 * max(0.1, min(1.5, 0.5 + quality)),
                                             f"chat:{tid}")
                except Exception:
                    log.warning("affect/motivation update failed", exc_info=True)
                for pk in targets:
                    try:
                        await self.record_interaction(pk, quality=quality, initiated_by="them", thread_id=tid)
                    except Exception:
                        log.warning("record_interaction failed", exc_info=True)
            elif outcome == "silent" and contacted:
                for pk in contacted:      # they reached out to me; I let it pass (for now): weaker, but contact
                    try:
                        await self.record_interaction(pk, quality=0.4 * quality, initiated_by="them", thread_id=tid)
                    except Exception:
                        log.warning("record_interaction failed", exc_info=True)
            for pk in humans:
                try:
                    await self._resolve_initiative(pk, tid, qr, items)
                except Exception:
                    log.warning("initiative resolution failed", exc_info=True)
        self._after(job, f"soc.after:{tid}")

    # ================================================================== initiatives (outreach tracking)
    async def _on_outreach_plan(self, ev) -> None:
        plan = (ev.payload or {}).get("plan") or {}
        pk = plan.get("person_key") or (ev.payload or {}).get("person_key")
        pid = plan.get("id") or (ev.payload or {}).get("plan_id")
        if not pk or not pid:
            return
        rec = {"plan_id": pid, "reason_type": plan.get("reason_type") or (ev.payload or {}).get("reason_type", ""),
               "thread_id": plan.get("thread_id") or ev.thread_id, "ts": self.k.clock.now(),
               "status": (ev.payload or {}).get("status", "")}
        self._recent_plans[pk] = rec
        pend = self._pending_init.get(pk)
        if pend and not pend.get("plan_id") and abs(pend["ts"] - rec["ts"]) < 3600:
            pend["plan_id"] = pid
            pend["reason_type"] = pend.get("reason_type") or rec["reason_type"]
            self.writer.add("UPDATE soc_initiatives SET plan_id=?, reason_type=? WHERE id=?",
                            (pid, pend["reason_type"], pend["id"]))

    def note_outreach(self, person_key: str, plan_id: str, reason_type: str = "", thread_id: str = "") -> None:
        """Extra API: tell social which OutreachPlan the next/last initiative to ``person_key`` belongs to."""
        self._recent_plans[person_key] = {"plan_id": plan_id, "reason_type": reason_type, "thread_id": thread_id,
                                          "ts": self.k.clock.now(), "status": "noted"}
        pend = self._pending_init.get(person_key)
        if pend and not pend.get("plan_id"):
            pend["plan_id"], pend["reason_type"] = plan_id, reason_type or pend.get("reason_type", "")

    def _note_initiative(self, st: ThreadState, person_key: Optional[str], plan_id: Optional[str], msg_ids: list) -> None:
        now = self.k.clock.now()
        self._init_counts.setdefault((st.channel, st.chat_id), []).append(now)
        if not person_key:
            return
        rp = self._recent_plans.get(person_key)
        reason_type = ""
        if plan_id is None and rp and now - rp["ts"] < 7200:
            plan_id, reason_type = rp["plan_id"], rp.get("reason_type", "")
        rec = {"id": new_id("ini"), "plan_id": plan_id or "", "person_key": person_key, "thread_id": st.thread_id,
               "reason_type": reason_type, "ts": now, "msg_ids": msg_ids}
        old = self._pending_init.get(person_key)
        if old is not None:      # still unanswered when I wrote again: one wait; an answer resolves both
            rec["carry"] = (list(old.get("carry") or []) + [old["id"]])[-10:]
        self._pending_init[person_key] = rec
        self.writer.add("INSERT OR REPLACE INTO soc_initiatives (id,plan_id,person_key,thread_id,reason_type,ts,status,"
                        "event_ids) VALUES (?,?,?,?,?,?,?,?)", (rec["id"], rec["plan_id"], person_key, st.thread_id,
                                                                 reason_type, now, "pending", dumps(msg_ids)))

    async def _load_initiatives(self) -> None:
        now = self.k.clock.now()
        rows = await self.k.db.fetchall("SELECT * FROM soc_initiatives WHERE ts > ?", (now - 31 * 86400,))
        for r in rows:
            route = None
            try:
                route = self.k.world.route(r["thread_id"] or "")
            except Exception:
                route = None
            ch, chat = (route[0], route[1]) if route else (r["thread_id"] or "").partition(":")[::2]
            self._init_counts.setdefault((ch, chat), []).append(r["ts"])
            if r["status"] == "pending":
                self._pending_init[r["person_key"]] = {"id": r["id"], "plan_id": r["plan_id"] or "",
                                                       "person_key": r["person_key"], "thread_id": r["thread_id"],
                                                       "reason_type": r["reason_type"] or "", "ts": r["ts"],
                                                       "msg_ids": loads(r["event_ids"], [])}

    async def _resolve_initiative(self, person_key: str, thread_id: str, qr: Optional[dict], items: Sequence) -> None:
        rec = self._pending_init.get(person_key)
        if rec is None:          # an answer in any space counts: initiatives are to a person (docs/22)
            return
        msgs = [i for i in items if i.msg.person_key == person_key and i.msg.ts >= rec["ts"]]
        if not msgs:
            return
        self._pending_init.pop(person_key, None)
        k = self.k
        latency = max(0.0, msgs[0].msg.ts - rec["ts"])
        s = float((qr or {}).get("sentiment") or 0.0)
        warm = s >= 0.2 or bool((qr or {}).get("affectionate") or (qr or {}).get("thanked"))
        if not qr:
            text = " ".join(i.msg.text or "" for i in msgs)
            warm = len(text) >= 8 or any(x in text for x in ("哈", "！", "!", "好呀", "嗯嗯", "谢谢", "❤", "😂"))
        outcome = "replied_warm" if warm else "replied_flat"
        self.writer.add("UPDATE soc_initiatives SET status='resolved', resolved_at=?, outcome=? WHERE id=?",
                        (k.clock.now(), outcome, rec["id"]))
        # they answered: the provisional "passed over" evidence of this wait is withdrawn (one wait, one outcome)
        await self._initiative_outcome(rec, outcome, latency, refs=[i.event_id for i in msgs if i.event_id][:5],
                                       note=f"主动消息后 {int(latency // 60)} 分钟得到回复（{'热情' if warm else '平淡'}）")
        self._emit_bg("soc.initiative_resolved", {"person_key": person_key, "plan_id": rec.get("plan_id"),
                                                  "outcome": outcome, "latency_s": latency}, thread_id=thread_id)

    # prior probability that an unprompted message is simply let slide (never answered), by closeness: low for the
    # closest people, higher for weak ties [inferred from the timing note §2: in-chat messages go unanswered for a
    # week 0.22 % (WhatsApp, strong ties) vs 1.54 % (Instagram, broader ties); a cold initiative slides more, ~x10].
    @staticmethod
    def _slide_prior(closeness: float) -> float:
        return 0.03 + 0.12 * (1.0 - min(1.0, max(0.0, closeness)))

    def reply_survival(self, person_key: str, since: float, now: Optional[float] = None, mode: str = "cold") -> float:
        """Sync. P(no answer yet from ``person_key`` to a message I sent at ``since`` | they answer as they usually
        do): their own latency distribution (PersonBaseline) shrunk toward the prior for their closeness, on a clock
        that runs slowly while they are usually offline (alive/social/latency.py)."""
        from alive.social import latency as L
        now = self.k.clock.now() if now is None else now
        r = self.relations.peek_rel(person_key)
        c = self.relations.closeness_at(r, now) if r is not None else 0.2
        lats = list(self._baseline(person_key).latencies)
        active = None
        if r is not None and sum(r.hours) >= 5:
            active = lambda ts: self.relations.active_prob(r, self.k.clock.hour_of_day(ts))  # noqa: E731
        t_eff = L.effective_elapsed(since, now, active)
        return L.reply_survival(t_eff, c, lats, mode)

    def ignore_evidence(self, person_key: str, since: float, now: Optional[float] = None) -> float:
        """Sync. Posterior that my unprompted message (sent at ``since``) is being passed over, given no answer so
        far: q / (q + (1 - q) S(t)). Starts at the prior q and grows smoothly as their own survival curve falls."""
        r = self.relations.peek_rel(person_key)
        c = self.relations.closeness_at(r, self.k.clock.now()) if r is not None else 0.2
        q = self._slide_prior(c)
        s = self.reply_survival(person_key, since, now)
        return q / (q + (1.0 - q) * s)

    async def _initiative_outcome(self, rec: dict, outcome: str, latency: Optional[float], *, refs: Sequence = (),
                                  note: str = "") -> None:
        """One outcome per initiative, recorded once (S5): to motivation (reception + needs + its plan, and the
        growth evidence for plan-backed initiatives), or - with no plan to report - as growth evidence here."""
        from alive.social.latency import EVIDENCE_STAKES
        k = self.k
        pk = rec["person_key"]
        mot = k.motivation
        settle = getattr(mot, "note_ignore_evidence", None) if mot is not None else None
        if settle is not None:
            for key in list(rec.get("carry") or []):   # earlier waits for the same person: settled by this one
                settle(pk, key, 1.0 if outcome == "ignored" else 0.0, final=True)
        reported = bool(self.report_outreach and rec.get("plan_id") and mot is not None)
        try:
            if reported:
                await mot.record_outreach_result(rec["plan_id"], outcome, latency, key=rec["id"])
            elif mot is not None and hasattr(mot, "record_initiative_outcome"):
                mot.record_initiative_outcome(pk, outcome, key=rec["id"])
        except Exception:
            log.warning("recording the initiative outcome failed", exc_info=True)
        if reported or k.growth is None or self._learning_paused():
            return
        rt = rec.get("reason_type") or "general"
        pol = {"replied_warm": "helpful", "replied_flat": "neutral", "ignored": "harmful"}[outcome]
        conf = {"replied_warm": 0.6, "replied_flat": 0.4, "ignored": 0.6}[outcome]
        if outcome == "ignored":                     # low-stakes kinds (share, greet) say little when let slide
            conf *= EVIDENCE_STAKES.get(rt, 0.6)
        try:
            await k.growth.record_evidence(level="S3", polarity=pol, about=f"outreach.{rt}", context_refs=list(refs),
                                           subject=pk, confidence=round(conf, 3), note=note)
        except Exception:
            log.warning("record_evidence failed", exc_info=True)

    async def _check_ignored_initiatives(self) -> None:
        """Unanswered initiatives: the evidence that each is being passed over (``ignore_evidence``) feeds the
        reception posterior as it grows - no fixed "ignored after a day" for everyone. A wait is closed as ignored
        only when that evidence has numerically converged (their survival curve has nothing left to say) or at the
        30-day bookkeeping horizon; an answer at any time before that resolves it as a reply."""
        k = self.k
        now = k.clock.now()
        note_fn = getattr(k.motivation, "note_ignore_evidence", None) if k.motivation is not None else None
        for pk, rec in list(self._pending_init.items()):
            try:
                e = self.ignore_evidence(pk, rec["ts"], now)
            except Exception:
                log.debug("ignore evidence failed", exc_info=True)
                continue
            rec["e"] = e
            if note_fn is not None:
                note_fn(pk, rec["id"], e)
            converged = (1.0 - e) < 1e-3                      # numerical convergence, not a deadline
            if not converged and now - rec["ts"] < 30 * 86400:  # bookkeeping horizon (initiatives reload 31 d)
                continue
            self._pending_init.pop(pk, None)
            self.writer.add("UPDATE soc_initiatives SET status='resolved', resolved_at=?, outcome='ignored' WHERE id=?",
                            (now, rec["id"]))
            await self._initiative_outcome(rec, "ignored", None, note=f"主动消息一直没有得到回复（把握 {e:.0%}）")
            self._emit_bg("soc.initiative_resolved", {"person_key": pk, "plan_id": rec.get("plan_id"),
                                                      "outcome": "ignored", "evidence": round(e, 4)},
                          thread_id=rec.get("thread_id"))

    async def _retention(self) -> None:
        cutoff = self.k.clock.now() - self.retention_days * 86400
        await self.writer.flush()
        n = await self.k.db.execute("DELETE FROM soc_messages WHERE ts < ?", (cutoff,))
        await self.k.db.execute("DELETE FROM soc_pairing WHERE status!='pending' AND created < ?",
                                (self.k.clock.now() - 30 * 86400,))
        if n:
            log.info("social retention: deleted %d old messages", n)

    # ================================================================== interface: queries
    def threads(self) -> list:
        return [st.info() for st in sorted(self._threads.values(), key=lambda s: -max(s.last_in, s.last_out))]

    async def history(self, thread_id: str, limit: int = 30) -> list:
        st = self._threads.get(thread_id)
        if st is not None:
            await self._ensure_lines(st)
            lines = list(st.lines or [])
            if limit <= len(lines) or len(lines) < st.lines_max:
                return lines[-limit:] if limit > 0 else []
        await self.writer.flush()
        rows = await self.k.db.fetchall("SELECT * FROM soc_messages WHERE thread_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                                        (thread_id, int(limit)))
        return [ChatLine(sender=r["sender_name"] or "", person_key=r["sender_key"] or "", text=r["text"] or "",
                         ts=r["ts"], mine=bool(r["mine"]), msg_id=r["msg_id"] or "") for r in reversed(rows)]

    def _native_id(self, person_key: str, channel: str) -> str:
        """This person's native user id on `channel` (world handles), for @-mentions."""
        w = getattr(self.k, "world", None)
        if w is not None:
            try:
                for h in w.handles_of(person_key):
                    ch, _, rest = h.partition(":")
                    if ch == channel and rest and rest[0] not in "#@":
                        return rest
            except Exception:
                pass
        ch, _, rest = person_key.partition(":")
        return rest if ch == channel else ""

    async def rekey_entity(self, old: str, new: str) -> None:
        """Entity merge (docs/22 §3.4): after the SQL pass moved the rows, switch the live caches over. Caches that
        mirror the DB are dropped and reload from the merged rows; live-only state is moved/merged."""
        from alive.world.rekey import rekey_dict, rekey_obj
        await self.writer.flush()
        # spaces: the old one-to-one space IS the old person; reload the surviving space from the merged rows
        st_old = self._threads.pop(old, None)
        self._dirty_threads.discard(old)
        st_new = self._threads.get(new)
        if st_new is not None:
            st_new.lines = None                     # reload merged lines lazily
            st_new.participants = type(st_new.participants)(
                [new if p == old else p for p in st_new.participants], maxlen=st_new.participants.maxlen)
            if st_old is not None:
                st_new.last_in = max(st_new.last_in, st_old.last_in)
                st_new.last_out = max(st_new.last_out, st_old.last_out)
                st_new.last_activity = max(st_new.last_activity, st_old.last_activity)
                st_new.muted_until = max(st_new.muted_until, st_old.muted_until)
        for st in self._threads.values():           # the person inside shared spaces
            if any(p == old for p in st.participants):
                st.participants = type(st.participants)([new if p == old else p for p in st.participants],
                                                        maxlen=st.participants.maxlen)
            st.my_last_targets = [new if p == old else p for p in st.my_last_targets]
            if st.lines:
                for line in st.lines:
                    if line.person_key == old:
                        line.person_key = new
            for lv in st.recent:
                if lv.speaker == old:
                    lv.speaker = new
        a = self._actors.pop(old, None)
        if a is not None and a.task is not None and not a.task.done() and not a.busy:
            a.task.cancel()
        rekey_dict(self._talk_override, old, new)
        rekey_dict(self._recent_plans, old, new, lambda o, n: n if (n or {}).get("ts", 0) >= (o or {}).get("ts", 0) else o)
        for d in (self._recent_plans, self._pending_init):
            for k_, v in list(d.items()):
                d[k_] = rekey_obj(v, old, new)
        rekey_dict(self._pending_init, old, new, lambda o, n: n if n.get("ts", 0) >= o.get("ts", 0) else o)
        for key in (old, new):
            self._baselines.pop(key, None)
        rel = getattr(self.relations, "rekey_entity", None)
        if rel is not None:
            await rel(old, new)

    async def entity_context(self, entity: str, now: float) -> dict:
        """State of an entity for the initiative process (alive/core/initiative.py). A person: closeness,
        reciprocity, how likely they are around at this hour (their rhythm). A shared space: how lively it is and
        how much of its floor I hold."""
        out: dict = {}
        st = self._threads.get(entity)
        if st is not None and st.chat_type == "group":
            recent = [lv for lv in list(st.recent) if now - lv.ts <= 1800]
            mine = sum(1 for lv in recent if lv.mine)
            speakers = {lv.speaker for lv in recent if not lv.mine}
            out.update({"msgs_30min": len(recent), "my_share": (mine / len(recent)) if recent else 0.0,
                        "fair_share": 1.0 / (len(speakers) + 1)})
            closes = []
            for pk in list(speakers)[:8]:
                r = self.relations.peek_rel(pk)
                if r is not None:
                    closes.append(self.relations.closeness_at(r, now))
            if closes:
                out["closeness"] = sum(closes) / len(closes)
            return out
        r = self.relations.peek_rel(entity)
        if r is None:
            try:
                _p, r = await self.relations.get(entity, create=False)
            except Exception:
                r = None
        if r is not None:
            out.update({"closeness": self.relations.closeness_at(r, now), "reciprocity": float(r.reciprocity),
                        "active_now_prob": self.relations.active_prob(r, self.k.clock.hour_of_day(now))})
        return out

    def recent_elsewhere(self, person_key: str, exclude_space: str, since: float, limit: int = 8, *,
                         allow=None, withheld: Optional[list] = None) -> list:
        """(where, ChatLine) from spaces other than `exclude_space` since `since`: lines this person wrote, and
        mine in spaces where they were present then. In-memory only (recently active spaces). ``allow(space)``:
        only spaces whose audience covers where I am now (INV-34); what this person said in the others goes to
        ``withheld``."""
        out = []
        for st in list(self._threads.values()):
            if st.thread_id == exclude_space or not st.lines:
                continue
            lines = [l for l in st.lines if l.ts >= since]
            if not lines:
                continue
            one_to_one = st.chat_type == "private" and st.thread_id == person_key
            present = one_to_one or any(l.person_key == person_key for l in lines)
            if not present:
                continue
            if allow is not None and not allow(st.thread_id):
                if withheld is not None:
                    withheld.extend(l.text for l in lines if l.person_key == person_key and l.text)
                continue
            where = "私聊" if st.chat_type == "private" else f"群「{st.title or ''}」"
            out += [(where, l) for l in lines if l.person_key == person_key or (l.mine and present)]
        out.sort(key=lambda x: x[1].ts)
        return out[-limit:]

    async def person_lines(self, person_key: str, *, limit: int = 300, since: Optional[float] = None) -> dict:
        """{space: [ChatLine]} oldest first: this person's lines in ANY space (the newest ``limit``), and my lines
        in those spaces over the same period (reply latency, who started what). A person's baseline and the
        topics we have open belong to the person, not to one space."""
        if not person_key:
            return {}
        await self.writer.flush()
        lo = float(since) if since is not None else 0.0

        def q(c):
            theirs = c.execute("SELECT * FROM soc_messages WHERE sender_key=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                               (person_key, lo, int(limit))).fetchall()
            if not theirs:
                return [], []
            spaces = sorted({r["thread_id"] for r in theirs})
            t0 = min(r["ts"] for r in theirs)
            mine = c.execute(f"SELECT * FROM soc_messages WHERE mine=1 AND ts>=? AND thread_id IN "
                             f"({','.join('?' * len(spaces))}) ORDER BY ts DESC LIMIT ?",
                             [t0] + spaces + [int(limit) * 2]).fetchall()
            return theirs, mine

        theirs, mine = await self.k.db.read(q)
        out: dict = {}
        for r in sorted(list(theirs) + list(mine), key=lambda r: (r["ts"], r["id"])):
            out.setdefault(r["thread_id"], []).append(
                ChatLine(sender=r["sender_name"] or "", person_key=r["sender_key"] or "", text=r["text"] or "",
                         ts=r["ts"], mine=bool(r["mine"]), msg_id=r["msg_id"] or ""))
        return out

    async def person_name(self, person_key: str) -> str:
        if not person_key:
            return self._names[0] if self._names else "我"
        p = self.relations.peek(person_key)
        if p is None:
            p, _ = await self.relations.get(person_key, create=False)
        return p.name() if p is not None else person_key.split(":", 1)[-1]

    async def record_interaction(self, person_key: str, *, quality: float, initiated_by: str,
                                 thread_id: Optional[str] = None, note: str = "") -> None:
        await self.relations.record_interaction(person_key, quality=quality, initiated_by=initiated_by,
                                                thread_id=thread_id, note=note)

    @staticmethod
    def _ago(seconds: float) -> str:
        s = max(0.0, seconds)
        if s < 120:
            return "刚刚"
        if s < 3600:
            return f"{int(s // 60)} 分钟前"
        if s < 86400:
            return f"{int(s // 3600)} 小时前"
        if s < 30 * 86400:
            return f"{int(s // 86400)} 天前"
        return f"{int(s // (30 * 86400))} 个月前"

    async def relationship_context(self, person_key: str, *, space: Optional[str] = None,
                                   present: Optional[Sequence[str]] = None, withheld: Optional[list] = None,
                                   origins: Optional[list] = None) -> str:
        """Interface block about a person. With ``space`` (where I'm about to speak) / ``present``, the remembered
        facts are only those everyone there may hear (INV-34); the rest go to ``withheld``; ``origins`` collects
        where the facts shown came from."""
        k = self.k
        p, r = await self.relations.get(person_key, create=False)
        if p is None:
            return prompts.REL_UNKNOWN.format(name=person_key.split(":", 1)[-1])
        now = k.clock.now()
        name = p.name()
        address = ""
        pref = p.names.get("preferred") or p.names.get("nick")
        if pref and pref != p.names.get("display"):
            address = f"（我叫 TA「{pref}」）"
        kind = prompts.REL_OWNER if p.meta.get("owner") or self.relations.is_owner(person_key) else (
            prompts.REL_AGENT if p.kind == "agent" else (prompts.REL_STRANGER if not self.relations.is_paired(person_key)
                                                         and int(p.meta.get("last_trust", 2)) <= 0 else ""))
        lines = [prompts.REL_HEADER.format(name=name, address=address, kind=kind)]
        c, w = self.relations.closeness_at(r, now), self.relations.warmth_at(r, now)
        trust = "；对 TA 还有些保留" if r.trust < 0.3 else ("；很信任 TA" if r.trust >= 0.8 else "")
        lines.append(prompts.REL_LINE_CLOSENESS.format(closeness_word=closeness_word(c), warmth_word=warmth_word(w),
                                                       trust=trust))
        if r.last_contact:
            ini = {"me": "，上次是我先找的 TA", "them": "，上次是 TA 先找的我"}.get(r.last_initiated_by, "")
            lines.append(prompts.REL_LINE_CONTACT.format(last=self._ago(now - r.last_contact), initiated=ini))
        facts = []
        if k.memory is not None:
            try:
                scoped = ({"thread_id": space, "present": list(present or ()), "withheld": withheld}
                          if (space or present) else {})
                for rec in (await k.memory.about_person(person_key, limit=8, **scoped))[:8]:
                    txt = rec.get("content") if isinstance(rec, dict) else getattr(rec, "content", "")
                    if txt:
                        facts.append(T.snippet(str(txt), 60))
                        if origins is not None and len(facts) <= 4:
                            from alive.world.audience import origins_of
                            origins.extend(origins_of(rec))
            except Exception:
                log.debug("about_person failed", exc_info=True)
        if facts:
            lines.append(prompts.REL_LINE_FACTS)
            lines.extend(f"  · {f}" for f in facts[:4])
        loops = list(r.open_loops[-3:])
        if k.motivation is not None:
            try:
                scope = None
                if space is not None:
                    from alive.world.audience import Scope
                    scope = Scope(getattr(k, "world", None), space, list(present or ()))
                for it in await k.motivation.active_intentions(limit=50):
                    if getattr(it, "target_person", None) == person_key and getattr(it, "goal", ""):
                        its_origins = list((getattr(it, "meta", None) or {}).get("origins") or [])
                        if scope is not None and not scope.covers(its_origins):
                            if withheld is not None:              # kept out, but the leak check knows it
                                withheld.append(T.snippet(it.goal, 60))
                            continue
                        loops.append(T.snippet(it.goal, 40))
            except Exception:
                log.debug("active_intentions failed", exc_info=True)
        pend = self._pending_init.get(person_key)
        if pend:
            loops.append(f"我{self._ago(now - pend['ts'])}主动找过 TA，还没回")
        if loops:
            lines.append(prompts.REL_LINE_LOOPS.format(loops="；".join(dict.fromkeys(loops[:4]))))
        mood = (p.mind_model or {}).get("recent_mood_guess")
        if mood:
            lines.append(prompts.REL_LINE_MOOD.format(mood=mood))
        sens = list((p.mind_model or {}).get("sensitivities") or [])[:3]
        if p.private_from_others:
            sens.append("有些关于 TA 的事是别人告诉我的，TA 不知道我知道，别说漏")
        if sens:
            lines.append(prompts.REL_LINE_SENSITIVE.format(items="；".join(sens)))
        try:
            asp = [a for a in await self.relations.aspects(person_key) if a["stable"] and a["n"] >= 3][:3]
        except Exception:
            asp = []
        if asp:
            def word(a):
                if a["summary"]:
                    return f"{a['aspect']}：{a['summary']}"
                tone = "正面" if a["pos"] >= max(a["neg"], a["neu"]) else ("负面" if a["neg"] >= a["neu"] else "中性")
                return f"{a['aspect']}方面偏{tone}"
            lines.append(prompts.REL_LINE_ATTITUDE.format(items="；".join(word(a) for a in asp)))
        try:
            ops = [o for o in await self.relations.opinions(person_key) if o["confidence"] >= 0.6][:2]
        except Exception:
            ops = []
        if ops:
            lines.append(prompts.REL_LINE_OPINION.format(items="；".join(T.snippet(o["text"], 40) for o in ops)))
        return "\n".join(lines)[: int(self.cfg.get("relationship_context_chars", 700))]

    async def _contact_gap_estimates(self, now: float) -> dict:
        """person_key -> typical time between days we were in touch (s), from the last 60 days of their messages
        (one aggregate query, cached for an hour - technical). People never seen in that window are absent."""
        if now - self._contact_gaps_at < 3600.0:
            return self._contact_gaps
        window = 60 * 86400.0
        try:
            off = self.k.clock.local(now).utcoffset().total_seconds()
        except Exception:
            off = 0.0
        out: dict = {}
        try:
            rows = await self.k.db.fetchall(
                "SELECT sender_key AS pk, COUNT(DISTINCT CAST((ts + ?) / 86400 AS INTEGER)) AS days, MIN(ts) AS first "
                "FROM soc_messages WHERE mine=0 AND ts>? AND sender_key IS NOT NULL GROUP BY sender_key",
                (off, now - window))
            for r in rows:
                days = int(r["days"] or 0)
                if days > 0:
                    span = max(86400.0, now - float(r["first"] or now))
                    out[r["pk"]] = span / days
        except Exception:
            log.debug("contact gap query failed", exc_info=True)
        self._contact_gaps, self._contact_gaps_at = out, now
        return out

    async def outreach_candidates(self, now: float, limit: int = 10) -> list:
        """People I could reach out to (consent, a private route). When there are more than ``limit``, WHO comes to
        mind is a Plackett-Luce sample weighted by closeness, warmth and whether they're around (Gumbel-top-k, a
        temperature from today's focus) - not a deterministic top-k; the list is then shown closest first."""
        hour = self.k.clock.hour_of_day(now)
        gaps = await self._contact_gap_estimates(now)
        out = []
        for p, r in await self.relations.all_persons():
            if p.kind != "human" or p.blocked or not p.consent.get("proactive_ok"):
                continue
            tid = p.meta.get("dm_thread")
            w = getattr(self.k, "world", None)
            if w is not None and w.entity(p.person_key) is not None:
                route = w.route(p.person_key)
                if route is None or route[2] != "private" or route[0] not in self.adapters:
                    continue                    # never reached in private: can't start a 1:1 exchange
                tid = p.person_key               # the person entity is the space for direct contact
            elif not tid or tid.partition(":")[0] not in self.adapters:
                continue
            c = self.relations.closeness_at(r, now)
            w = self.relations.warmth_at(r, now)
            act = self.relations.active_prob(r, hour)
            recent = sum(1 for t in r.initiatives if now - t < 7 * 86400)
            out.append({"person_key": p.person_key, "thread_id": tid, "name": p.name(), "closeness": round(c, 4),
                        "warmth": round(w, 4), "last_contact": r.last_contact, "last_initiated_by": r.last_initiated_by,
                        "reciprocity": round(r.reciprocity, 4), "active_now_prob": act, "recent_initiatives": recent,
                        "proactive_ok": bool(p.consent.get("proactive_ok")), "reply_latency": r.reply_latency,
                        "contact_gap_s": gaps.get(p.person_key),
                        "pending_initiative": p.person_key in self._pending_init})

        def weight(d: dict) -> float:
            return 0.6 * d["closeness"] + 0.2 * max(0.0, d["warmth"]) + 0.2 * d["active_now_prob"]
        n = max(0, int(limit))
        if len(out) > n:
            tex = getattr(self.k, "texture", None)
            # T ~0.08 on a 0..1 weight: someone 0.2 closer is ~12x likelier to come to mind; a scattered day
            # (low focus) spreads it out
            temp = 0.08 / (tex.multiplier("focus", 0.6) if tex is not None else 1.0)
            keep = set(C.gumbel_topk(C.rng_of(self.k), {i: weight(d) for i, d in enumerate(out)}, n, temp))
            out = [d for i, d in enumerate(out) if i in keep]
        out.sort(key=lambda d: -weight(d))
        return out

    async def read_url(self, url: str, purpose: str = "curiosity") -> dict:
        return await self.web.read_url(url, purpose)

    async def poll_feeds(self) -> list:
        return await self.web.poll_feeds()

    # ================================================================== extra public API
    def reading_cues(self, thread_id: str) -> list:
        """Sync. Level-0 understanding cues (docs/17 §3.1) computed for the thread's latest replied-to batch."""
        st = self._threads.get(thread_id)
        return list(st.last_cues) if st is not None else []

    def person_baseline(self, person_key: str) -> dict:
        """Sync. PersonBaseline summary (docs/17 §4) for a person (cached people only)."""
        return self._baseline(person_key).summary()

    def group_context(self, thread_id: str) -> dict:
        """Sync. GroupContext (docs/17 §8): active members, frame, my share vs fair share, addressee."""
        st = self._threads.get(thread_id)
        if st is None or st.chat_type != "group":
            return {}
        return group_context(list(st.lines or []), self.k.clock.now(),
                             names={ln.person_key: ln.sender for ln in list(st.lines or []) if ln.person_key})

    def adapter(self, name: str):
        """The channel adapter instance named ``name`` (e.g. the sim adapter in tests)."""
        return self.adapters.get(name)

    def channel_quotas(self, channel: str) -> dict:
        ad = self.adapters.get(channel)
        return asdict(ad.quotas) if ad is not None else {}

    def channel_capabilities(self, channel: str) -> dict:
        ad = self.adapters.get(channel)
        return asdict(ad.caps) if ad is not None else {}

    def is_paired(self, person_key: str) -> bool:
        return self.relations.is_paired(person_key)

    def consent_of(self, person_key: str) -> dict:
        """Sync. Consent flags {remember, train_on_my_words, proactive_ok} (defaults if unknown/not cached)."""
        p = self.relations.peek(person_key)
        return dict(p.consent) if p is not None else {"remember": True, "train_on_my_words": True, "proactive_ok": False}

    def list_pairings(self, channel: Optional[str] = None) -> list:
        """Pending pairing requests [{code, channel, person_key, sender_name, created, expires}]."""
        return self.relations.list_pairings(channel)

    async def approve_pairing(self, code: str, approved_by: str = "owner") -> Optional[dict]:
        """Owner approves a stranger's pairing code (CLI ``/approve CODE`` or HTTP). Returns the request or None."""
        d = await self.relations.decide_pairing(code, True, approved_by)
        if d is not None:
            self._emit_bg("soc.paired", {"person_key": d["person_key"], "channel": d["channel"],
                                         "approved_by": approved_by, "name": d.get("sender_name")})
        return d

    async def reject_pairing(self, code: str, rejected_by: str = "owner") -> Optional[dict]:
        return await self.relations.decide_pairing(code, False, rejected_by)

    async def revoke_pairing(self, person_key: str) -> bool:
        return await self.relations.revoke_pairing(person_key)

    async def allow_group(self, channel: str, chat_id: str, allowed: bool = True) -> None:
        """Owner admits (or removes) a group under group_policy=allowlist. Persisted."""
        key = f"{channel}:{chat_id}"
        (self._allowed_groups.add if allowed else self._allowed_groups.discard)(key)
        await self.k.db.kv_set("social", "allowed_groups", sorted(self._allowed_groups))

    def set_talk_value(self, thread_id: str, value: Optional[float]) -> None:
        """Override a thread's base talk_value (None clears). Time-of-day rules still apply."""
        if value is None:
            self._talk_override.pop(thread_id, None)
        else:
            self._talk_override[thread_id] = float(value)

    def mute_thread(self, thread_id: str, seconds: float) -> None:
        """Cooldown: stay silent in a thread for ``seconds`` (virtual)."""
        st = self._thread_by_id(thread_id)
        st.muted_until = self.k.clock.now() + float(seconds)
        self._save_thread(st)

    async def update_person(self, person_key: str, **kw):
        """preferred/nick/kind/consent/blocked/mind_model/common_ground/private_from_others (see relations)."""
        return await self.relations.update_person(person_key, **kw)

    async def observe_aspect(self, person_key: str, aspect: str, probs, strength: float = 1.0, summary: str = "") -> dict:
        return await self.relations.observe_aspect(person_key, aspect, probs, strength, summary)

    async def add_opinion(self, person_key: str, text: str, confidence: float = 0.5, evidence: Sequence[str] = ()) -> str:
        return await self.relations.add_opinion(person_key, text, confidence, evidence)

    async def update_opinion(self, opinion_id: str, relation: str, evidence_id: str = "") -> Optional[dict]:
        return await self.relations.update_opinion(opinion_id, relation, evidence_id)

    async def adjust_trust(self, person_key: str, delta: float, reason: str = "") -> float:
        return await self.relations.adjust_trust(person_key, delta, reason)

    async def relationship(self, person_key: str) -> Optional[dict]:
        """Raw relationship snapshot (inspector/tests)."""
        p, r = await self.relations.get(person_key, create=False)
        if p is None:
            return None
        now = self.k.clock.now()
        return {"person": asdict(p), "closeness": self.relations.closeness_at(r, now),
                "warmth": self.relations.warmth_at(r, now), "trust": r.trust, "reciprocity": r.reciprocity,
                "hours": list(r.hours), "reply_latency": r.reply_latency, "last_contact": r.last_contact,
                "last_initiated_by": r.last_initiated_by,
                "interactions": r.interactions, "milestones": list(r.milestones), "initiatives": list(r.initiatives)}
