"""Sleep pipeline (docs/06 §5.1): replay → reflect → extract → reconcile → learn → verify → forget → self →
plan → precompute → dream (optional) → train (L3 stub).

- Per-stage checkpoints in kv (ns ``grow.sleep``, key ``night:<YYYY-MM-DD>``): completed stages, the
  cross-stage state (replayed episodes, reflections, promotion count, plan) and failures. A wake-up
  (``interrupt()``) or a crash stops at a stage boundary (or inside long stages via ``ctx.check()``);
  the next ``run`` for the same night resumes with the first unfinished stage.
- Every stage outcome emits ``grow.sleep_stage`` (done | skipped | failed | interrupted).
- ``kernel.gov.learning_paused`` / ``frozen`` skip the learning stages (INV-16); memory upkeep and the
  day plan still run.
- Change-rate cap: at most ``max_promotions_per_night`` L2 promotions per night (counted in the checkpoint,
  so resuming cannot exceed it).
- Stages are pluggable (Consolidator SPI, docs/12): ``register_stage(name, fn, after=..., learning=...)``;
  ``fn(ctx: SleepContext) -> dict``.
"""
from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Sequence

from alive.core.types import EV, Trust
from alive.growth.curriculum import apply_agenda, build_agenda
from alive.growth.prompts import (DREAM_SCHEMA, DREAM_SYSTEM, DREAM_USER, LEARN_SCHEMA, LEARN_SYSTEM, LEARN_USER,
                                  PLAN_SCHEMA, PLAN_SYSTEM, PLAN_USER, REFLECT_I_SCHEMA, REFLECT_I_SYSTEM, REFLECT_I_USER,
                                  REFLECT_Q_SCHEMA, REFLECT_Q_SYSTEM, REFLECT_Q_USER, VERIFY_SCHEMA, VERIFY_SYSTEM,
                                  VERIFY_USER)
from alive.growth.evidence import evidence_gate
from alive.growth.util import clamp, field, truncate
from alive.runtime import chance

log = logging.getLogger("alive.growth")

# Nightly counts (ADR-0007): how much I replay and reflect is a draw scaled by how full the day was, not a quota.
#   day factor f = sqrt(episodes / 25) x sqrt((1 + affect intensity) / 2.5), clipped to [0.25, 3]
#   (25 remembered episodes and an affect intensity of ~1.5 - one peak plus some feeling - is a typical day: f = 1)
#   replay    ~ 1 + NegBin(mean 20 f - 1, dispersion 4)    (bursty; the old fixed 20 is the typical-day mean)
#   questions ~ 1 + Poisson(3 sqrt(f) x curiosity - 1)     (old fixed 3)
#   insights  ~ 1 + Poisson(5 sqrt(f) x curiosity - 1)     (old cap 5; the number I ask for)
# The ceilings (replay 60, questions 6, insights 10) are technical: prompt size and runaway model output.
DAY_EPISODES_REF = 25.0
DAY_INTENSITY_REF = 1.5

STAGES = ("replay", "reflect", "extract", "reconcile", "learn", "verify", "forget", "self", "plan", "precompute",
          "dream", "train")
LEARNING_STAGES = {"reflect", "learn", "verify", "self", "dream", "train"}
PLAN_KINDS = ("outreach", "read", "learn", "reflect", "rest", "create", "general", "free")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WIN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)\s*[-~–]\s*([01]?\d|2[0-4]):([0-5]\d)$")


class SleepInterrupted(Exception):
    pass


class SleepContext:
    """What a stage sees: persisted ``state`` plus runtime values. Long stages call ``check()``."""

    def __init__(self, pipeline: "SleepPipeline", state: dict, *, now: float, since: float, until: float, night: str,
                 date: str, explicit: bool):
        self._p = pipeline
        self.state = state
        self.now, self.since, self.until = now, since, until
        self.night, self.date, self.explicit = night, date, explicit

    def check(self) -> None:
        if self._p._interrupted:
            raise SleepInterrupted()

    def as_dict(self) -> dict:
        return {"now": self.now, "since": self.since, "until": self.until, "night": self.night, "date": self.date}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", str(s or "").strip().lower()).strip("_")
    return s[:48] or "general"


def _small(rep: Any) -> Any:
    try:
        txt = json.dumps(rep, ensure_ascii=False, default=str)
    except Exception:
        return {"repr": repr(rep)[:200]}
    return rep if len(txt) <= 2000 else {"truncated": txt[:2000]}


class SleepPipeline:
    def __init__(self, growth):
        self.g = growth
        self.k = growth.k
        self.cfg = growth.cfg["sleep"]
        self.order: list = list(STAGES)
        self.fns: dict = {s: getattr(self, f"_s_{s}") for s in STAGES}
        self.learning: set = set(LEARNING_STAGES)
        self._interrupted = False
        self._lock: Optional[asyncio.Lock] = None
        self.running = False

    # ------------------------------------------------------------------ extension points
    def register_stage(self, name: str, fn: Callable[[SleepContext], Awaitable[dict]], *, after: Optional[str] = None,
                       before: Optional[str] = None, learning: bool = False) -> None:
        """Add or replace a stage (Consolidator SPI)."""
        self.fns[name] = fn
        if name not in self.order:
            if after in self.order:
                self.order.insert(self.order.index(after) + 1, name)
            elif before in self.order:
                self.order.insert(self.order.index(before), name)
            else:
                self.order.append(name)
        if learning:
            self.learning.add(name)

    def interrupt(self) -> None:
        if self.running:
            self._interrupted = True

    def _enabled(self, stage: str) -> bool:
        if stage == "dream":
            return bool(self.cfg.get("dream", False))
        if stage == "train":
            return bool(self.cfg.get("train", True))
        configured = self.cfg.get("stages")
        return configured is None or stage in configured

    def night_key(self, now: float) -> str:
        return datetime.fromtimestamp(now - 12 * 3600, self.k.clock.tz).strftime("%Y-%m-%d")

    @staticmethod
    def plan_date(night: str) -> str:
        return (datetime.strptime(night, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    async def checkpoint(self, night: str) -> Optional[dict]:
        return await self.k.db.kv_get("grow.sleep", f"night:{night}")

    # ------------------------------------------------------------------ run
    async def run(self, now: float, stages: Optional[Sequence[str]] = None) -> dict:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._lock.locked():
            return {"busy": True}
        async with self._lock:
            self.running = True
            self._interrupted = False
            try:
                return await self._run(now, stages)
            finally:
                self.running = False
                self._interrupted = False

    async def _run(self, now: float, stages: Optional[Sequence[str]]) -> dict:
        night = self.night_key(now)
        db = self.k.db
        ck = await self.checkpoint(night)
        if not ck:
            last = await db.kv_get("grow.sleep", "last") or {}
            since = float(last.get("finished_at") or (now - 24 * 3600))
            since = max(since, now - 3 * 86400)
            ck = {"night": night, "started_at": now, "since": since, "until": now, "done": [], "failed": {},
                  "reports": {}, "state": {"promotions": 0}}
        explicit = stages is not None
        wanted = [s for s in self.order if (s in stages if explicit else self._enabled(s))]
        resumed = bool(ck["done"])
        paused = self.g.learning_paused()
        ctx = SleepContext(self, ck["state"], now=now, since=float(ck["since"]), until=float(ck["until"]), night=night,
                           date=self.plan_date(night), explicit=explicit)
        out = {"night": night, "date": ctx.date, "resumed": resumed, "interrupted": False, "next": None,
               "stages": [], "learning_paused": paused}
        for st in wanted:
            if not explicit and st in ck["done"]:
                continue
            if self._interrupted:
                out["interrupted"], out["next"] = True, st
                await self._emit(night, st, "interrupted", 0, {})
                break
            if st in self.learning and paused:
                out["stages"].append({"stage": st, "status": "skipped", "reason": "learning_paused"})
                await self._emit(night, st, "skipped", 0, {"reason": "learning_paused"})
                continue
            t0 = time.perf_counter()
            try:
                rep = await self.fns[st](ctx)
                status = "done"
            except SleepInterrupted:
                ms = int((time.perf_counter() - t0) * 1000)
                out["interrupted"], out["next"] = True, st
                out["stages"].append({"stage": st, "status": "interrupted", "ms": ms})
                await db.kv_set("grow.sleep", f"night:{night}", ck, ts=self.k.clock.now())
                await self._emit(night, st, "interrupted", ms, {})
                break
            except asyncio.CancelledError:
                await db.kv_set("grow.sleep", f"night:{night}", ck, ts=self.k.clock.now())
                raise
            except Exception as e:
                log.exception("sleep stage %s failed", st)
                rep, status = {"error": repr(e)[:300]}, "failed"
            ms = int((time.perf_counter() - t0) * 1000)
            rep = rep if isinstance(rep, dict) else {"result": rep}
            if status == "done":
                if st not in ck["done"]:
                    ck["done"].append(st)
                ck["failed"].pop(st, None)
            else:
                ck["failed"][st] = rep.get("error", "")
            ck["reports"][st] = _small(rep)
            out["stages"].append({"stage": st, "status": status, "ms": ms, "report": rep})
            await db.kv_set("grow.sleep", f"night:{night}", ck, ts=self.k.clock.now())
            await self._emit(night, st, status, ms, _small(rep))
        out["complete"] = all(s in ck["done"] for s in wanted if not (s in self.learning and paused))
        if out["complete"] and not out["interrupted"]:
            ck["finished_at"] = self.k.clock.now()
            await db.kv_set("grow.sleep", f"night:{night}", ck, ts=self.k.clock.now())
            await db.kv_set("grow.sleep", "last", {"night": night, "finished_at": ck["until"]}, ts=self.k.clock.now())
        out["promotions"] = ck["state"].get("promotions", 0)
        return out

    async def _emit(self, night: str, stage: str, status: str, ms: int, rep: dict) -> None:
        await self.k.bus.emit(EV.SLEEP_STAGE, {"night": night, "stage": stage, "status": status, "ms": ms,
                                               "report": rep}, source="growth")

    # ------------------------------------------------------------------ helpers
    def _me(self) -> str:
        try:
            return self.k.selfm.name()
        except Exception:
            return "我"

    def _legend(self, pks: Sequence[str]) -> tuple:
        codes: dict = {}
        for pk in pks:
            if pk and pk not in codes:
                codes[pk] = f"p{len(codes) + 1}"
        names = self.g._names
        legend = "\n".join(f"{c} = {names.get(pk) or pk.split(':')[-1]}" for pk, c in codes.items()) or "（无）"
        return codes, {c: pk for pk, c in codes.items()}, legend

    # ------------------------------------------------------------------ 1 replay
    async def _s_replay(self, c: SleepContext) -> dict:
        eps = await self.k.memory.episodes_between(c.since, c.until, limit=500)
        items = []
        for h in eps:
            aff = field(h, "affect") or {}
            v = float(aff.get("valence", 0) or 0) if isinstance(aff, dict) else 0.0
            a = float(aff.get("arousal", 0) or 0) if isinstance(aff, dict) else 0.0
            peak = bool(aff.get("peak")) if isinstance(aff, dict) else False
            imp = int(field(h, "importance", 3) or 3)
            items.append({"id": field(h, "id"), "content": str(field(h, "content", "")),
                          "importance": imp, "person_keys": list(field(h, "person_keys", []) or []),
                          "observed_at": float(field(h, "observed_at", 0) or 0),
                          "salience": round(imp / 10 + 0.5 * abs(v) * a + (0.5 if peak else 0.0), 3)})
        items.sort(key=lambda x: (-x["salience"], x["observed_at"]))
        intensity = sum(it["salience"] - it["importance"] / 10 for it in items)   # the affect part (|v|a, peaks)
        counts = c.state.get("counts") or self._night_counts(len(items), intensity)
        c.state["counts"] = counts
        pick = sorted(items[: counts["replay"]], key=lambda x: x["observed_at"])
        c.state["replay"] = pick
        return {"episodes": len(eps), "replayed": len(pick), "counts": counts}

    def _night_counts(self, n_episodes: int, intensity: float) -> dict:
        """Tonight's replay / reflection counts (see the module constants for the priors). Drawn once per night and
        kept in the checkpoint, so a resumed night does not redraw."""
        rng = chance.rng_of(self.k)
        f = math.sqrt(max(0.0, n_episodes) / DAY_EPISODES_REF) * math.sqrt(
            (1.0 + max(0.0, intensity)) / (1.0 + DAY_INTENSITY_REF))
        f = max(0.25, min(3.0, f))
        tex = getattr(self.k, "texture", None)
        cur = tex.multiplier("curiosity", 0.5) if tex is not None else 1.0
        cfg = self.cfg
        replay = 1 + chance.neg_binomial(rng, max(0.0, float(cfg["replay_mean"]) * f - 1.0), 4.0)
        questions = 1 + chance.poisson(rng, max(0.0, float(cfg["reflect_questions_mean"]) * math.sqrt(f) * cur - 1.0))
        insights = 1 + chance.poisson(rng, max(0.0, float(cfg["reflect_insights_mean"]) * math.sqrt(f) * cur - 1.0))
        return {"replay": int(min(replay, int(cfg["replay_ceiling"]))),
                "questions": int(min(questions, int(cfg["reflect_questions_ceiling"]))),
                "insights": int(min(insights, int(cfg["reflect_insights_ceiling"]))), "day_factor": round(f, 3)}

    # ------------------------------------------------------------------ 2 reflect (deep)
    async def _s_reflect(self, c: SleepContext) -> dict:
        items = c.state.get("replay") or []
        if not items:
            c.state["reflections"] = []
            return {"insights": 0, "skipped": "no episodes"}
        g, mem = self.g, self.k.memory
        codes, rev, legend = self._legend([pk for it in items for pk in it["person_keys"]])
        listing = "\n".join(
            f"{i + 1}. {truncate(it['content'], 160)}" +
            (f"（涉及 {','.join(codes[p] for p in it['person_keys'] if p in codes)}）" if it["person_keys"] else "")
            for i, it in enumerate(items))
        counts = c.state.get("counts") or self._night_counts(len(items), 0.0)
        c.state["counts"] = counts
        q = await self.k.llm.json("grow.reflect.questions",
                                  REFLECT_Q_USER.format(me=self._me(), items=listing, n=counts["questions"]),
                                  tier="deliberate", system=REFLECT_Q_SYSTEM, schema=REFLECT_Q_SCHEMA)
        questions = [str(x).strip() for x in (q.get("questions") or []) if str(x).strip()]
        questions = questions[: int(self.cfg["reflect_questions_ceiling"])] or ["今天发生的事里，哪些对我和朋友们最重要？"]
        c.check()
        evidence: list = [{"id": it["id"], "content": it["content"], "person_keys": it["person_keys"]} for it in items]
        seen = {e["id"] for e in evidence}
        for qq in questions:
            try:
                hits = await mem.retrieve(qq, limit=6, budget_tokens=600)
            except Exception:
                hits = []
            for h in hits:
                hid = field(h, "id")
                if hid and hid not in seen:
                    seen.add(hid)
                    evidence.append({"id": hid, "content": str(field(h, "content", "")),
                                     "person_keys": list(field(h, "person_keys", []) or [])})
        c.check()
        for e in evidence:
            for pk in e["person_keys"]:
                if pk not in codes:
                    codes[pk] = f"p{len(codes) + 1}"
                    rev[codes[pk]] = pk
        legend = "\n".join(f"{cd} = {g._names.get(pk) or pk.split(':')[-1]}" for pk, cd in codes.items()) or "（无）"
        ev_text = "\n".join(f"{i + 1}. {truncate(e['content'], 160)}" for i, e in enumerate(evidence))
        data = await self.k.llm.json("grow.reflect.insights",
                                     REFLECT_I_USER.format(me=self._me(), legend=legend,
                                                           questions="\n".join(f"- {x}" for x in questions),
                                                           evidence=ev_text, n=counts["insights"]),
                                     tier="deep", system=REFLECT_I_SYSTEM, schema=REFLECT_I_SCHEMA)
        reflections, self_notes, lessons = [], [], 0
        for ins in (data.get("insights") or [])[: int(self.cfg["reflect_insights_ceiling"])]:
            if not isinstance(ins, dict):
                continue
            text = str(ins.get("text") or "").strip()
            cites = []
            for x in ins.get("cites") or []:
                try:
                    n = int(x)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(evidence) and evidence[n - 1]["id"]:
                    cites.append(evidence[n - 1]["id"])
            if not text or not cites:   # an insight without support is not kept
                continue
            about = str(ins.get("about") or "").strip().lower()
            pk = rev.get(about)
            conf = clamp(ins.get("confidence", 0.5))
            try:
                imp = max(1, min(10, int(ins.get("importance", 5))))
            except (TypeError, ValueError):
                imp = 5
            mid = await mem.note(kind="opinion", content=truncate(text, 300), importance=imp, source="reflected",
                                 evidence=cites, derived_from=cites, person_keys=[pk] if pk else [])
            reflections.append({"id": mid, "text": text, "about": pk or ("me" if about == "me" else ""),
                                "confidence": conf, "cites": cites})
            if about == "me":
                self_notes.append(text)
            lesson = ins.get("lesson")
            if isinstance(lesson, dict) and str(lesson.get("text") or "").strip():
                pol = "harmful" if str(lesson.get("polarity") or "").lower() == "harmful" else "helpful"
                tag = _slug(lesson.get("tag") or text)
                kind = str(lesson.get("kind") or "").lower()
                kind = kind if kind in ("strategy", "habit", "pitfall", "preference") else (
                    "pitfall" if pol == "harmful" else "strategy")
                scope = f"person:{pk}" if str(lesson.get("scope") or "") == "person" and pk else "global"
                ev = await g.evidence.record(level="S4", polarity=pol, about=tag, context_refs=cites, subject=pk,
                                             confidence=conf, note=str(lesson["text"])[:200])
                art, _ = await g.artifacts.create("playbook", truncate(str(lesson["text"]).strip(), 80), kind=kind,
                                                  scope=scope, tag=tag, provenance=[ev.id], source="reflect",
                                                  author="self", status="candidate")
                await g.evidence.attach([ev.id], art.id)
                lessons += 1
        if self_notes and self.k.selfm is not None:
            await self.k.selfm.add_self_notes(self_notes, source_event="")
        c.state["reflections"] = reflections
        return {"questions": len(questions), "evidence": len(evidence), "insights": len(reflections),
                "self_notes": len(self_notes), "lessons": lessons}

    # ------------------------------------------------------------------ 3/4/7/10 memory stages
    async def _memory_stage(self, stage: str, c: SleepContext) -> dict:
        return await self.k.memory.consolidate(stage, c.as_dict())

    async def _s_extract(self, c: SleepContext) -> dict:
        return await self._memory_stage("extract", c)

    async def _s_reconcile(self, c: SleepContext) -> dict:
        return await self._memory_stage("reconcile", c)

    async def _s_forget(self, c: SleepContext) -> dict:
        return await self._memory_stage("forget", c)

    async def _s_precompute(self, c: SleepContext) -> dict:
        return await self._memory_stage("precompute", c)

    # ------------------------------------------------------------------ 5 learn
    async def _s_learn(self, c: SleepContext) -> dict:
        g = self.g
        rep = {"usage_flushed": await g.artifacts.flush_usage(), "learner_runs": await g.learner.flush_all(force=False),
               "groups": 0, "candidates": 0, "attached": 0, "quarantined": 0, "rejected": 0, "merged": 0, "retired": 0}
        evs = await g.evidence.unprocessed()
        groups: dict = {}
        for e in evs:
            if not e.entry_id and e.polarity != "neutral":
                groups.setdefault(e.about, []).append(e)
        fresh = []
        for tag, es in groups.items():
            subjects = {e.subject for e in es}
            scope = f"person:{next(iter(subjects))}" if len(subjects) == 1 and None not in subjects else "global"
            existing = await g.artifacts.find_tag(tag)
            if existing is not None:
                await g.evidence.attach([e.id for e in es], existing.id)
                h = sum(1 for e in es if e.level != "S4" and e.polarity == "helpful")
                b = sum(1 for e in es if e.level != "S4" and e.polarity == "harmful")
                await g.artifacts.add_counts(existing.id, helpful=h, harmful=b, evidence_id=es[-1].id)
                rep["attached"] += len(es)
            else:
                fresh.append((tag, scope, es))
        if fresh:
            blocks = []
            for n, (tag, scope, es) in enumerate(fresh, start=1):
                h = sum(1 for e in es if e.polarity == "helpful")
                b = sum(1 for e in es if e.polarity == "harmful")
                lv = {}
                for e in es:
                    lv[e.level] = lv.get(e.level, 0) + 1
                notes = "；".join(truncate(e.note, 60) for e in es if e.note)[:300]
                blocks.append(f"{n}. 行为 {tag}（范围 {'只针对某个人' if scope != 'global' else '普遍'}）：正面 {h} 次，"
                              f"负面 {b} 次；证据等级 {', '.join(f'{k}×{v}' for k, v in sorted(lv.items()))}；备注：{notes or '无'}")
            rep["groups"] = len(fresh)
            data = await self.k.llm.json("grow.learn", LEARN_USER.format(groups="\n".join(blocks)), tier="deliberate",
                                         system=LEARN_SYSTEM, schema=LEARN_SCHEMA)
            for ent in data.get("entries") or []:
                if not isinstance(ent, dict) or ent.get("skip") is True:
                    continue
                try:
                    n = int(ent.get("group"))
                except (TypeError, ValueError):
                    continue
                text = str(ent.get("text") or "").strip()
                if not (1 <= n <= len(fresh)) or not text:
                    continue
                tag, scope, es = fresh[n - 1]
                kind = str(ent.get("kind") or "strategy").lower()
                kind = kind if kind in ("strategy", "habit", "pitfall", "preference") else "strategy"
                art, created = await g.artifacts.create("playbook", truncate(text, 80), kind=kind, scope=scope, tag=tag,
                                                        provenance=[e.id for e in es], source="learn", author="self",
                                                        status="candidate")
                await g.evidence.attach([e.id for e in es], art.id)
                h = sum(1 for e in es if e.level != "S4" and e.polarity == "helpful")
                b = sum(1 for e in es if e.level != "S4" and e.polarity == "harmful")
                await g.artifacts.add_counts(art.id, helpful=h, harmful=b, evidence_id=es[-1].id)
                rep["candidates"] += int(created)
        await g.evidence.mark_processed([e.id for e in evs])
        c.check()
        for a in await g.artifacts.list(status="candidate"):
            ok, why = await g.static_check(a)
            if ok:
                await g.artifacts.set_status(a.id, "quarantined", reason="static check passed")
                rep["quarantined"] += 1
            else:
                await g.artifacts.set_status(a.id, "retired", reason=f"static check failed: {why}")
                rep["rejected"] += 1
        c.check()
        rep["merged"] = await g.merge_duplicates()
        rep["retired"] = await g.retire_stale(c.now)
        return rep

    # ------------------------------------------------------------------ 6 verify
    async def _s_verify(self, c: SleepContext) -> dict:
        g = self.g
        cap = int(g.cfg["max_promotions_per_night"])
        rep = {"checked": 0, "eligible": 0, "promoted": 0, "retired": 0, "deferred": 0, "capped": 0}
        quarantined = await g.artifacts.list(status="quarantined")
        eligible = []
        for a in quarantined:
            gate = await g.promotion_gate(a)
            rep["checked"] += 1
            if gate.ok:
                eligible.append((a, gate))
        eligible.sort(key=lambda x: (-x[1].score, -x[0].helpful, x[0].created_at))
        rep["eligible"] = len(eligible)
        batch_n = int(g.cfg["verify"]["batch"])
        for i in range(0, len(eligible), batch_n):
            c.check()
            if c.state.get("promotions", 0) >= cap:
                rep["capped"] += len(eligible) - i
                break
            chunk = eligible[i:i + batch_n]
            reviews = await g.safety_review([a for a, _ in chunk])
            for (a, gate), rv in zip(chunk, reviews):
                if rv is None:
                    rep["deferred"] += 1           # review unavailable: fail closed, stay quarantined
                    continue
                if not rv.get("ok"):
                    await g.artifacts.set_status(a.id, "retired", reason=f"verify failed: {rv.get('reason', '')}",
                                                 eval_score=0.0)
                    rep["retired"] += 1
                    continue
                hook = await g.run_verify_hooks(a)
                if not hook["pass"]:
                    rep["deferred"] += 1
                    continue
                if c.state.get("promotions", 0) >= cap:
                    rep["capped"] += 1
                    continue
                score = round((gate.score + hook["score"]) / 2, 3) if hook["n"] else gate.score
                await g.artifacts.set_status(a.id, "active", reason=f"{gate.reason}; review ok", eval_score=score)
                c.state["promotions"] = c.state.get("promotions", 0) + 1
                rep["promoted"] += 1
                await g.artifacts.enforce_caps(a.type, a.scope)
        return rep

    # ------------------------------------------------------------------ 8 self
    async def _s_self(self, c: SleepContext) -> dict:
        if self.k.selfm is None:
            return {"skipped": "no self model"}
        res = await self.k.selfm.sleep_update({
            "night": c.night, "reflections": [r.get("text") for r in c.state.get("reflections") or []],
            "events": [{"content": it["content"], "importance": it["importance"]} for it in c.state.get("replay") or []],
            "lessons": [a.text for a in await self.g.artifacts.list(type_="playbook", status="quarantined")][:10]})
        return res if isinstance(res, dict) else {"result": str(res)[:200]}

    # ------------------------------------------------------------------ 9 plan (+ curriculum)
    def _rhythm_text(self, now: Optional[float] = None) -> str:
        """Habitual rhythm plus how tomorrow actually looks (the sampled wake / bed times, when the affect system has
        them): the plan is placed in the hours I will really be awake."""
        r = self.g.rhythm or {}
        parts = []
        if r.get("wake") or r.get("sleep"):
            parts.append("平时" + "、".join(x for x in (f"{r['wake']} 起床" if r.get("wake") else "",
                                                       f"{r['sleep']} 睡觉" if r.get("sleep") else "") if x))
        span = getattr(self.k.affect, "waking_span", None) if self.k.affect is not None else None
        if callable(span):
            try:
                w, b = span(self.k.clock.now() if now is None else now)
                fmt = lambda t: datetime.fromtimestamp(t, self.k.clock.tz).strftime("%H:%M")   # noqa: E731
                parts.append(f"这一天大概 {fmt(w)} 醒、{fmt(b)} 睡")
            except Exception:
                pass
        if r.get("active_hours"):
            parts.append("习惯的活跃时段 " + "、".join(r["active_hours"]))
        return "，".join(parts) or "（未知）"

    async def _s_plan(self, c: SleepContext) -> dict:
        mot = self.k.motivation
        if mot is None:
            return {"skipped": "no motivation"}
        tz = self.k.clock.tz
        d = datetime.strptime(c.date, "%Y-%m-%d")
        start = datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
        intents = []
        for it in await mot.active_intentions(limit=20):
            due = field(it, "due_at")
            due_s = datetime.fromtimestamp(float(due), tz).strftime("%m-%d %H:%M") if due else ""
            intents.append(f"- [{field(it, 'level', '')}/{field(it, 'kind', '')}] {field(it, 'goal', '')}"
                           + (f"（截止 {due_s}）" if due_s else "") + ("（答应别人的）" if field(it, "is_commitment") else ""))
        loops = [f"- {x}" for x in await mot.open_loops(limit=7) if isinstance(x, str)]
        try:
            needs = mot.needs() or {}
        except Exception:
            needs = {}
        need_lines = [f"- {n}: {float(v.get('level', 0)):.2f}/{float(v.get('setpoint', 0)):.2f}"
                      for n, v in needs.items() if isinstance(v, dict)]
        upcoming = []
        up_fn = getattr(self.k.memory, "upcoming", None)
        pks: list = []
        if callable(up_fn):
            try:
                for h in await up_fn(start, start + 86400, limit=10):
                    upcoming.append(f"- {field(h, 'content', '')}")
                    pks += list(field(h, "person_keys", []) or [])
            except Exception:
                pass
        refl = [f"- {r.get('text')}" for r in (c.state.get("reflections") or [])][:6]
        codes, rev, legend = self._legend(pks)
        prompt = PLAN_USER.format(me=self._me(), date=c.date, weekday="周" + "一二三四五六日"[d.weekday()],
                                  rhythm=self._rhythm_text(c.now), intentions="\n".join(intents) or "（无）",
                                  loops="\n".join(loops) or "（无）", needs="\n".join(need_lines) or "（未知）",
                                  upcoming="\n".join(upcoming) or "（无）", reflections="\n".join(refl) or "（无）",
                                  legend=legend)
        try:
            data = await self.k.llm.json("grow.plan", prompt, tier="deliberate", system=PLAN_SYSTEM, schema=PLAN_SCHEMA)
        except Exception as e:
            log.warning("grow.plan failed, using fallback plan: %s", e)
            data = {"items": []}
        items = self._validate_plan(data.get("items") or [], rev)
        await mot.set_day_plan(c.date, items)
        c.state["plan"] = items
        rep = {"date": c.date, "items": len(items)}
        c.check()
        if self.g.learning_paused():
            rep["agenda"] = 0
            return rep
        agenda = await build_agenda(self.g, c)
        ids = await apply_agenda(self.g, agenda)
        await self.k.db.kv_set("grow.curriculum", c.date, agenda, ts=self.k.clock.now())
        c.state["agenda"] = agenda
        rep["agenda"] = len(ids)
        return rep

    def _validate_plan(self, raw: list, rev: dict) -> list:
        items = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            goal = str(it.get("goal") or "").strip()
            if not goal:
                continue
            kind = str(it.get("kind") or "general").lower()
            kind = kind if kind in PLAN_KINDS else "general"
            out = {"goal": truncate(goal, 60), "kind": kind, "priority": round(clamp(it.get("priority", 0.5)), 3),
                   "autonomous": it.get("autonomous") is True or kind in ("free", "read", "learn", "create", "rest")}
            w = str(it.get("window") or "").strip()
            if _WIN.match(w):
                out["window"] = w.replace("~", "-").replace("–", "-").replace(" ", "")
            due = str(it.get("due") or "").strip()
            if _HHMM.match(due):
                out["due"] = due
            tp = str(it.get("target_person") or "").strip().lower()
            if tp in rev:
                out["target_person"] = rev[tp]
            items.append(out)
        items = items[:8]
        if not any(i["autonomous"] and i["kind"] != "free" for i in items):
            items.append({"goal": "读点自己感兴趣的东西", "kind": "read", "priority": 0.4, "autonomous": True})
        if not any(i["kind"] == "free" for i in items):
            items.append({"goal": "空档：随便做点什么或者发会儿呆", "kind": "free", "priority": 0.2, "autonomous": True})
        return items

    # ------------------------------------------------------------------ 11 dream (optional)
    async def _s_dream(self, c: SleepContext) -> dict:
        eps = await self.k.memory.episodes_between(c.now - 90 * 86400, c.now, limit=400)
        if len(eps) < 2:
            return {"skipped": "not enough memories"}
        rnd = random.Random(c.night)
        by_day: dict = {}
        for h in eps:
            day = datetime.fromtimestamp(float(field(h, "observed_at", 0) or 0), self.k.clock.tz).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(h)
        days = list(by_day)
        rnd.shuffle(days)
        pick = [rnd.choice(by_day[d]) for d in days[:3]]
        if len(pick) < 2:
            pick = rnd.sample(eps, min(3, len(eps)))
        listing = "\n".join(f"- {truncate(str(field(h, 'content', '')), 120)}" for h in pick)
        data = await self.k.llm.json("grow.dream", DREAM_USER.format(items=listing), tier="deliberate",
                                     system=DREAM_SYSTEM, schema=DREAM_SCHEMA)
        dream = str(data.get("dream") or "").strip()
        if not dream:
            return {"dreamed": False}
        # derived_from: the dream keeps its sources' audience (INV-34) - a dream built from something told to me
        # one-to-one must not surface in a shared space
        mid = await self.k.memory.note(kind="episodic", content=truncate("（梦）" + dream, 300), importance=2,
                                       source="dreamed", trust=Trust.SYSTEM,
                                       derived_from=[str(field(h, "id")) for h in pick if field(h, "id")])
        seed = str(data.get("seed") or "").strip()
        if seed:
            seeds = (await self.k.db.kv_get("grow", "dream_seeds", []) or [])[-19:] + [seed]
            await self.k.db.kv_set("grow", "dream_seeds", seeds, ts=self.k.clock.now())
        return {"dreamed": True, "memory": mid}

    # ------------------------------------------------------------------ 12 train (L3 stub)
    async def _s_train(self, c: SleepContext) -> dict:
        """Export JSONL samples of my own S1/S2-verified utterances (never other people's text) to
        ``<data_dir>/train/<night>.jsonl``. No training happens here."""
        interval = float(self.cfg["train_interval_days"]) * 86400
        last = await self.k.db.kv_get("grow.sleep", "last_train", None)
        if not c.explicit and last and c.now - float(last) < interval:
            return {"skipped": "not due"}
        evs = await self.g.evidence.since(c.now - max(interval, 86400), levels=("S1", "S2"))
        samples = []
        for e in evs:
            if e.trust is not None and int(e.trust) <= int(Trust.UNTRUSTED):
                continue
            for ref in e.context_refs:
                ev = await self.k.bus.get(ref) if isinstance(ref, str) else None
                if ev is None or not (ev.type.startswith("outcome.sent") or ev.type.startswith("act.send")):
                    continue
                p = ev.payload or {}
                if p.get("redacted"):
                    continue
                text = p.get("text") or "\n".join(str(s) for s in (p.get("segments") or []) if s)
                if not str(text).strip():
                    continue
                samples.append({"messages": [{"role": "user", "content": f"情境：{e.about}" + (f"（{e.note[:80]}）" if e.note else "")},
                                             {"role": "assistant", "content": str(text)}],
                                "label": "positive" if e.polarity == "helpful" else "negative",
                                "meta": {"evidence": e.id, "level": e.level, "event": ev.id, "night": c.night}})
        ddir = getattr(self.k, "data_dir", None) or self.k.cfg.get("data_dir") or "."
        path = os.path.join(ddir, "train", f"{c.night}.jsonl")

        def _write():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for s in samples:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")

        if samples:
            await asyncio.get_running_loop().run_in_executor(None, _write)
        await self.k.db.kv_set("grow.sleep", "last_train", c.now, ts=self.k.clock.now())
        return {"samples": len(samples), "path": path if samples else None}
