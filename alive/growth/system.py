"""GrowthSystem implementation (docs/06): evidence, L2 artifacts (playbook / expressions / jargon), learners,
skills, sleep orchestration, curriculum.

Hot-path methods (``playbook_for``, ``expressions_for``, ``format_playbook``) read an in-memory cache of
active artifacts (no DB I/O). Learning is batched: learners run per thread in the background, and all
promotions happen at night through the gated lifecycle.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import os
import re
import time
from typing import Any, Callable, Optional, Sequence

import numpy as np

from alive.core.types import EV, ChatLine, Trust
from alive.growth import schema as _schema  # noqa: F401
from alive.growth.artifacts import TYPES, Artifact, ArtifactStore, scope_class
from alive.growth.evidence import Gate, EvidenceStore, evidence_gate
from alive.growth.learners import ExpressionLearner
from alive.growth.prompts import (MERGE_SCHEMA, MERGE_SYSTEM, MERGE_USER, VERIFY_SCHEMA, VERIFY_SYSTEM,
                                  VERIFY_USER)
from alive.growth.skills import SkillRegistry
from alive.growth.sleep import SleepPipeline
from alive.growth.util import field, norm, truncate
from alive.interfaces import GrowthSystem
from alive.runtime import chance

log = logging.getLogger("alive.growth")

DEFAULTS: dict = {
    "caps": {"global": 60, "person": 15, "channel": 30, "situation": 30, "default": 30},
    "max_promotions_per_night": 5,
    "s3": {"min": 3, "min_subjects": 2, "min_days": 2},
    "auto_demote": {"min_harmful": 2},
    "expression_min_seen": 2,
    "quarantine_ttl_days": 14,
    "retire_unused_days": 60,
    "merge_threshold": 0.8,          # candidate pairs (cosine) -> LLM confirms "same meaning"
    "auto_merge_threshold": 0.985,   # practically identical -> merged without asking
    "learning": {"enabled": True, "group_lines": 25, "private_lines": 12, "min_lines": 5, "idle_s": 1200,
                 "max_batch": 60, "concurrency": 3, "blacklist": {"persons": [], "threads": [], "words": []}},
    "prompt": {"global_slots": 4, "min_cue_sim": 0.1},
    # nightly counts are draws around these means (growth/sleep.py); the ceilings are technical limits
    "sleep": {"stages": None, "dream": False, "train": True, "train_interval_days": 7,
              "replay_mean": 20, "replay_ceiling": 60, "reflect_questions_mean": 3, "reflect_questions_ceiling": 6,
              "reflect_insights_mean": 5, "reflect_insights_ceiling": 10},
    "curriculum": {"max_items": 3, "min_score": 0.01},
    "verify": {"safety": True, "batch": 8},
}

_URL = re.compile(r"https?://|www\.", re.I)
_INJECTION = ("忽略之前", "忽略以上", "无视之前", "ignore previous", "ignore all", "system prompt", "系统提示", "<<",
              "you are now", "越狱", "jailbreak")


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _hash(pk: str) -> str:
    return hashlib.sha256(pk.encode("utf-8")).hexdigest()[:16]


class Growth(GrowthSystem):
    def __init__(self, kernel):
        super().__init__(kernel)
        self.cfg: dict = _deep_merge(DEFAULTS, kernel.cfg.section("growth").data)
        self.artifacts = ArtifactStore(kernel, self.cfg)
        self.evidence = EvidenceStore(kernel)
        self.learner = ExpressionLearner(self)
        self.skills_registry = SkillRegistry(kernel)
        self.pipeline = SleepPipeline(self)
        self._verify_hooks: list = []
        self._names: dict = {}
        self._active = 0
        self._tasks: set = set()
        self.rhythm: dict = {}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.artifacts.load()
        try:
            await self.skills_registry.load()
        except Exception:
            log.exception("loading skills failed")
        self.rhythm = await asyncio.get_running_loop().run_in_executor(None, self._read_rhythm)
        self._names.update(await self.k.db.kv_get("grow", "names", {}) or {})
        if hasattr(self.k.clock, "add_settle_hook"):
            self.k.clock.add_settle_hook(self._settle_hook)
        self.k.bus.subscribe(EV.MEMORY_FORGET, self._on_forget, name="growth.forget")

    async def stop(self) -> None:
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        try:
            await self.artifacts.flush_usage()
            await self.k.db.kv_set("grow", "names", dict(list(self._names.items())[-5000:]), ts=self.k.clock.now())
        except Exception:
            log.exception("growth stop failed")

    def status(self) -> dict:
        return {"active_artifacts": len(self.artifacts._active), "skills": len(self.skills_registry._skills),
                "learner_buffers": sum(len(b.lines) for b in self.learner._bufs.values()),
                "sleep_running": self.pipeline.running, "active_jobs": self._active}

    def _read_rhythm(self) -> dict:
        import yaml
        pdir = getattr(self.k, "persona_dir", None) or self.k.cfg.get("persona_dir")
        p = os.path.join(pdir, "rhythm.yaml") if pdir else None
        if not p or not os.path.isfile(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    async def _settle_hook(self) -> bool:
        if self._active <= 0:
            return False
        deadline = time.monotonic() + 10.0
        while self._active > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        return True

    def _spawn(self, coro_fn, name: str) -> None:
        h = self.k.scheduler.spawn(coro_fn, name)
        self._tasks.add(h.task)
        h.task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        for _ in range(100):
            pending = [t for t in self._tasks if not t.done()]
            if not pending and self._active <= 0:
                return
            if pending:
                await asyncio.wait(pending, timeout=10.0)
            else:
                await asyncio.sleep(0.001)

    def learning_paused(self) -> bool:
        gov = getattr(self.k, "gov", None)
        if gov is None:
            return False
        try:
            return bool(gov.learning_paused or gov.frozen)
        except Exception:
            return False

    # ------------------------------------------------------------------ evidence
    async def record_evidence(self, *, level: str, polarity: str, about: str, context_refs: Sequence[str] = (),
                              subject: Optional[str] = None, confidence: float = 0.5, entry_id: Optional[str] = None,
                              note: str = "", trust: Optional[int] = None) -> str:
        """See interface. Extra kwarg ``trust``: the trust level of the evidence source (UNTRUSTED -> S4)."""
        ev = await self.evidence.record(level=level, polarity=polarity, about=about, context_refs=context_refs,
                                        subject=subject, confidence=confidence, entry_id=entry_id, note=note,
                                        trust=trust)
        if entry_id and ev.level != "S4" and ev.polarity in ("helpful", "harmful"):
            await self.artifacts.add_counts(entry_id, helpful=int(ev.polarity == "helpful"),
                                            harmful=int(ev.polarity == "harmful"), evidence_id=ev.id)
        return ev.id

    # ------------------------------------------------------------------ retrieval for prompts
    async def _cue_vec(self, cue: str) -> Optional[np.ndarray]:
        if not (cue or "").strip():
            return None
        return (await self.k.embedder.embed([cue]))[0]

    async def playbook_for(self, cue: str, scopes: Sequence[str], limit: int = 8) -> list:
        scopes = list(dict.fromkeys(scopes or ["global"]))
        slots = min(limit, int(self.cfg["prompt"]["global_slots"])) if "global" in scopes else 0
        glob = self.artifacts.global_selection(slots) if slots else []
        others = self.artifacts.active("playbook", [s for s in scopes if s != "global"])
        if others:
            qv = await self._cue_vec(cue)

            def rank(a: Artifact) -> float:
                v = self.artifacts.vec(a.id)
                sim = float(v @ qv) if (qv is not None and v is not None) else 0.0
                return sim + 0.05 * math.tanh(a.net / 3.0)
            others.sort(key=lambda a: (-rank(a), a.id))
        out = glob + others[: max(0, limit - len(glob))]
        self.artifacts.mark_used(a.id for a in out)
        return out

    async def expressions_for(self, cue: str, scopes: Sequence[str], limit: int = 5) -> list:
        scopes = list(dict.fromkeys(list(scopes or []) + ["global"]))
        act = self.artifacts.active("expression", scopes)
        if not act:
            return []
        nc = norm(cue)
        jar = [a for a in act if a.kind == "jargon" and (a.jargon or {}).get("term") and norm(a.jargon["term"]) in nc]
        style = [a for a in act if a.kind != "jargon"]
        qv = await self._cue_vec(cue)
        thr = float(self.cfg["prompt"]["min_cue_sim"])
        scores, by_id = {}, {}
        for a in sorted(style, key=lambda x: x.id):
            v = self.artifacts.vec(a.id)
            sim = float(v @ qv) if (qv is not None and v is not None) else 0.0
            if qv is None or sim >= thr:
                scores[a.id], by_id[a.id] = sim + 0.03 * math.tanh(a.net / 3.0), a
        # which phrases come to mind is a Plackett-Luce draw, not a fixed top-k (the same cue does not always yield
        # the same phrases). Temperature 0.05 x today's playfulness (texture multiplier 0.5..1.5): a cosine gap of
        # 0.1 between two phrases is ~7:1 on a typical day, ~3.5:1 on a playful one, ~50:1 on a flat one.
        tex = getattr(self.k, "texture", None)
        temp = 0.05 * (tex.multiplier("playfulness", 1.0) if tex is not None else 1.0)
        n_style = max(0, limit - len(jar[:limit]))
        picked = chance.gumbel_topk(chance.rng_of(self.k), scores, n_style, temp) if n_style and scores else []
        out = jar[:limit] + [by_id[i] for i in picked]
        self.artifacts.mark_used(a.id for a in out)
        return out

    def format_playbook(self, entries: Sequence[Any], expressions: Sequence[Any]) -> str:
        glob = sorted([e for e in entries if field(e, "scope", "global") == "global"], key=lambda e: str(field(e, "id", "")))
        scoped = [e for e in entries if field(e, "scope", "global") != "global"]
        style = [x for x in expressions if field(x, "kind", "style") != "jargon"]
        jargon = [x for x in expressions if field(x, "kind", "style") == "jargon"]
        parts = []
        if glob:
            parts.append("【我的做事习惯】\n" + "\n".join(f"- {field(e, 'text', '')}" for e in glob))
        if scoped:
            parts.append("【和这里/这个人相处的心得】\n" + "\n".join(f"- {field(e, 'text', '')}" for e in scoped))
        if style:
            lines = []
            for x in style:
                ex = (field(x, "examples", []) or [])
                lines.append(f"- 当「{field(x, 'situation', '')}」时，可以「{field(x, 'text', '')}」"
                             + (f"（例：{truncate(ex[-1], 30)}）" if ex else ""))
            parts.append("【可以用的说法】\n" + "\n".join(lines))
        if jargon:
            lines = []
            for x in jargon:
                j = field(x, "jargon", {}) or {}
                lines.append(f"- {j.get('term', '')}：{j.get('meaning') or field(x, 'text', '')}")
            parts.append("【这里的黑话】\n" + "\n".join(lines))
        return "\n".join(parts)

    # ------------------------------------------------------------------ learners
    async def observe_messages(self, thread_id: str, chat_type: str, lines: Sequence[ChatLine]) -> None:
        if not self.cfg["learning"].get("enabled", True) or self.learning_paused():
            return
        await self.learner.observe(thread_id, chat_type, lines)

    # ------------------------------------------------------------------ sleep
    async def run_sleep(self, now: float, *, stages: Optional[Sequence[str]] = None) -> dict:
        return await self.pipeline.run(now, stages)

    def interrupt_sleep(self) -> None:
        """Wake-up: stop the running sleep pipeline at the next checkpoint (resumable)."""
        self.pipeline.interrupt()

    def register_stage(self, name: str, fn, *, after: Optional[str] = None, before: Optional[str] = None,
                       learning: bool = False) -> None:
        self.pipeline.register_stage(name, fn, after=after, before=before, learning=learning)

    def register_verify_hook(self, fn: Callable[[Artifact], Any]) -> None:
        """Extra verifier (e.g. the evaluation harness's shadow/regression check). ``fn(artifact)`` may be
        async and returns bool, {"pass": bool, "score": float, "reason": str} or None (abstain)."""
        self._verify_hooks.append(fn)

    # ------------------------------------------------------------------ skills
    async def skills(self) -> list:
        return self.skills_registry.list()

    def skill_body(self, name: str) -> Optional[str]:
        return self.skills_registry.body(name)

    async def reload_skills(self) -> int:
        return await self.skills_registry.load()

    async def verify_skill(self, name: str, passed: bool, score: Optional[float] = None, note: str = "") -> dict:
        return await self.skills_registry.verify(name, passed, score, note)

    # ------------------------------------------------------------------ artifact management API
    async def propose_entry(self, text: str, *, scope: str = "global", kind: str = "strategy", situation: str = "",
                            tag: Optional[str] = None, evidence: Sequence[str] = (), source: str = "external",
                            author: str = "self") -> Artifact:
        """Add a playbook candidate (reflection, owner instruction, mind). It is only used after the night
        lifecycle (static check -> quarantine -> verify)."""
        a, _ = await self.artifacts.create("playbook", text, kind=kind, situation=situation, scope=scope, tag=tag,
                                           provenance=evidence, source=source, author=author, status="candidate")
        if evidence:
            await self.evidence.attach(list(evidence), a.id)
        return a

    async def get_entry(self, aid: str) -> Optional[Artifact]:
        return await self.artifacts.get(aid)

    async def entry_history(self, aid: str) -> list:
        return await self.artifacts.history(aid)

    async def rollback(self, aid: str, vid: str) -> Optional[Artifact]:
        return await self.artifacts.rollback(aid, vid)

    async def edit_entry(self, aid: str, text: str, **kw) -> Optional[Artifact]:
        return await self.artifacts.edit(aid, text, **kw)

    async def merge_entries(self, keep_id: str, drop_id: str, text: Optional[str] = None) -> Optional[Artifact]:
        return await self.artifacts.merge(keep_id, drop_id, text=text)

    # ------------------------------------------------------------------ gates
    async def static_check(self, a: Artifact) -> tuple:
        """Format + static safety checks for candidate -> quarantined."""
        text, sit = (a.text or "").strip(), (a.situation or "").strip()
        term = (a.jargon or {}).get("term", "") if a.jargon else ""
        if not text:
            return False, "empty"
        if a.type == "playbook" and len(text) > 80:
            return False, "too long"
        if a.type == "expression" and (len(text) > 40 or len(sit) > 40):
            return False, "too long"
        joined = f"{sit} {text} {term}"
        if _URL.search(joined):
            return False, "contains url"
        low = joined.lower()
        for pat in _INJECTION:
            if pat in low:
                return False, f"injection pattern {pat}"
        for w in (self.cfg["learning"].get("blacklist") or {}).get("words") or []:
            if w and norm(w) in norm(joined):
                return False, f"blacklisted word {w}"
        if a.type == "expression":
            if "@" in joined:
                return False, "mentions someone"
            names = {n for n in self._names.values() if n and len(n) >= 2}
            try:
                names.add(self.k.selfm.name())
            except Exception:
                pass
            if any(n in f"{sit} {text}" for n in names):
                return False, "contains a person name"
        if a.provenance:
            rows = []
            for eid in a.provenance:
                if isinstance(eid, str) and eid.startswith("ev"):
                    r = await self.k.db.fetchone("SELECT trust, requested_level FROM grow_evidence WHERE id=?", (eid,))
                    if r is not None:
                        rows.append(r)
            if rows and all((r["trust"] is not None and int(r["trust"]) <= int(Trust.UNTRUSTED)) for r in rows):
                return False, "INV-14: only untrusted evidence"
        return True, ""

    async def promotion_gate(self, a: Artifact) -> Gate:
        rows = await self.evidence.for_entry(a.id)
        if a.type == "playbook":
            return evidence_gate(rows, self.cfg["s3"])
        h12 = sum(1 for r in rows if r.level in ("S1", "S2") and r.polarity == "helpful")
        b12 = sum(1 for r in rows if r.level in ("S1", "S2") and r.polarity == "harmful")
        if b12 and b12 >= h12:
            return Gate(False, "explicit negative feedback", -0.5)
        if h12:
            return Gate(True, "S1/S2 helpful", 0.6)
        need = int(self.cfg["expression_min_seen"])
        if a.seen_count >= need:
            return Gate(True, f"observed {a.seen_count}x", round(min(1.0, a.seen_count / (2.0 * need)), 3))
        return Gate(False, f"observed {a.seen_count}x < {need}", 0.0)

    async def safety_review(self, arts: Sequence[Artifact]) -> list:
        """Fast-tier identity/constitution/manipulation review. Returns aligned list of
        {"ok", "reason", ...} or None where no verdict was obtained (fail closed)."""
        if not arts:
            return []
        if not self.cfg["verify"].get("safety", True):
            return [{"ok": True, "reason": "safety review disabled"} for _ in arts]
        lines = []
        for n, a in enumerate(arts, start=1):
            if a.type == "playbook":
                lines.append(f"{n}. [行为手册] {a.text}")
            elif a.kind == "jargon":
                j = a.jargon or {}
                lines.append(f"{n}. [黑话] {j.get('term', '')}：{j.get('meaning', a.text)}")
            else:
                lines.append(f"{n}. [表达方式] 当「{a.situation}」时，可以「{a.text}」")
        try:
            constitution = self.k.gov.constitution_text() if self.k.gov is not None else ""
        except Exception:
            constitution = ""
        try:
            identity = self.k.selfm.identity_digest() if self.k.selfm is not None else ""
        except Exception:
            identity = ""
        try:
            data = await self.k.llm.json("grow.verify", VERIFY_USER.format(constitution=constitution or "（无）",
                                                                           identity=identity or "（无）",
                                                                           items="\n".join(lines)),
                                         tier="fast", system=VERIFY_SYSTEM, schema=VERIFY_SCHEMA)
        except Exception as e:
            log.warning("grow.verify failed: %s", e)
            return [None] * len(arts)
        res: dict = {}
        for r in data.get("results") or []:
            if not isinstance(r, dict):
                continue
            try:
                n = int(r.get("item"))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(arts) and n not in res:
                res[n] = r
        out = []
        for n, a in enumerate(arts, start=1):
            r = res.get(n)
            if r is None:
                out.append(None)
                continue
            ok = r.get("ok") is True and r.get("conflict") is not True and r.get("manipulative") is not True
            if a.type == "expression" and r.get("generic") is False:
                ok = False
            out.append({"ok": ok, "reason": str(r.get("reason") or "")[:200], "conflict": r.get("conflict") is True,
                        "manipulative": r.get("manipulative") is True})
        return out

    async def run_verify_hooks(self, a: Artifact) -> dict:
        ok, scores = True, []
        for h in self._verify_hooks:
            try:
                r = h(a)
                if inspect.isawaitable(r):
                    r = await r
            except Exception as e:
                log.warning("verify hook failed for %s: %s", a.id, e)
                ok = False
                scores.append(0.0)
                continue
            if r is None:
                continue
            if isinstance(r, bool):
                ok = ok and r
                scores.append(1.0 if r else 0.0)
            elif isinstance(r, dict):
                p = bool(r.get("pass", r.get("ok", False)))
                ok = ok and p
                try:
                    scores.append(float(r.get("score", 1.0 if p else 0.0)))
                except (TypeError, ValueError):
                    scores.append(1.0 if p else 0.0)
        return {"pass": ok, "score": (sum(scores) / len(scores)) if scores else 0.0, "n": len(scores)}

    # ------------------------------------------------------------------ maintenance
    async def merge_duplicates(self) -> int:
        """Near-duplicate artifacts (same type/kind/scope): pairs with cosine >= auto_merge_threshold merge
        directly; pairs >= merge_threshold are confirmed by one batched fast-tier call that may also propose
        the merged text. The merge itself is deterministic (ArtifactStore.merge)."""
        thr, auto = float(self.cfg["merge_threshold"]), float(self.cfg["auto_merge_threshold"])
        rank = {"active": 0, "quarantined": 1}
        pairs: list = []
        for type_ in TYPES:
            arts = await self.artifacts.list(type_=type_, status="active") +                 await self.artifacts.list(type_=type_, status="quarantined")
            groups: dict = {}
            for a in arts:
                groups.setdefault((a.scope, a.kind), []).append(a)
            for items in groups.values():
                if len(items) < 2:
                    continue
                vecs = await self.k.embedder.embed([a.embed_text for a in items])
                sims = vecs @ vecs.T
                for i in range(len(items)):
                    for j in range(i + 1, len(items)):
                        if sims[i, j] >= thr:
                            keep, drop = sorted((items[i], items[j]), key=lambda x: (
                                rank.get(x.status, 2), -x.net, -x.seen_count, x.created_at, x.id))
                            pairs.append((float(sims[i, j]), keep, drop))
        pairs.sort(key=lambda p: -p[0])
        confirm = [(k, d) for s, k, d in pairs if s < auto][:20]
        texts: dict = {}
        if confirm:
            listing = "\n".join(f"{n}. a：{truncate(k.situation + ' ' + k.text, 60)}\n"
                                f"   b：{truncate(d.situation + ' ' + d.text, 60)}"
                                for n, (k, d) in enumerate(confirm, start=1))
            try:
                data = await self.k.llm.json("grow.merge", MERGE_USER.format(pairs=listing), tier="fast",
                                             system=MERGE_SYSTEM, schema=MERGE_SCHEMA)
            except Exception as e:
                log.warning("grow.merge failed: %s", e)
                data = {"results": []}
            for r in data.get("results") or []:
                if not isinstance(r, dict) or r.get("same") is not True:
                    continue
                try:
                    n = int(r.get("pair"))
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(confirm):
                    t = str(r.get("text") or "").strip()
                    texts[(confirm[n - 1][0].id, confirm[n - 1][1].id)] = t if 0 < len(t) <= 80 else ""
        dead: set = set()
        merged = 0
        for s, keep, drop in pairs:
            if keep.id in dead or drop.id in dead:
                continue
            if s < auto and (keep.id, drop.id) not in texts:
                continue
            text = texts.get((keep.id, drop.id)) or None
            if keep.type == "expression":
                text = None    # expression style text is an observed form: keep the survivor's wording
            await self.artifacts.merge(keep.id, drop.id, text=text)
            dead.add(drop.id)
            merged += 1
        return merged

    async def retire_stale(self, now: float) -> int:
        n = 0
        ttl = float(self.cfg["quarantine_ttl_days"]) * 86400
        for a in await self.artifacts.list(status="quarantined"):
            qa = float(a.meta.get("quarantined_at") or a.created_at)
            if now - qa > ttl:
                await self.artifacts.set_status(a.id, "retired", reason="expired in quarantine without verification")
                n += 1
        unused = float(self.cfg["retire_unused_days"]) * 86400
        for a in await self.artifacts.list(status="active"):
            last = float(a.last_used or a.meta.get("promoted_at") or a.created_at)
            if now - last > unused:
                await self.artifacts.set_status(a.id, "retired", reason="unused for a long time")
                n += 1
        return n

    # ------------------------------------------------------------------ entity merge (docs/22 §3.3)
    async def rekey_entity(self, old: str, new: str) -> None:
        """``old`` was merged into ``new``: grow_* rows were rewritten (scopes as new versions, INV-9) by
        alive/world/rekey.py; move the caches (names, learner buffers, cached active artifacts)."""
        if old in self._names:
            self._names.setdefault(new, self._names.pop(old))
        await self.learner.rekey_entity(old, new)
        await self.artifacts.refresh_entity(old, new)

    # ------------------------------------------------------------------ privacy
    async def forget_person(self, person_key: str) -> dict:
        """Remove a person's traces from growth: examples they said, evidence about them, and artifacts
        scoped to them (rows deleted; their version snapshots are redacted, the only permitted UPDATE)."""
        pk = person_key
        await self.learner.forget_person(pk)    # first: wait for an in-flight learning run on their lines
        ex = await self.artifacts.remove_speaker(pk)
        ev = await self.evidence.delete_subject(pk)
        scope = f"person:{pk}"
        rows = await self.k.db.fetchall("SELECT id FROM grow_artifacts WHERE scope=?", (scope,))
        ids = [r["id"] for r in rows]
        if ids:
            q = ",".join("?" * len(ids))

            def _tx(c):
                c.execute(f"UPDATE grow_versions SET snapshot='{{\"redacted\": true}}', reason='privacy-redacted' "
                          f"WHERE artifact_id IN ({q})", ids)
                c.execute(f"DELETE FROM grow_artifacts WHERE id IN ({q})", ids)
            await self.k.db.transaction(_tx)
            for i in ids:
                self.artifacts._active.pop(i, None)
                self.artifacts._vecs.pop(i, None)
            self.artifacts._global_sel = None
        self._names.pop(pk, None)
        return {"examples": ex, "evidence": ev, "artifacts": len(ids)}

    async def _on_forget(self, ev) -> None:
        h = (ev.payload or {}).get("person_hash")
        if not h:
            return
        cands: set = set()
        for r in await self.k.db.fetchall("SELECT DISTINCT subject FROM grow_evidence WHERE subject IS NOT NULL"):
            cands.add(r["subject"])
        for r in await self.k.db.fetchall("SELECT scope, meta FROM grow_artifacts"):
            if str(r["scope"]).startswith("person:"):
                cands.add(r["scope"].split(":", 1)[1])
            try:
                import json
                m = json.loads(r["meta"] or "{}")
                cands.update(m.get("speakers") or [])
            except Exception:
                pass
        cands.update(self._names)
        for pk in cands:
            if pk and _hash(pk) == h:
                rep = await self.forget_person(pk)
                log.info("growth forget_person on tombstone: %s", rep)

    # ------------------------------------------------------------------ stats
    async def stats(self) -> dict:
        rows = await self.k.db.fetchall("SELECT type, kind, status, COUNT(*) AS n FROM grow_artifacts "
                                        "GROUP BY type, kind, status")
        arts: dict = {}
        for r in rows:
            arts.setdefault(f"{r['type']}/{r['kind']}", {})[r["status"]] = r["n"]
        last = await self.k.db.kv_get("grow.sleep", "last", None)
        return {"artifacts": arts, "evidence": await self.evidence.counts(), "skills": len(self.skills_registry._skills),
                "versions": await self.k.db.fetchval("SELECT COUNT(*) FROM grow_versions", default=0),
                "last_sleep": last, "learner_runs": self.learner.runs, **self.status()}
