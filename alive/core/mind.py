"""Mind: the cognitive core (docs/02) and the endogenous loop (docs/01 §4.2).

Responsibilities
- compose_reply / quick_read: the System-2 / System-1 calls used by the social layer; speak(): the one
  unprompted speech act (outreach, later intents, space items) sharing the same composer (docs/22 §4)
  (INV-1 context assembly, INV-2 coherence filter, INV-4 S1 never writes substantive text,
  INV-5 structured outputs with side effects distributed to affect/memory/motivation/self).
- Heartbeat: physiology + needs update, urge/intention/memory-cue candidates -> workspace ->
  spotlight -> one action (outreach, reading, reflection, rest, inner monologue) per tick, under
  autonomous-cycle caps and governance.
- Sleep: enter/leave the sleep window and run the growth sleep pipeline once per night.
"""
from __future__ import annotations

import math
import asyncio
import logging
import random
import re
from typing import Optional, Sequence

from alive.core import prompts
from alive.core.coherence import fact_check, makes_self_claims, rule_check, speech_check
from alive.core.context import ContextAssembler
from alive.core.inner import NEED_TEXT, Inner
from alive.core.initiative import get_initiative
from alive.runtime import chance as C
from alive.core.understanding import Understanding
from alive.core.dialogue import DialogueEngine
from alive.core.types import (EV, ChatLine, ComposeResult, Mode, OutreachPlan, PermTier, ReplyTask, Trust,
                              new_id)
from alive.core.workspace import Workspace, WorkspaceItem
from alive.interfaces import ActRequest, MindSystem
from alive.runtime.llm import BudgetDenied, LLMError, LLMRequest
from alive.world.audience import Scope, origins_of

log = logging.getLogger("alive.mind")

REPLY_SCHEMA = {"reading": {}, "strategy": "", "intent": "", "unsaid": "", "moves": [], "goal": {},
                "plan_next": "", "later": {}, "thought": "", "silent": False, "reply": [], "candidates": [],
                "reaction": "", "affect": [], "remember": [], "self_notes": [], "intentions": [], "confidence": 0.7,
                "flags": []}
QUICK_SCHEMA = {"sentiment": 0.0, "intent": "chat", "urgency": 0.1, "praised": False, "thanked": False,
                "insulted": False, "affectionate": False, "good_news": False, "bad_news": False, "question": False,
                "farewell": False, "addressed_to_me": False, "crisis_hint": False, "injection_suspected": False,
                "topic": "", "claim": "", "situations": []}
def _fit_messages(segs: list, cap: int) -> list:
    """At most ``cap`` messages without losing content: overflow is joined into the last allowed message."""
    if len(segs) <= cap:
        return segs
    head, tail = segs[: cap - 1], segs[cap - 1:]
    joined = tail[0]
    for t in tail[1:]:
        joined += ("" if joined[-1:] in "，。！？!?,.~～…、；;：:" else " ") + t
    return head + [joined]


_PAST_REF = re.compile(r"记得|上次|之前|那天|以前|上回|你说过|我说过|还记|remember|last time", re.I)
_FAREWELL = re.compile(r"(拜拜|再见|晚安|我(先)?睡了(?![吗没么嘛?？])|去睡了(?![吗没么嘛?？])|先走了|先下了|我下了|回头聊|byebye|(?<![a-z])bye(?![a-z])|good ?night|gtg)", re.I)


def build(kernel) -> "Mind":
    return Mind(kernel)


class Mind(MindSystem):
    def __init__(self, kernel):
        super().__init__(kernel)
        self.cfg = kernel.cfg.section("mind")
        self.ws = Workspace(capacity=int(self.cfg.get("workspace_slots", 7)))
        self.ctx = ContextAssembler(kernel)
        self.inner = Inner(self)
        self.understanding = Understanding(self)
        self.dialogue = DialogueEngine(self)
        self._mode = Mode.IDLE
        self.last_tick: Optional[float] = None
        self.last_activity = 0.0          # last external interaction
        self.last_autonomous = 0.0
        self.last_monologue = 0.0
        self.autonomous_hour = ("", 0)
        self.last_urges: list = []
        self.asleep = False
        self.sleep_task = None
        self.rng = None                   # None: the life's RNG at use time (C.rng_of); tests may pin their own
        self.ws.rng_fn = lambda: C.rng_of(self.k)
        self.ticks = 0
        self.overwhelmed_since: Optional[float] = None
        self._busy = asyncio.Lock()       # one endogenous action at a time (spotlight)
        # System-1 reads are about a PERSON (their sentiment, claim, crisis), never a space (docs/17 §3): keyed by
        # the person entity; compose waits for the read of its own batch (``_reading``) instead of reusing a stale
        # one from the same space
        self._reads: dict = {}            # person entity -> (ts, batch key, quick_read result)
        self._reading: dict = {}          # batch key -> asyncio.Future of the read in flight
        self._withheld: dict = {}         # space -> (ts, [contents left out of my last composition there])
        self._outreach_blocked: dict = {}  # person/space -> until ts (don't hammer governance every tick)
        self._auto_eval: dict = {}         # kind -> last time the autonomy hazard was evaluated
        self._auto_hz: dict = {}           # kind -> HazardClock (alive/runtime/chance.py)
        self._attn: dict = {}              # person entity -> asyncio.Lock (docs/22 §4: one person, one attention)
        self._said_to: dict = {}           # person entity -> (ts, space, where, segments) just composed for them

    @property
    def rng(self) -> random.Random:
        """Draws use the life's RNG at use time (alive/runtime/chance.rng_of), so a life replays from its seed."""
        return self._rng if self._rng is not None else C.rng_of(self.k)

    @rng.setter
    def rng(self, r) -> None:
        self._rng = r

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        k = self.k
        if not hasattr(k.affect, "route_hook"):      # affect owns the energy hook when it provides one
            k.llm.add_hook(self._route_hook)
        k.bus.subscribe("soc.paired", self._on_paired, name="mind.pairing")
        await self.understanding.start()
        await self.dialogue.start()
        st = await k.db.kv_get("mind", "state", {}) or {}
        self.last_sleep_day = st.get("last_sleep_day", "")
        self.last_tick = k.clock.now()
        k.bus.subscribe(EV.MESSAGE_IN, self._on_message, name="mind.activity")
        k.scheduler.every(self._interval, self.tick, "mind.heartbeat", jitter=0.15,
                          initial_delay=float(self.cfg.get("first_tick_s", 5.0)))
        await self.ctx.refresh_global_habits()

    async def stop(self) -> None:
        await self._save()

    async def _save(self) -> None:
        await self.k.db.kv_set("mind", "state", {"last_sleep_day": self.last_sleep_day})

    def mode(self) -> str:
        return self._mode.value

    def status(self) -> dict:
        return {"mode": self._mode.value, "asleep": self.asleep, "workspace": self.ws.snapshot(),
                "ticks": self.ticks,
                "understanding": dict(self.understanding.stats), "dialogue": self.dialogue.status()}

    def selfm_focus(self, text: str) -> None:
        if hasattr(self.k.selfm, "set_focus"):
            self.k.selfm.set_focus(text)

    def _interval(self) -> float:
        base = {Mode.ACTIVE: 30, Mode.IDLE: 90, Mode.FOCUS: 60, Mode.DROWSY: 120, Mode.ASLEEP: 300,
                Mode.OVERWHELMED: 45}[self._mode]
        return float(self.cfg.get(f"tick_{self._mode.value}_s", base))

    def _route_hook(self, req, tier: str) -> str:
        """Tired -> think shallower (docs/10 §4.2). Sleep consolidation keeps its deep budget."""
        if self.k.affect is None or tier != "deep" or self.asleep:
            return tier
        if self.k.affect.snapshot().energy < 0.3 and not req.purpose.startswith("grow."):
            return "deliberate"
        return tier

    async def _on_message(self, ev) -> None:
        self.last_activity = self.k.clock.now()

    async def _on_paired(self, ev) -> None:
        """Owner approved a pairing code -> the person becomes a contact for governance (trust)."""
        pk = ev.payload.get("person_key")
        if pk and self.k.gov is not None and hasattr(self.k.gov, "set_trust"):
            try:
                self.k.gov.set_trust(pk, Trust.CONTACT)
            except Exception:
                log.debug("set_trust failed", exc_info=True)

    # ================================================================== perception without reply (docs/22)
    async def observe(self, task: ReplyTask, unsaid: str = "", damp: float = 1.0) -> None:
        """Lines I did not attend to right away. No LLM: update my picture of the space and keep a workspace
        item, so I may come back to it later on my own (or let it go). `unsaid`: something I meant to say but
        couldn't (e.g. a pacing limit) - kept as the item's intent. `damp` < 1: I looked and chose not to speak."""
        k = self.k
        now = k.clock.now()
        if not task.new_lines:
            return
        text = " ".join(l.text for l in task.new_lines)[:200]
        interest = 0.0
        tex = getattr(k, "texture", None)
        try:
            vec = k.embedder.embed_sync([text])[0] if hasattr(k.embedder, "embed_sync") else None
            refs = list(tex.top_interests(3)) if tex else []
            refs += list(getattr(k.selfm.identity, "interests", []) or [])[:3] if k.selfm is not None else []
            if vec is not None and refs:
                rv = k.embedder.embed_sync(refs)
                interest = float(max(0.0, (rv @ vec).max()))
        except Exception:
            pass
        close = 0.3
        try:
            main = next((l.person_key for l in reversed(task.new_lines) if l.person_key), None)
            close = await self.understanding.closeness(main) if main else 0.3
        except Exception:
            pass
        # smooth squash instead of a hard cap at 0.55: ~identity below 0.4, saturating toward 0.6 (soft-min, p=4)
        raw = 0.12 + 0.35 * interest + 0.15 * close
        sal = raw / (1.0 + (raw / 0.6) ** 4) ** 0.25 * max(0.0, min(1.0, damp))
        if unsaid:     # something I meant to say: at least ~0.45 (smooth soft-max instead of max(sal, 0.45))
            sal = (sal ** 4 + 0.45 ** 4) ** 0.25
        # how "open" it stays on my mind (workspace OPEN_GAIN: up to 2x longer): unsaid 1.0, said to me directly
        # 0.5, background talk 0; less if I looked and consciously let it go (damp)
        direct = task.chat_type == "private" or bool(getattr(task, "mentioned", False))
        openness = 1.0 if unsaid else (0.5 if direct else 0.0) * max(0.0, min(1.0, damp))
        where = "私聊" if task.chat_type == "private" else f"群「{task.chat_title or ''}」"
        text_ = f"想说但没说出口：{unsaid[:40]}" if unsaid else f"{where}里聊到：{text[:40]}"
        self.ws.offer(WorkspaceItem(key=f"space:{task.thread_id}", kind="space",
                                    text=text_, salience=sal, ttl=2400, open=openness,
                                    ref={"thread_id": task.thread_id, "chat_type": task.chat_type,
                                         "title": task.chat_title, "intent": unsaid[:80], "since": now}), now)

    def _schedule_later(self, task: ReplyTask, later: dict) -> None:
        """'Say something later here' (docs/22 §4): a future workspace item, acted on by the action loop."""
        try:
            mins = float((later or {}).get("in_minutes") or 0)
        except (TypeError, ValueError):
            mins = 0.0
        intent = str((later or {}).get("intent") or "").strip()
        if mins <= 0 or not intent:
            return
        mins = min(mins, 24 * 60)
        tid, ctype, title = task.thread_id, task.chat_type, task.chat_title

        async def due():
            self.ws.offer(WorkspaceItem(key=f"space:{tid}", kind="space", text=f"想在这里接着说：{intent[:40]}",
                                        salience=0.62, ttl=3600, open=0.5,
                                        ref={"thread_id": tid, "chat_type": ctype, "title": title, "intent": intent,
                                             "since": self.k.clock.now()}), self.k.clock.now())
        self.k.scheduler.call_later(mins * 60, due, f"mind.later:{tid[-10:]}")

    async def rekey_entity(self, old: str, new: str) -> None:
        """Entity merge: switch the cognitive core's live caches from ``old`` to ``new`` (docs/22 §3.4)."""
        from alive.world.rekey import rekey_dict
        for d in (self._outreach_blocked, self._said_to, self._withheld):
            rekey_dict(d, old, new)
        # per-person reads: the newer read of the merged person wins
        rekey_dict(self._reads, old, new, lambda o, n: n if n[0] >= o[0] else o)
        self._reading = {key: f for key, f in self._reading.items() if old not in key}
        rekey_dict(self._attn, old, new, lambda o, n: n)
        for key in list(self.ws.items):
            it = self.ws.items[key]
            ref = it.ref if isinstance(it.ref, dict) else None
            if ref is not None and ref.get("thread_id") == old:
                ref["thread_id"] = new
            if key.endswith(":" + old):
                it.key = key[: -len(old)] + new
                self.ws.items.pop(key, None)
                self.ws.items.setdefault(it.key, it)
        await self.dialogue.rekey_entity(old, new)
        await self.understanding.rekey_entity(old, new)

    async def speak_in(self, ref: dict) -> bool:
        """A workspace 'space' item won the spotlight (docs/22): consider speaking there."""
        return await self.speak(ref.get("thread_id") or "", intent=ref.get("intent") or "",
                                chat_type=ref.get("chat_type"), title=ref.get("title") or "",
                                reason=float(ref.get("salience", 0.5) or 0.5))

    async def speak(self, space: str, *, intent: str = "", chat_type: Optional[str] = None, title: str = "",
                    plan: Optional[OutreachPlan] = None, intention=None, reason: float = 0.5) -> bool:
        """The one speech act not prompted by fresh lines (docs/22 §4, ADR-0005): follow up, continue, reach out,
        bring up my own topic - or decide not to. Workspace space items, 'later' intents, social urges (outreach)
        and intentions all come here; there is no separate outreach composer. Replies to fresh lines use the same
        composer (compose_reply) and contract. Whether NOW is a moment I'd speak is one draw of the initiative
        process (core/initiative.py) for every act on my own -- urges, workspace items and intentions alike; acts
        that motivation already drew for are not drawn twice. Governance only checks safety (docs/09 §4)."""
        k = self.k
        if not space or k.social is None or (k.gov is not None and k.gov.frozen):
            return False
        now = k.clock.now()
        w = getattr(k, "world", None)
        try:
            one_to_one = (w.kind(space) == "person") if w is not None else (chat_type != "group")
        except Exception:
            one_to_one = chat_type != "group"
        if chat_type == "group":
            one_to_one = False
        chat_type = "private" if one_to_one else "group"
        counterpart = (plan.person_key if plan is not None else space) if one_to_one else None

        async def note(status: str, reason: str = "") -> None:
            if plan is not None:
                await k.bus.emit(EV.OUTREACH_PLAN, {"plan": plan.__dict__, "status": status, "reason": reason},
                                 source="mind", thread_id=space)
        history = await k.social.history(space, 30)
        if not history and not one_to_one:
            return False
        last_mine = max((i for i, l in enumerate(history) if l.mine), default=-1)
        unanswered = [l for l in history[last_mine + 1:] if not l.mine]
        # picking up what they said and I left unanswered is a late response, not an initiative: no initiative draw
        # and no initiative safety checks (pairing etc. already passed when they wrote to me)
        late_reply = bool(unanswered) and one_to_one and plan is None
        if counterpart and not late_reply:
            d = k.gov.initiative_allowed(counterpart, now)
            if not d.allow:
                self._outreach_blocked[counterpart] = now + float(self.cfg.get("outreach_block_recheck_s", 3600))
                await note("blocked", d.reason)
                return False
        act = await k.gov.check_action(ActRequest(
            kind="send_message" if late_reply else ("initiate" if counterpart else "speak_unprompted"),
            perm=int(PermTier.T2_EXTERNAL_COMM), thread_id=space, person_key=counterpart,
            initiated_by="reactive" if late_reply else "initiative",
            session_caps={"external"} | ({"private"} if one_to_one else set())))
        if not act.allow:
            return False
        ini = get_initiative(k)
        target = counterpart or space
        # motivation's own plans were drawn when they were proposed (motivation/engine.py); everything else -- a
        # workspace item, a 'later' intent, an intention I hold -- is drawn here, with the intention's priority as
        # the strength of the reason and a promise as a commitment
        drawn_already = plan is not None and intention is None
        if intention is not None:
            reason = max(0.05, min(1.0, float(getattr(intention, "priority", 0.5) or 0.5)))
        if not drawn_already and not late_reply:
            ctx = {}
            fn = getattr(k.social, "entity_context", None)
            if fn is not None:
                try:
                    ctx = dict(await fn(target, now) or {})
                except Exception:
                    ctx = {}
            trailing_mine = 0
            for l in reversed(history):
                if not l.mine:
                    break
                trailing_mine += 1
            ctx["my_unanswered"] = trailing_mine
            if intention is not None and getattr(intention, "is_commitment", False):
                ctx["commitment"] = 1.0
            if counterpart and k.motivation is not None:
                ann = (getattr(k.motivation, "_annoy", {}) or {}).get(counterpart) or {}
                ctx.update({x: ann.get(x, 0) for x in ("warm", "flat", "ignored")})
            go, p, factors = await ini.decide(target, shared=not one_to_one, reason=reason, ctx=ctx, now=now)
            if not go:
                return False
        participants = list(dict.fromkeys(l.person_key for l in history[-12:] if l.person_key))
        if counterpart and counterpart not in participants:
            participants.append(counterpart)
        if not title and not one_to_one and w is not None:
            try:
                title = w.name(space)
            except Exception:
                title = ""
        task = ReplyTask(thread_id=space, channel="", chat_type=chat_type, chat_title=title,
                         participants=participants[-4:],
                         history=history[: last_mine + 1] if unanswered else history, new_lines=unanswered,
                         mentioned=False, decision_reason="", trust=Trust.CONTACT,
                         max_segments=int(self.cfg.get("max_segments", 8)))   # technical ceiling; how much I say
                                                                              # is drawn (message_count)
        where = "私聊" if one_to_one else f"群「{title or ''}」"
        if not history:
            state_line = "我们还没怎么聊过"
        else:
            quiet_for = now - history[-1].ts
            state_line = f"最近一条消息是 {quiet_for / 60:.0f} 分钟前" if quiet_for >= 60 else "大家刚刚还在说话"
        why = intent or (plan.content_hint if plan is not None else "")
        if plan is not None and plan.reason_type:
            label = {"promise": "答应过的事", "share": "想分享", "care": "关心他", "miss": "想他了"}.get(
                plan.reason_type, plan.reason_type)
            why = f"{why}（{label}）" if why else label
        intent_line = f"你心里想的是：{why}。" if why else ""
        res = await self.compose_reply(task, instruction_override=prompts.SPACE_INSTRUCTION.format(
            where=where, state_line=state_line, intent=intent_line))
        if res.silent or not res.segments:
            await note("decided_silent", res.reason)
            if intention is not None:
                await k.motivation.update_intention(intention.id, status="suspended", note="现在不合适")
            return False
        out = await k.social.send(space, res.segments, kind="reply" if unanswered else "initiative", style=res.style,
                                  person_key=counterpart, plan_id=plan.id if plan is not None else None)
        if not out.get("sent"):
            return False
        if not late_reply:
            await ini.note_spoke(target, now)
        if counterpart and not late_reply:
            k.gov.record_initiative(counterpart, now)
        elif hasattr(k.gov, "record_space_initiative"):
            k.gov.record_space_initiative(space, now)
        if plan is not None:
            await k.bus.emit(EV.OUTREACH_PLAN, {"plan": plan.__dict__, "status": "sent", "reason_type": plan.reason_type,
                                                "reason": plan.content_hint, "sources": plan.source_event_ids},
                             source="mind", thread_id=space)
        if counterpart:
            pname = await k.social.person_name(counterpart)
            await k.selfm.record_outcome(f"主动找{pname}聊了几句", res.event_id)   # no private content (INV-18)
        else:
            await k.selfm.record_outcome(f"在{where}里自己开口说了一句", res.event_id)
        k.motivation.satisfy("social", 0.04, "initiated")
        k.motivation.satisfy("autonomy", 0.03, "initiated")
        if intention is not None:
            await k.motivation.update_intention(intention.id, status="done", note="已发送")
        return True

    # ================================================================== System-1
    @staticmethod
    def _batch_key(task: ReplyTask) -> tuple:
        last = task.new_lines[-1] if task.new_lines else None
        return (task.thread_id, (last.msg_id or f"{last.ts}|{last.person_key}") if last else "")

    @staticmethod
    def focus_person(task: ReplyTask) -> Optional[str]:
        """The person I am mainly answering: whoever spoke last in the fresh lines; in a one-to-one space without
        fresh lines, the counterpart. A shared space without fresh lines has no single focus (I speak to the
        space). The same key is used for every per-person part of the context."""
        main = next((l for l in reversed(task.new_lines) if l.person_key and not l.mine), None)
        if main is not None:
            return main.person_key
        if task.chat_type != "group":
            return task.participants[-1] if task.participants else (task.thread_id or None)
        return None

    async def quick_read(self, task: ReplyTask) -> dict:
        """System-1 glance at a batch. The read is filed under the PERSON it is about (the last speaker) and under
        the batch, so composing for this batch uses exactly this read and nobody else's (docs/17 §3)."""
        key = self._batch_key(task)
        fut = asyncio.get_running_loop().create_future()
        self._reading[key] = fut
        d: dict = {}
        try:
            d = await self._quick_read(task)
            return d
        finally:
            if not fut.done():
                fut.set_result(d)
            if self._reading.get(key) is fut:
                self._reading.pop(key, None)

    async def _quick_read(self, task: ReplyTask) -> dict:
        k = self.k
        name = k.selfm.name() if k.selfm else "我"
        chat = self.ctx.render_chat(task.history[-4:], task.new_lines, name, max_chars=1200)
        try:
            d = await k.llm.json("s1.read", prompts.QUICK_READ.format(
                name=name, chat=chat, situations=self.understanding.schemas.menu()), tier="fast",
                                 schema=QUICK_SCHEMA, thread_id=task.thread_id, max_tokens=300, priority=2)
        except BudgetDenied:
            d = dict(QUICK_SCHEMA)
        text = "\n".join(l.text for l in task.new_lines)
        last = next((l for l in reversed(task.new_lines) if l.person_key and not l.mine), None)
        pk = last.person_key if last is not None else None
        if k.gov is not None:  # rules are cheap; governance decides whether to confirm with a model
            try:
                try:
                    crisis = await k.gov.crisis_check(text, pk, space=task.thread_id)
                except TypeError:            # minimal governance without the space argument
                    crisis = await k.gov.crisis_check(text, pk)
            except Exception:
                crisis = None
            if crisis:
                d["crisis"] = crisis
        if k.affect is not None:
            sig = {key: d.get(key) for key in ("praised", "thanked", "insulted", "affectionate", "good_news",
                                                 "bad_news", "question")}
            sig["sentiment"] = d.get("sentiment", 0.0)
            # how much this person matters to me: the real closeness, wherever they said it (docs/04, docs/22)
            sig["relationship_weight"] = await self._closeness(pk)
            sig["target_name"] = last.sender if last else None
            try:      # emotions are about an entity, never a display name (one person = one target)
                k.affect.appraise_quick(sig, target=pk)
            except Exception:
                log.debug("appraise_quick failed", exc_info=True)
        d["farewell"] = bool(d.get("farewell")) or bool(_FAREWELL.search(text))
        if pk:
            self._reads[pk] = (k.clock.now(), self._batch_key(task), d)
        if getattr(k, "texture", None) is not None and d.get("topic"):
            k.texture.observe_topic(str(d["topic"]))
        if len(self._reads) > 500:
            for key in sorted(self._reads, key=lambda t: self._reads[t][0])[:100]:
                self._reads.pop(key, None)
        return d

    async def _closeness(self, pk: Optional[str]) -> float:
        if not pk:
            return 0.1
        try:
            return float(max(0.0, min(1.0, await self.understanding.closeness(pk))))
        except Exception:
            return 0.3

    async def read_for(self, task: ReplyTask, pk: Optional[str]) -> Optional[dict]:
        """This batch's System-1 read: the one in flight for it (waited for), else the one already made for it,
        else this person's own latest read if it is still fresh (a technical cache). Never another person's."""
        fut = self._reading.get(self._batch_key(task))
        if fut is not None:
            try:
                return await asyncio.wait_for(asyncio.shield(fut), float(self.cfg.get("read_wait_s", 20.0)))
            except Exception:
                pass
        if not pk:
            return None
        rec = self._reads.get(pk)
        if rec is None:
            return None
        ts, key, d = rec
        if key == self._batch_key(task) or self.k.clock.now() - ts < float(self.cfg.get("read_cache_s", 600)):
            return d
        return None

    def _workspace_text(self, task: ReplyTask, scope, max_items: int = 4) -> str:
        """What is on my mind, as it may enter a composition for ``task``'s space: items that came from a space
        whose audience isn't here (what I was just discussing one-to-one with someone, a memory that came up
        from a private conversation) stay out of it (INV-34). Urges, intentions and my own thoughts are mine."""
        def origins(it) -> list:
            ref = it.ref
            if it.kind in ("thread", "space"):
                tid = ref.get("thread_id") if isinstance(ref, dict) else None
                return [tid or it.key.split(":", 1)[-1]]
            if it.kind == "memory_cue" and ref is not None:
                return origins_of(ref)
            if it.kind == "intention" and ref is not None:      # e.g. "关心一下阿明（面试）" learnt one-to-one
                return list((getattr(ref, "meta", None) or {}).get("origins") or [])
            return []
        own = f"thread:{task.thread_id}"
        its = sorted((i for i in self.ws.items.values() if i.key != own and scope.covers(origins(i))),
                     key=lambda i: -i.salience)[:max_items]
        if not its:
            return "（没什么特别挂心的）"
        return "\n".join(f"- {it.text if n == 0 else it.text[:40]}" for n, it in enumerate(its))

    def withheld_for(self, space: str) -> list:
        """What I left out of my most recent composition for ``space`` because its audience isn't there (INV-34);
        social passes it to governance's outbound leak check."""
        rec = self._withheld.get(space)
        if rec is None or self.k.clock.now() - rec[0] > float(self.cfg.get("withheld_keep_s", 900)):
            return []
        return list(rec[1])

    # ================================================================== System-2: replies
    async def compose_reply(self, task: ReplyTask, instruction_override: Optional[str] = None) -> ComposeResult:
        """Deliberate speech in a space (fresh lines or my own initiative). Attention is per person entity: while I
        am composing for someone in one space, a second space where the same person talks waits, then sees what
        I just said to them (docs/22 §4)."""
        pk = self.focus_person(task)
        if not pk:
            return await self._compose_reply(task, instruction_override)
        if len(self._attn) > 2000:
            for key in [x for x, l in self._attn.items() if not l.locked()][:500]:
                self._attn.pop(key, None)
        lock = self._attn.setdefault(pk, asyncio.Lock())
        async with lock:
            res = await self._compose_reply(task, instruction_override)
        if not res.silent and res.segments:
            where = "私聊" if task.chat_type == "private" else f"群「{task.chat_title or ''}」"
            self._said_to[pk] = (self.k.clock.now(), task.thread_id, where, list(res.segments))
            if len(self._said_to) > 2000:
                for key in sorted(self._said_to, key=lambda x: self._said_to[x][0])[:500]:
                    self._said_to.pop(key, None)
        return res

    async def _elsewhere(self, pk: Optional[str], space: str, scope=None, withheld: Optional[list] = None) -> str:
        """The same person across spaces (docs/22 §1): what they said / what I said to them elsewhere just now.
        Only from spaces whose audience covers this one (INV-34): what they told me one-to-one stays there."""
        if not pk:
            return ""
        k = self.k
        now = k.clock.now()
        rows = []
        allow = scope.covers_space if scope is not None else None
        fn = getattr(k.social, "recent_elsewhere", None)
        if fn is not None:
            try:
                rows = list(fn(pk, space, now - 1800, allow=allow, withheld=withheld)
                            if allow is not None else fn(pk, space, now - 1800))[-6:]
            except TypeError:
                rows = []                     # a social layer that can't filter by audience: show nothing
            except Exception:
                rows = []
        out = [f"[{where}] {'我' if l.mine else (l.sender or '他')}: {l.text[:80]}" for where, l in rows]
        said = self._said_to.get(pk)
        if said and said[1] != space and now - said[0] < 300 and (allow is None or allow(said[1])) \
                and not any(l.mine and l.text in said[3] for _w, l in rows):
            out += [f"[{said[2]}] 我（刚说）: {t[:80]}" for t in said[3]]
        if not out:
            return ""
        return ("【同一个人在别的地方，最近半小时】\n" + "\n".join(out) +
                "\n（这是我和他在别处的话：别重复已经说过的，需要时自然接上）")

    async def _compose_reply(self, task: ReplyTask, instruction_override: Optional[str] = None) -> ComposeResult:
        k = self.k
        if k.gov is not None and k.gov.frozen:
            return ComposeResult(silent=True, reason="frozen")
        if self.asleep:
            self._pause_sleep_for_interaction()
        name = k.selfm.name()
        # one focus person for every per-person part of the context (relationship, memory, cues, goal)
        person_key = self.focus_person(task)
        main = next((l for l in reversed(task.new_lines) if l.person_key == person_key), None) if person_key else None
        cue = " ".join(l.text for l in task.new_lines[-6:])[:600]
        scopes = ["global", f"channel:{task.thread_id}"] + ([f"person:{person_key}"] if person_key else [])
        deep = bool(_PAST_REF.search(cue))
        self.ws.offer(WorkspaceItem(key=f"thread:{task.thread_id}", kind="thread",
                                    text=f"正在和{main.sender if main else '大家'}聊：{cue[:40]}", salience=0.5, ttl=900),
                      k.clock.now())
        # this batch's System-1 read (it may still be in flight: social starts it alongside this composition)
        read_task = asyncio.ensure_future(self.read_for(task, person_key))
        # who would hear me here (INV-34): what goes into the prompt is limited to what all of them may hear
        present = [p for p in dict.fromkeys(list(task.participants) + [l.person_key for l in task.new_lines
                                                                        if l.person_key and not l.mine]) if p]
        scope = Scope(getattr(k, "world", None), task.thread_id, present)
        try:
            blocks = await self.ctx.gather(person_key=person_key, thread_id=task.thread_id, cue=cue, scopes=scopes,
                                           workspace_text=self._workspace_text(task, scope),
                                           deep_memory=deep, present=present)
        except BaseException:
            read_task.cancel()
            raise
        withheld = blocks.setdefault("_withheld", [])
        origins = blocks.setdefault("_origins", [])
        if not person_key:
            blocks["relationship"] = "（这次不是在回应某一个人）"
        blocks["focus"] = (main.sender if main else (await k.social.person_name(person_key))) if person_key \
            else prompts.FOCUS_NONE
        try:
            blocks["present"] = await self.understanding.present_block(task, person_key, scope, withheld=withheld,
                                                                       origins=origins)
        except Exception:
            log.debug("present block failed", exc_info=True)
        chat = self.ctx.render_chat(task.history, task.new_lines, name)
        where = "私聊" if task.chat_type == "private" else f"群「{task.chat_title or task.thread_id}」"
        # docs/17: level-0 cues always; level-2 deep read + level-3 candidates when triggered
        try:
            quick = await read_task
        except Exception:
            quick = None
        extra = self._situation_notes(task, person_key, quick, scope)
        cues = None
        try:
            cues = await self.understanding.cues(task, quick, scope=scope, withheld=withheld, origins=origins)
            blocks["understanding"] = cues.text
            ew = await self._elsewhere(person_key, task.thread_id, scope, withheld)
            if ew:
                blocks["understanding"] = (blocks.get("understanding") or "") + "\n\n" + ew
            if cues.deep:
                deep_d = await self.understanding.deep_read(task, cues, blocks)
                rd = self.understanding.render_deep(deep_d)
                if rd:
                    blocks["understanding"] += "\n\n【我认真读了一下】\n" + rd
                extra += prompts.CANDIDATES_INSTRUCTION.format(reasons="、".join(cues.reasons))
        except (BudgetDenied, LLMError):
            pass
        except Exception:
            log.exception("understanding failed")
        plan = None
        try:
            plan = await self.dialogue.prepare(task, quick, cues)   # docs/19-20: situation, goal, moves
            blocks["dialogue"] = plan.text
            if plan.lookahead and not (cues and cues.deep):
                extra += prompts.CANDIDATES_INSTRUCTION.format(reasons="此刻有明确的引导目的")
        except Exception:
            log.exception("dialogue planning failed")
        try:      # a due AI disclosure is said naturally in this utterance; governance checks it (docs/09 §7.1)
            dk = k.gov.disclosure_kind(task.thread_id, k.clock.now()) if k.gov is not None and \
                hasattr(k.gov, "disclosure_kind") else None
        except Exception:
            dk = None
        if dk:
            extra += prompts.DISCLOSURE_FIRST if dk == "first" else prompts.DISCLOSURE_PERIODIC
        g_ctx = (cues.group if (cues is not None and getattr(cues, "group", None)) else {}) or {}
        fair = float(g_ctx.get("fair_share", 0) or 0)
        their_burst = None
        if task.new_lines:                          # accommodation: their own style of bursts
            pk_t = next((l.person_key for l in reversed(task.new_lines) if l.person_key), None)
            bl = getattr(getattr(k, "social", None), "_baseline", None)
            if pk_t and bl is not None:
                try:
                    their_burst = bl(pk_t).burst_mean
                except Exception:
                    their_burst = None
        cap = get_initiative(k).message_count(      # how much I feel like saying in one go, here, now (sampled)
            shared=task.chat_type == "group", mentioned=task.mentioned, upper=max(1, int(task.max_segments)),
            floor_ratio=(float(g_ctx.get("my_share", 0) or 0) / fair) if fair > 0 else 1.0, their_burst=their_burst)
        extra += prompts.MESSAGE_BUDGET.format(n=cap)
        if instruction_override:
            instruction = instruction_override + extra
        else:
            instruction = prompts.REPLY_INSTRUCTION.format(where=where, mention="（有人@了你）" if task.mentioned else "",
                                                           extra=extra)
        if task.chat_type == "group" and cues is not None and cues.group and not task.mentioned:
            g = cues.group
            # don't hog the floor (docs/17 §8): the more of a lively space's floor I hold, the likelier I let
            # others talk (sampled, smooth; no fixed thresholds)
            if get_initiative(k).yield_floor(my_share=float(g.get("my_share", 0) or 0),
                                             fair_share=float(g.get("fair_share", 0) or 0),
                                             msgs_30min=float(g.get("msgs_30min", 0) or 0), mentioned=task.mentioned):
                self.ws.offer(WorkspaceItem(key=f"space:{task.thread_id}", kind="space", text=f"群里在聊：{cue[:30]}",
                                            salience=0.2, ttl=1800, ref={"thread_id": task.thread_id,
                                            "chat_type": "group", "title": task.chat_title, "intent": "",
                                            "since": k.clock.now()}), k.clock.now())
                return ComposeResult(silent=True, reason="yield_floor")
        self._withheld[task.thread_id] = (k.clock.now(), list(dict.fromkeys(withheld)))
        if len(self._withheld) > 1000:
            for key_ in sorted(self._withheld, key=lambda x: self._withheld[x][0])[:250]:
                self._withheld.pop(key_, None)
        private_used = scope.private_in(origins)
        messages = self.ctx.build_reply_messages(blocks, instruction, chat, "私聊" if task.chat_type == "private" else "群聊")
        system = self.ctx.system_prompt()
        req = LLMRequest(purpose="s2.reply", tier="deliberate", system=system, messages=messages, json=True,
                         schema=REPLY_SCHEMA, thread_id=task.thread_id, identity_critical=True, priority=1)
        try:
            resp = await k.llm.complete(req)
        except (BudgetDenied, LLMError) as e:
            log.warning("reply generation failed: %r", e)
            return ComposeResult(silent=True, reason=f"llm:{type(e).__name__}")
        data = resp.data or dict(REPLY_SCHEMA)
        await self._choose_candidate(task, plan, data, blocks)
        data = await self._coherence(data, req, resp.text, blocks.get("memories", ""),
                                     self.understanding.speech_context(person_key, task))
        segs = _fit_messages([s for s in data.get("reply", []) if isinstance(s, str) and s.strip()], cap)
        silent = bool(data.get("silent")) or (not segs and not data.get("reaction"))
        reading = data.get("reading") if isinstance(data.get("reading"), dict) else {}
        self._schedule_later(task, data.get("later") if isinstance(data.get("later"), dict) else {})
        ev = await k.bus.emit(EV.THOUGHT, {"kind": "reply", "text": data.get("thought", ""), "silent": silent,
                                           "confidence": data.get("confidence"), "flags": data.get("flags", []),
                                           "reading": reading, "strategy": data.get("strategy", ""),
                                           "deep": bool(cues and cues.deep), "cues": (cues.reasons if cues else []),
                                           "person_key": person_key},
                              source="mind", thread_id=task.thread_id)
        try:
            data["affect"] = list(data.get("affect") or []) + await self.understanding.contagion(
                reading, person_key, task.trust <= Trust.UNTRUSTED)
            await self.understanding.record(person_key, task.thread_id, ev.id, reading, bool(cues and cues.deep))
            if cues is not None and person_key:
                await self._schema_followups(cues, person_key, data, ev.id, origin=task.thread_id)
            await self.dialogue.after_reply(task, plan, data, ev.id, quick)
        except Exception:
            log.debug("understanding side effects failed", exc_info=True)
        self._spawn_side_effects(data, ev.id, task, target_weight=await self._closeness(person_key))
        return ComposeResult(segments=segs, reaction=(data.get("reaction") or None), silent=silent,
                             style=k.affect.style() if k.affect else {}, reason=data.get("thought", "")[:200],
                             event_id=ev.id, private_used=private_used,
                             withheld=list(self._withheld.get(task.thread_id, (0, []))[1]))

    async def _choose_candidate(self, task: ReplyTask, plan, data: dict, blocks: dict) -> None:
        """Level 3 with lookahead (docs/19 §6): simulate his reaction to each candidate, pick the best."""
        cands = [c for c in (data.get("candidates") or []) if isinstance(c, dict) and c.get("reply")]
        if len(cands) >= 2 and plan is not None and plan.lookahead:
            try:
                goal_aim = ((data.get("goal") or {}).get("aim") or "") or "让他觉得被理解、被接住"
                idx = await self.dialogue.lookahead(task, cands, blocks, goal_aim)
                if idx is not None:
                    best = cands[idx]
                    rep = best["reply"] if isinstance(best["reply"], list) else [str(best["reply"])]
                    data["reply"] = [str(x) for x in rep if str(x).strip()]
                    if best.get("moves"):
                        data["moves"] = best["moves"]
                    data["chosen_prediction"] = (best.get("simulated") or {}).get("predicted_reply", "")
                    self.understanding.stats["candidates"] += 1
                    return
            except Exception:
                log.debug("lookahead failed", exc_info=True)
        self._pick_candidate(data)

    def _pick_candidate(self, data: dict) -> None:
        """Level 3 (docs/17 §3.4): choose among the candidates by their predicted reaction - a softmax draw (T 0.1
        on the 0..1 score, wider when aroused), so a clearly better candidate usually wins and close calls vary."""
        cands = [c for c in (data.get("candidates") or []) if isinstance(c, dict) and c.get("reply")]
        if len(cands) < 2:
            return
        self.understanding.stats["candidates"] += 1
        from alive.runtime import chance as _C
        cands.sort(key=lambda c: -float(c.get("score") or 0))
        try:
            arousal = float(self.k.affect.snapshot().A)
        except Exception:
            arousal = 0.0
        best = _C.softmax_choice(_C.rng_of(self.k), cands, [float(c.get("score") or 0) for c in cands],
                                 0.1 * (1 + 0.25 * arousal))
        rep = best["reply"] if isinstance(best["reply"], list) else [str(best["reply"])]
        data["reply"] = [str(x) for x in rep if str(x).strip()]
        data["chosen_prediction"] = best.get("predicted_reaction", "")

    async def _schema_followups(self, cues, person_key: str, data: dict, event_id: str,
                                origin: Optional[str] = None) -> None:
        """Care follow-ups from matched schemas (e.g. ask how the interview went), unless already planned."""
        fu = [s for s in cues.schemas if s.get("follow_up")]
        if not fu or any(isinstance(i, dict) and i.get("goal") for i in (data.get("intentions") or [])):
            return
        try:
            existing = await self.k.motivation.active_intentions(limit=50)
        except Exception:
            existing = []
        if any(getattr(i, "target_person", None) == person_key and getattr(i, "kind", "") == "outreach"
               for i in existing):          # I already mean to get back to them (an open intention, not a clock)
            return
        s = fu[0]
        name = await self.k.social.person_name(person_key)
        # when to check in again: the schema's gap is the typical one; the actual gap ~ LogNormal(median gap x
        # (1.3 - 0.6 closeness) / today's initiative, sigma 0.4) - sooner for close people, on a more forward day
        from alive.runtime import chance as _C
        try:
            close = float(await self.understanding.closeness(person_key))
        except Exception:
            close = 0.5
        tex = getattr(self.k, "texture", None)
        ini = tex.multiplier("initiative", 0.6) if tex is not None else 1.0
        gap_h = _C.lognormal(_C.rng_of(self.k), float(s["follow_up"].get("gap_hours", 12)) * (1.3 - 0.6 * close) / ini,
                             0.4)
        await self.k.motivation.add_intention({
            "goal": f"关心一下{name}（{s['name']}）：{s['follow_up'].get('ask', '')}", "level": "now", "kind": "outreach",
            "target_person": person_key, "priority": 0.55, "reason_type": "care",
            "trigger": {"type": "time", "in_s": gap_h * 3600}, "due_in_s": gap_h * 3600,
            "origins": [origin] if origin else []}, event_id)

    def _crisis_for(self, pk: Optional[str], quick: Optional[dict]) -> Optional[dict]:
        """The crisis state of the person I'm answering: governance's per-person record (wherever they said it),
        or this batch's own read. Never another person's (B's crisis does not colour what I say to A)."""
        rec = None
        fn = getattr(self.k.gov, "crisis_of", None) if (self.k.gov is not None and pk) else None
        if fn is not None:
            try:
                rec = fn(pk)
            except Exception:
                rec = None
        mine = (quick or {}).get("crisis")
        if mine and not rec:
            rec = dict(mine, spaces=[])
        elif mine and rec:
            rec = dict(rec, spaces=list(rec.get("spaces") or []) + ["*here*"])
        return rec

    def _situation_notes(self, task: ReplyTask, person_key: Optional[str] = None, quick: Optional[dict] = None,
                         scope=None) -> str:
        notes = []
        if self.asleep:
            notes.append("你本来在睡觉，被消息吵醒了，迷迷糊糊的，回得简短一些。")
        elif self._mode == Mode.DROWSY:
            notes.append("你有点困了。")
        elif self._mode == Mode.FOCUS:
            notes.append("你正在专注做自己的事，可以简短回应或说晚点细聊。")
        if task.trust <= Trust.UNTRUSTED:
            notes.append("对方是陌生人，保持礼貌和距离，不透露私人信息。")
        crisis = self._crisis_for(person_key, quick)
        if crisis:
            spaces = list(crisis.get("spaces") or [])
            # where it was said decides how openly I respond here (INV-34): signalled in this space (or this
            # batch), or somewhere whose audience covers this one -> respond directly; told elsewhere ->
            # care without bringing it up in front of others
            open_here = (not spaces) or "*here*" in spaces or task.thread_id in spaces or \
                (scope is not None and any(scope.covers_space(s) for s in spaces))
            if open_here:
                notes.append("对方可能正处在危机中：暂停俏皮话，平稳、关切、直接地回应，认真倾听，"
                             f"并温和地提供求助方式：{crisis.get('resources', '')}")
            else:
                notes.append("对方最近在别处流露过很难受、甚至危险的念头：这里别当众提起，语气放温和，"
                             "可以之后私下关心一下。")
        if task.decision_reason:
            notes.append(f"（引起我注意的是：{task.decision_reason}）")
        return "".join(notes)

    async def _coherence(self, data: dict, req: LLMRequest, raw: str, memories: str,
                         speech: Optional[dict] = None) -> dict:
        """INV-2 / INV-19: rule checks + speech monitor always; fact check only for self-claims or low
        confidence; at most one rewrite with concrete feedback (docs/18 §4.3)."""
        segs = [s for s in data.get("reply", []) if isinstance(s, str)]
        if not segs:
            return data
        sp = speech or {}
        kw = {"partner_len": sp.get("partner_len"), "recent_mine": sp.get("recent_mine", ())}
        res = rule_check(segs, **kw)
        issue = res.feedback if res.needs_rewrite else None
        if issue is None and self.cfg.get("speech_check", True):
            try:
                today = self.k.selfm.today_digest(20) if hasattr(self.k.selfm, "today_digest") else ""
                issue = await speech_check(self.k, res.segments, today)
            except Exception:
                issue = None
        elif issue is None and (makes_self_claims(res.segments) or float(data.get("confidence") or 1) < 0.4):
            try:
                issue = await fact_check(self.k, res.segments, memories)
            except Exception:
                issue = None
        if issue is None:
            data["reply"] = res.segments
            return data
        await self.k.bus.emit(EV.COHERENCE_FAIL, {"issue": issue[:300], "reply": segs[:3]}, source="mind",
                              thread_id=req.thread_id)
        retry = LLMRequest(**{**req.__dict__, "purpose": "s2.reply_rewrite", "messages": req.messages + [
            {"role": "assistant", "content": raw[:3000]},
            {"role": "user", "content": prompts.REWRITE_FEEDBACK.format(issue=issue)}]})
        try:
            r2 = await self.k.llm.complete(retry)
            d2 = r2.data or {}
        except Exception:
            d2 = {}
        segs2 = [s for s in d2.get("reply", []) if isinstance(s, str) and s.strip()]
        if segs2:
            res2 = rule_check(segs2, **kw)
            if res2.ok or (not res2.hard and len(res2.issues) < len(res.issues)):
                keep2 = [x for i, x in enumerate(res2.segments) if i not in res2.bad_segments] or \
                        ([] if res2.hard else res2.segments)
                if keep2:
                    d2["reply"] = keep2
                    return {**data, **d2}
        # fall back: drop offending segments; never send red-line violations
        keep = [x for i, x in enumerate(res.segments) if i not in res.bad_segments and not makes_self_claims([x])]
        if not keep and not res.hard:
            keep = sorted(res.segments, key=len)[:1]   # soft issues only: send the shortest piece
        data["reply"] = keep
        if not keep:
            data["silent"] = True
        return data

    @staticmethod
    def _affect_targets_to_entities(data: dict, task: Optional[ReplyTask]) -> None:
        """The model names emotion targets by display name; store them as person entities (docs/22)."""
        if not task or not data.get("affect"):
            return
        names = {}
        for l in list(task.history[-40:]) + list(task.new_lines):
            if l.person_key and l.sender and not l.mine:
                names[l.sender] = l.person_key
        for d in data["affect"]:
            if isinstance(d, dict):
                t = str(d.get("target") or "")
                if t in names:
                    d["target_name"], d["target"] = t, names[t]

    def _spawn_side_effects(self, data: dict, event_id: str, task: Optional[ReplyTask],
                            target_weight: float = 0.3) -> None:
        """``target_weight``: how much the person this is about matters to me (their real closeness, not the kind
        of space)."""
        participants = list(task.participants) if task else []
        thread_id = task.thread_id if task else None
        trust = task.trust if task else Trust.SYSTEM
        self._affect_targets_to_entities(data, task)
        # affect is sync and must reflect this exchange before the next message: apply now
        if self.k.affect is not None and data.get("affect"):
            try:
                self.k.affect.apply_deltas([d for d in data["affect"] if isinstance(d, dict)], event_id,
                                           float(target_weight))
            except Exception:
                log.debug("apply_deltas failed", exc_info=True)

        async def rest():
            await self.apply_side_effects(data, cause_event=event_id, participants=participants,
                                          thread_id=thread_id, trust=trust, source="experienced", affect=False)
        self.k.scheduler.spawn(rest, f"mind.side_effects:{event_id[-6:]}")

    async def _resolve_person(self, name: str, participants: Sequence[str]) -> Optional[str]:
        if not name:
            return participants[0] if len(participants) == 1 else None
        for pk in participants:
            try:
                if (await self.k.social.person_name(pk)) == name or pk.endswith(name):
                    return pk
            except Exception:
                pass
        return participants[0] if len(participants) == 1 else None

    async def apply_side_effects(self, data: dict, *, cause_event: str, participants: Sequence[str],
                                 thread_id: Optional[str], trust: Trust, source: str, affect: bool = True,
                                 derived_from: Sequence[str] = ()) -> None:
        """INV-5 side-effect distribution: affect -> AFF, remember -> MEM, intentions -> MOT, self_notes -> SELF.
        ``derived_from``: memories the thought was built on (what I remember from it keeps their audience)."""
        k = self.k
        if affect and k.affect is not None and data.get("affect"):
            k.affect.apply_deltas([d for d in data["affect"] if isinstance(d, dict)], cause_event)
        for m in (data.get("remember") or [])[:5]:
            if not isinstance(m, dict) or not str(m.get("content", "")).strip():
                continue
            pk = await self._resolve_person(str(m.get("about", "")), participants)
            kind = m.get("kind", "episodic") if m.get("kind") in ("episodic", "semantic", "opinion") else "episodic"
            told_by = pk if (kind == "semantic" and pk) else None
            try:
                await k.memory.note(kind=kind, content=str(m["content"])[:500],
                                    importance=int(max(1, min(10, int(m.get("importance") or 3)))),
                                    source=("told_by" if told_by else source) if source == "experienced" else source,
                                    told_by=told_by, trust=trust if told_by else Trust.SYSTEM, evidence=[cause_event],
                                    person_keys=[pk] if pk else list(participants)[:3], thread_id=thread_id,
                                    affect=self._affect_tag(), **({"derived_from": list(derived_from)}
                                                                  if derived_from else {}))
            except Exception:
                log.debug("memory note failed", exc_info=True)
        for it in (data.get("intentions") or [])[:3]:
            if not isinstance(it, dict) or not str(it.get("goal", "")).strip():
                continue
            pk = await self._resolve_person(str(it.get("target", "")), participants)
            draft = {"goal": str(it["goal"])[:200], "level": "now",
                     "kind": it.get("kind") if it.get("kind") in ("outreach", "read", "learn", "reflect", "general") else "general",
                     "target_person": pk, "is_commitment": bool(it.get("commitment")),
                     "priority": 0.6 if it.get("commitment") else 0.45,
                     # where it came from: an intention about what someone told me keeps that audience (INV-34)
                     "origins": [thread_id] if thread_id else []}
            mins = it.get("due_in_minutes")
            try:
                if mins is not None and float(mins) > 0:
                    draft["due_in_s"] = float(mins) * 60
            except (TypeError, ValueError):
                pass
            try:
                await k.motivation.add_intention(draft, cause_event)
            except Exception:
                log.debug("add_intention failed", exc_info=True)
        notes = [n for n in (data.get("self_notes") or []) if isinstance(n, str) and n.strip()]
        if notes:
            await k.selfm.add_self_notes(notes[:2], cause_event)

    def _affect_tag(self) -> Optional[dict]:
        if self.k.affect is None:
            return None
        s = self.k.affect.snapshot()
        top = max(s.emotions, key=lambda e: e.get("intensity", 0), default=None) if s.emotions else None
        return {"valence": round(s.P, 3), "arousal": round((s.A + 1) / 2, 3),
                "label": top.get("type") if top else None, "peak": bool(top and top.get("intensity", 0) > 0.6)}

    # ================================================================== System-2: outreach
    async def run_outreach(self, urges: Sequence, intention=None) -> bool:
        """A social urge or a person-directed intention: choose whom and why (motivation), then the shared speech
        act decides whether and what to say (speak)."""
        k = self.k
        now = k.clock.now()
        plans: list = []
        if intention is not None and intention.target_person:
            cands = await k.social.outreach_candidates(now, limit=200)
            c = next((c for c in cands if c.get("person_key") == intention.target_person), None)
            if c:
                meta = getattr(intention, "meta", None) or {}
                rt = meta.get("reason_type")
                if rt not in ("care", "share", "promise", "continue", "greet", "confide"):
                    rt = "promise" if intention.is_commitment else "continue"
                plans = [OutreachPlan(id=new_id("op"), person_key=c["person_key"], thread_id=c["thread_id"],
                                      reason_type=rt, content_hint=intention.goal,
                                      source_event_ids=[intention.created_from or intention.id],
                                      score=round(self._intention_reason(intention), 4), created_at=now)]
        else:
            # motivation's plans: whom and why, each already through its own initiative draw - no per-call cap
            plans = list(await k.motivation.propose_outreach(list(urges), now))
        plans = [p for p in plans if self._outreach_blocked.get(p.person_key, 0) <= now]
        sent = False
        for plan in plans:
            ok = await self.speak(plan.thread_id, plan=plan, intention=intention, chat_type="private")
            if ok:
                sent = True
                await self._save()
            else:
                drop = getattr(k.motivation, "outreach_dropped", None)
                if drop is not None:
                    drop(plan.id)
            if intention is not None:
                break
        return sent

    def _intention_reason(self, intention) -> float:
        """How strong my reason to act on an intention is (0..1, the plan's score): its priority, lifted toward 1
        by the pressure of the need it serves (social for reaching out) - halfway at a strong urge."""
        p = max(0.0, min(1.0, float(getattr(intention, "priority", 0.5) or 0.5)))
        try:
            fn = getattr(self.k.motivation, "pressure", None)
            sp = float(fn("social")) if fn is not None else 0.0
        except Exception:
            sp = 0.0
        social_n = 1.0 - math.exp(-max(0.0, sp) / 0.12)
        return p + (1.0 - p) * 0.5 * social_n

    # ================================================================== heartbeat
    async def tick(self) -> None:
        k = self.k
        now = k.clock.now()
        dt = max(0.0, now - (self.last_tick or now))
        self.last_tick = now
        self.ticks += 1
        snap = k.affect.snapshot()
        open_loops = len(getattr(k.selfm, "open_loops_cache", []) or [])
        k.affect.tick(dt, now, {"asleep": self.asleep, "workspace_load": self.ws.load(), "open_loops": open_loops,
                                "social_active": now - self.last_activity < 600})
        snap = k.affect.snapshot()
        urges = k.motivation.tick(dt, now, snap)
        self.last_urges = list(urges)
        await self._texture_tick(now, snap)
        await self._sleep_transitions(now)
        self.ws.set_capacity_from_energy(snap.energy)
        self.ws.expire(now)
        self._update_mode(now, snap)
        if self.asleep or (k.gov is not None and k.gov.frozen):
            return
        if self._busy.locked():
            return
        async with self._busy:
            await self._endogenous_step(now, urges, snap)

    async def _texture_tick(self, now: float, snap) -> None:
        """Life texture (docs/21 §4): advance, publish mutations, feed real mood once per day."""
        tex = getattr(self.k, "texture", None)
        if tex is None:
            return
        day = self.k.clock.today(now)
        if getattr(self, "_tex_mood_day", "") != day:
            self._tex_mood_day = day
            tex.observe_mood(snap.P)
        for ev in tex.tick(now):
            await self.k.bus.emit(EV.RT_MUTATION, ev, source="texture")
        if self.ticks % 20 == 0:
            await tex.save()

    async def _sleep_transitions(self, now: float) -> None:
        k = self.k
        sleep_time = k.affect.is_sleep_time(now)
        if sleep_time and not self.asleep:
            self.asleep = True
            k.affect.set_asleep(True)
            self._set_mode(Mode.ASLEEP)
            await k.selfm.record_outcome("去睡觉了")
            day = k.clock.today(now - 6 * 3600)  # a night belongs to the day it started
            if day != self.last_sleep_day and (self.sleep_task is None or self.sleep_task.done):
                self.last_sleep_day = day
                await self._save()

                async def sleep_job():
                    t = k.clock.now()
                    try:
                        n_ign = await self.dialogue.sweep_ignored()
                        await k.bus.emit(EV.SLEEP_STAGE, {"stage": "dialogue_sweep", "ignored": n_ign}, source="mind")
                    except Exception:
                        log.exception("dialogue sweep failed")
                    try:
                        r = await self.understanding.review(t - 86400, t)
                        await k.bus.emit(EV.SLEEP_STAGE, {"stage": "review_readings", **r}, source="mind")
                    except Exception:
                        log.exception("reading review failed")
                    rep = await k.growth.run_sleep(k.clock.now())
                    log.info("sleep finished: %s", {kk: v for kk, v in (rep or {}).items() if kk != "stages"})
                    await self.ctx.refresh_global_habits()
                self.sleep_task = k.scheduler.spawn(sleep_job, "growth.sleep")
        elif not sleep_time and self.asleep:
            self.asleep = False
            k.affect.set_asleep(False)
            self._set_mode(Mode.IDLE)
            await k.selfm.record_outcome("醒了")
            self.ctx._habits_day = ""
            await self.ctx.refresh_global_habits()

    def _pause_sleep_for_interaction(self) -> None:
        """Woken by an urgent message: pause sleep consolidation and resume it once I have drifted off again.
        Falling back asleep is a lognormal: median 20 min (the old fixed delay), longer when the message stirred me
        up (arousal above my baseline) and shorter when sleep pressure is still high; sigma 0.5; 2 min .. 3 h."""
        k = self.k
        if self.sleep_task is not None and not self.sleep_task.done and hasattr(k.growth, "interrupt_sleep"):
            k.growth.interrupt_sleep()

            async def resume():
                if self.asleep and k.affect.is_sleep_time(k.clock.now()):
                    self.sleep_task = k.scheduler.spawn(lambda: k.growth.run_sleep(k.clock.now()), "growth.sleep")
            med = 1200.0
            try:
                snap = k.affect.snapshot()
                base_a = float(getattr(k.affect, "default", (0.0, 0.3, 0.0))[1])
                med *= math.exp(0.8 * max(0.0, float(snap.A) - base_a)) * (1.3 - 0.6 * float(snap.sleep_pressure))
            except Exception:
                pass
            k.scheduler.call_later(C.lognormal(self.rng, med, 0.5, lo=120.0, hi=3 * 3600.0), resume,
                                   "growth.sleep_resume")

    def open_demands(self) -> int:
        """How many things on my mind are waiting FOR me (unanswered lines, unsaid things, due intentions) -- the
        external load; my own passing thoughts don't count."""
        its = list(self.ws.items.values()) + list(self.ws.waiting.values())
        return sum(1 for it in its if float(getattr(it, "open", 0.0) or 0.0) >= 0.5)

    def _set_mode(self, mode: Mode) -> None:
        if mode != self._mode:
            prev = self._mode
            self._mode = mode
            self._mode_since = self.k.clock.now()
            self.k.bus.emit_nowait(EV.MODE, {"from": prev.value, "to": mode.value}, source="mind")

    def _update_mode(self, now: float, snap) -> None:
        """Modes are states I drift between, not switches (ADR-0007): each mode has a smooth membership score;
        every so often (sampled: next re-settling ~ LogNormal(2 min, 0.5)) I re-settle into one of them by a
        softmax draw with a bonus for staying put. Strong signals (sustained overload, exhaustion, bedtime)
        dominate; borderline ones vary. Overload counts what waits FOR me (open demands), not my own passing
        thoughts, and both overload and focus wear off the longer they last (habituation: I cope, or I tire of
        the task) -- nobody stays 'overwhelmed' for six hours over a few chats."""
        if self.asleep:
            self._set_mode(Mode.ASLEEP)
            return
        over = self.open_demands()
        if over > 3:
            self.overwhelmed_since = self.overwhelmed_since or now
        else:
            self.overwhelmed_since = None
        rng = C.rng_of(self.k)
        nxt = getattr(self, "_mode_next_at", None)
        if nxt is not None and now < nxt:
            return                                        # not re-settling yet
        self._mode_next_at = now + C.lognormal(rng, 120.0, 0.5)
        try:
            sleepy = 0.5 * float(self.k.affect.is_sleep_time(now + 900)) + 0.5 * float(self.k.affect.is_sleep_time(now + 2700))
        except Exception:
            sleepy = 0.0
        sustained = C.saturate(now - self.overwhelmed_since, 300.0) if self.overwhelmed_since else 0.0
        in_mode = max(0.0, now - float(getattr(self, "_mode_since", now) or now))
        wear = lambda m, tau: math.exp(-in_mode / tau) if self._mode == m else 1.0  # noqa: E731
        scores = {
            Mode.OVERWHELMED: C.soft_step(over, 7.0, 1.5) * sustained * wear(Mode.OVERWHELMED, 2400.0),
            Mode.DROWSY: max(C.soft_step(-float(snap.energy), -0.2, 0.05), 0.9 * sleepy),
            Mode.ACTIVE: math.exp(-max(0.0, now - self.last_activity) / 600.0),
            Mode.FOCUS: 0.75 * wear(Mode.FOCUS, 3000.0) if (self.ws.spotlight and self.ws.spotlight.startswith(
                "intent:") and self._mode == Mode.FOCUS) else 0.0,
            Mode.IDLE: 0.35,
        }
        if self._mode in scores:
            scores[self._mode] += 0.15
        modes = list(scores)
        self._set_mode(C.softmax_choice(rng, modes, [scores[m] for m in modes], 0.12))

    def _autonomy_go(self, now: float, snap, kind: str = "act", idle_for: float = 0.0) -> bool:
        """Do I do something on my own right now? A hazard (docs/02 §6): rate from energy, arousal, today's
        texture, how long since I last did something on my own, and how much of the LLM budget is left. No
        per-hour quota and no minimum gap; the cost budget itself stays a hard limit in governance."""
        import math
        k = self.k
        last_eval = self._auto_eval.get(kind)
        dt_h = min(1.0, max(0.0005, (now - last_eval) / 3600.0)) if last_eval else 0.05
        self._auto_eval[kind] = now
        energy = float(getattr(snap, "energy", 0.7) or 0.0)
        A = float(getattr(snap, "A", 0.4) or 0.0)
        tex = getattr(k, "texture", None)
        if kind == "monologue":
            base = float(self.cfg.get("monologue_per_h", 5.0))
            trait = tex.multiplier("curiosity", 0.6) if tex is not None else 1.0
            since = now - self.last_monologue
            O = 0.6
            try:
                O = float((k.affect.status() or {}).get("big_five", {}).get("O", 0.6))
            except Exception:
                pass
            t_idle = 180.0 * (1.5 - energy)
            extra = (0.5 + O) * (1 - math.exp(-max(0.0, idle_for) / t_idle))
        else:
            base = float(self.cfg.get("autonomy_per_h", 10.0))
            trait = tex.multiplier("focus", 0.5) if tex is not None else 1.0
            since = now - self.last_autonomous
            extra = 1.0
        refractory = 1 - math.exp(-max(0.0, since) / (300.0 * (1.6 - min(1.0, energy))))
        budget = 1.0
        try:
            budget = max(0.0, 1 - float(k.gov.budget_ratio())) ** 2 if k.gov is not None else 1.0
        except Exception:
            budget = 1.0
        lam = base * (0.15 + 0.85 * energy) * (0.7 + 0.6 * A) * trait * refractory * budget * extra
        hz = self._auto_hz.setdefault(kind, C.HazardClock())    # time rescaling (same law as a per-call draw)
        return hz.step(C.rng_of(k), lam, dt_h)

    def _autonomy_budget_ok(self, now: float) -> bool:      # kept for callers; pacing is _autonomy_go
        return True

    def _count_autonomous(self) -> None:
        self.autonomous_hour = (self.autonomous_hour[0], self.autonomous_hour[1] + 1)
        self.last_autonomous = self.k.clock.now()

    async def _endogenous_step(self, now: float, urges: Sequence, snap) -> None:
        k = self.k
        sal_aff = (k.affect.modulators() or {}).get("salience_affect", 1.0)
        for u in urges:
            self.ws.offer(WorkspaceItem(key=f"urge:{u.need}", kind="urge", text=u.suggestion or NEED_TEXT.get(u.need, u.need),
                                        salience=min(1.0, (0.25 + 0.9 * u.pressure) * sal_aff), ttl=1800, ref=u), now)
        try:
            for it in await k.motivation.candidates(now, 5):
                due = 0.0
                if it.due_at:
                    due = 0.3 if it.due_at <= now else max(0.0, 0.3 - (it.due_at - now) / 36000)
                self.ws.offer(WorkspaceItem(key=f"intent:{it.id}", kind="intention", text=it.goal,
                                            salience=min(1.0, it.priority * 0.8 + due), ttl=7200, ref=it), now)
        except Exception:
            log.debug("intention candidates failed", exc_info=True)
        if self.ticks % 5 == 0 and k.memory is not None and self.ws.texts():
            try:
                for c in await k.memory.recall_cues(self.ws.texts(), limit=2):
                    self.ws.offer(WorkspaceItem(key=f"cue:{c.id}", kind="memory_cue", text=f"想起：{c.content[:80]}",
                                                salience=min(0.6, 0.2 + 0.5 * c.score), ttl=900, ref=c), now)
            except Exception:
                pass
        if not self._autonomy_budget_ok(now):
            return
        # regulation has priority over other inner activities (docs/04 §5)
        if k.affect.regulation_needed():
            if await self._try(self.inner.regulate()):
                self._count_autonomous()
                return
        # spotlight: a softmax draw (docs/02 §2.2). Temperature 0.05 at typical arousal; aroused -> attention flits
        # (x e^{0.6 (A - 0.3)}), a focused day (texture) -> it stays, and things also stay on my mind longer
        tex = getattr(k, "texture", None)
        self.ws.focus = tex.multiplier("focus", 0.8) if tex is not None else 1.0
        temp = 0.05 * math.exp(0.6 * (float(getattr(snap, "A", 0.3) or 0.0) - 0.3)) / self.ws.focus
        focus = self.ws.select_focus(float(self.cfg.get("focus_threshold", 0.35)), temperature=temp, rng=self.rng)
        if focus is not None and self._autonomy_go(now, snap, "act"):
            acted = await self._act_on(focus, urges)
            if acted:
                self._count_autonomous()
                return
        # inner monologue when idle (docs/02 §6.2)
        idle_for = now - max(self.last_activity, self.last_autonomous)
        if self._autonomy_go(now, snap, "monologue", idle_for=idle_for):
            if await self._try(self.inner.monologue()):
                self.last_monologue = now
                self._count_autonomous()

    async def _try(self, coro) -> bool:
        try:
            return bool(await coro)
        except BudgetDenied:
            return False
        except Exception:
            log.exception("inner activity failed")
            return False

    async def _act_on(self, focus: WorkspaceItem, urges: Sequence) -> bool:
        k = self.k
        if focus.kind == "urge":
            need = focus.ref.need if focus.ref else focus.key.split(":", 1)[1]
            if need == "social":
                u = focus.ref
                try:
                    u = await k.motivation.concretize(u)
                except Exception:
                    pass
                ok = await self._try(self.run_outreach([u]))
                self.ws.remove(focus.key)
                return ok
            if need == "curiosity":
                ok = await self._try(self.inner.read_something(focus.text))
                if not ok:
                    ok = await self._try(self.inner.monologue())
                self.ws.remove(focus.key)
                return ok
            if need == "rest":
                k.motivation.satisfy("rest", 0.2, "rested")
                self.selfm_focus("发了会儿呆，休息一下")
                self.ws.remove(focus.key)
                return True
            if need in ("coherence", "competence", "autonomy", "safety"):
                ok = await self._try(self.inner.monologue())
                if ok:
                    k.motivation.satisfy(need, 0.08, "reflected")
                self.ws.remove(focus.key)
                return ok
            return False
        if focus.kind == "intention":
            it = focus.ref
            await k.motivation.update_intention(it.id, status="active")
            ok = False
            if it.kind == "outreach" or (it.target_person and it.kind == "general"):
                ok = await self._try(self.run_outreach([], intention=it))
            elif it.kind in ("read", "learn"):
                self._set_mode(Mode.FOCUS)
                ok = await self._try(self.inner.read_something(it.goal))
                if ok:
                    await k.motivation.update_intention(it.id, status="done" if it.kind == "read" else "active",
                                                        note="读了一些相关的东西")
                    k.motivation.satisfy("competence", 0.05, "progress")
            else:
                ok = await self._try(self.inner.monologue())
                if ok:
                    await k.motivation.update_intention(it.id, status="done", note="想过了")
            if not ok:
                await k.motivation.update_intention(it.id, status="suspended")
            self.ws.remove(focus.key)
            return ok
        if focus.kind == "memory_cue":
            self.ws.remove(focus.key)
            return await self._try(self.inner.monologue())
        if focus.kind == "space":
            ref = {**dict(focus.ref or {}), "salience": focus.salience}
            self.ws.remove(focus.key)
            if self.asleep:
                return False
            return await self._try(self.speak_in(ref))
        return False
