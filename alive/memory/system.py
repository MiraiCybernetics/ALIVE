"""MemorySystem implementation (docs/03).

Layout
- ``Memory`` (this file): lifecycle, write path (``note``/``note_many``), hybrid retrieval, formatting,
  passive recall, queries, invalidation + dependency propagation, privacy deletion, batched access
  updates.
- ``extraction.ExtractionMixin``: per-thread debounced conversation observation, fast-tier extraction
  of episodes + candidate facts, Mem0-style reconciliation.
- ``consolidation.ConsolidationMixin``: sleep stages extract / reconcile / forget / precompute.

Concurrency: every DB read runs on the reader pool, writes are atomic jobs on the single writer
thread (``kernel.db.transaction``), vector search runs in a worker thread once the index is large.
Nothing blocks the event loop. Background work (debounced extraction, access flushes) runs as named
scheduler tasks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from alive.core.types import EV, Trust, new_id
from alive.interfaces import MemoryHit, MemorySystem
from alive.memory import schema as S
from alive.memory.consolidation import ConsolidationMixin
from alive.memory.extraction import ExtractionMixin
from alive.memory.prompts import DEEP_SCHEMA, DEEP_SYSTEM, DEEP_USER
from alive.memory.records import (CONFLICT_PENALTY, DAY, DEEP_STATUSES, DEFAULT_BASE_DAYS, KINDS, LIVE_STATUSES,
                                  STATUS_PENALTY, Hit, apply_provenance_rules, date_note, initial_strength,
                                  provenance_tag, recall_strength, retrievability, row_to_hit)
from alive.memory.textutil import (estimate_tokens, fts_match_expr, fts_terms, jaccard, like_escape, norm,
                                   truncate)
from alive.memory.timeparse import find_time_refs, resolve_first
from alive.memory.vindex import MemVectorIndex
from alive.runtime import chance as C
from alive.runtime.embed import from_blob, to_blob
from alive.world.audience import Scope, merge_origins, origins_of
from alive.world.rekey import rekey_dict

log = logging.getLogger("alive.memory")

DEFAULTS: dict = {
    "base_days": dict(DEFAULT_BASE_DAYS),
    "recall_gain": 0.5,
    "semantic_min_trust": int(Trust.PUBLIC),
    "weights": {"rel": 1.0, "rec": 0.6, "imp": 0.5, "aff": 0.15, "per": 0.4},
    "rrf_k": 60,
    "fast_candidates": 20,
    "standard_candidates": 48,
    "deep_rounds": 3,
    # spontaneous recall (docs/03 §5.3): each candidate surfaces with probability soft_step(activation, centre,
    # width); the centre and the sampling temperature move with arousal and fatigue (alive/runtime/chance.py)
    "recall": {"centre": 0.32, "width": 0.05, "temperature": 0.12, "mood_weight": 0.15},
    "recall_min_age_s": 300,
    "access_flush_s": 30,
    "access_flush_max": 256,
    "debounce": {"max_lines": 12, "idle_s": 600, "context_lines": 8, "max_batch": 40, "max_failures": 3},
    "extract_concurrency": 3,
    "reconcile_top_k": 10,
    "reconcile_min_sim": 0.25,
    "max_facts_per_segment": 12,
    "sensitive_words": ["性取向", "同性恋", "双性恋", "出柜", "宗教信仰", "信教", "受洗", "政治立场", "党员",
                        "抑郁症", "确诊", "艾滋", "HIV", "癌症", "精神病", "流产", "怀孕"],
    "forget": {"r_threshold": 0.1, "max_importance": 4, "max_groups": 20, "min_group": 2, "max_loss": 0.25},
    "recheck_batch": 8,
    "recheck_max_per_night": 40,
    "precompute": {"max_topics": 12, "budget_tokens": 500, "limit": 6},
}


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _qmarks(n: int) -> str:
    return ",".join("?" * n)


class Memory(ExtractionMixin, ConsolidationMixin, MemorySystem):
    def __init__(self, kernel):
        super().__init__(kernel)
        self.cfg: dict = _deep_merge(DEFAULTS, kernel.cfg.section("memory").data)
        self.w = self.cfg["weights"]
        self.index: Optional[MemVectorIndex] = None
        self._names: dict = {}             # person_key -> display name (for provenance tags)
        self._entity_names: dict = {}      # normalized entity -> count (cue entity detection)
        self._access: dict = {}            # id -> [count, last_ts]
        self._active = 0                   # background jobs in flight (settle hook / drain)
        self._tasks: set = set()
        self._precomputed: dict = {}       # date -> [bundle]
        self._lat: dict = {"fast": deque(maxlen=200), "standard": deque(maxlen=200), "deep": deque(maxlen=50)}
        self.counters: dict = {"writes": 0, "downgrades": 0, "retrievals": 0, "extractions": 0,
                               "facts_skipped_sensitive": 0, "invalidations": 0}
        self._started = False
        self._embedder_tag = ""
        self._init_extraction()

    # ================================================================== lifecycle
    async def start(self) -> None:
        emb = self.k.embedder
        self._embedder_tag = f"{getattr(emb, 'name', 'embedder')}:{emb.dim}"
        self.index = MemVectorIndex(emb.dim, capacity=1024)
        await self._load_vectors()
        await self._load_entity_names()
        self._names.update(await self.k.db.kv_get("mem", "names", {}) or {})
        self._precomputed = {self.k.clock.today(): await self.k.db.kv_get("mem.precompute", self.k.clock.today(), [])
                             or []}
        await self._load_buffers()
        if hasattr(self.k.clock, "add_settle_hook"):
            self.k.clock.add_settle_hook(self._settle_hook)
        self.k.scheduler.every(float(self.cfg["access_flush_s"]), self.flush_access, "mem.access_flush")
        self._started = True
        try:
            await self.load_seed_memories()
        except Exception:
            log.exception("loading seed memories failed")

    async def stop(self) -> None:
        self._started = False
        self._stop_watchers()
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        try:
            await self.flush_access()
        except Exception:
            log.exception("final access flush failed")
        try:
            await self._save_buffers()
            await self.k.db.kv_set("mem", "names", dict(list(self._names.items())[-5000:]), ts=self.k.clock.now())
        except Exception:
            log.exception("saving memory state failed")

    def status(self) -> dict:
        return {"indexed": len(self.index) if self.index is not None else 0,
                "buffers": len(self._buffers), "pending_lines": sum(len(b.lines) for b in self._buffers.values()),
                "active_jobs": self._active, "access_pending": len(self._access), **self.counters}

    async def _settle_hook(self) -> bool:
        """ManualClock settle hook: wait (real time) for in-flight background memory jobs."""
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
        """Wait for background jobs (tests, sleep stages)."""
        for _ in range(100):
            pending = [t for t in self._tasks if not t.done()]
            if not pending and self._active <= 0:
                return
            if pending:
                await asyncio.wait(pending, timeout=10.0)
            else:
                await asyncio.sleep(0.001)

    async def _load_vectors(self) -> None:
        tag, dim = self._embedder_tag, self.k.embedder.dim

        def _read(c):
            # runs in a reader thread: decoding, stacking and filling the index never touch the event loop
            ids, vecs, stale = [], [], []
            for r in c.execute("SELECT id, embedder, dim, vec FROM mem_vec"):
                if r["embedder"] != tag or r["dim"] != dim:
                    stale.append(r["id"])
                    continue
                ids.append(r["id"])
                vecs.append(from_blob(r["vec"]))
            missing = [r[0] for r in c.execute(
                "SELECT r.id FROM mem_records r LEFT JOIN mem_vec v ON v.id = r.id WHERE v.id IS NULL")]
            index = None
            if ids:
                index = MemVectorIndex(dim, capacity=max(1024, int(len(ids) * 1.25)))
                index.add_many(ids, np.stack(vecs).astype(np.float32))
            return index, stale + missing

        index, stale = await self.k.db.read(_read)
        if index is not None:
            self.index = index
        if stale:
            log.info("re-embedding %d memory records (embedder changed or vectors missing)", len(stale))
            self._spawn(lambda: self._reembed(stale), "mem.reembed")

    async def _reembed(self, ids: list) -> None:
        for i in range(0, len(ids), 256):
            chunk = ids[i:i + 256]
            rows = await self.k.db.fetchall(f"SELECT id, content FROM mem_records WHERE id IN ({_qmarks(len(chunk))})",
                                            chunk)
            if not rows:
                continue
            vecs = await self.k.embedder.embed([r["content"] for r in rows])
            data = [(r["id"], self._embedder_tag, self.k.embedder.dim, to_blob(v)) for r, v in zip(rows, vecs)]
            await self.k.db.executemany("INSERT OR REPLACE INTO mem_vec (id, embedder, dim, vec) VALUES (?,?,?,?)", data)
            for (rid, *_), v in zip(data, vecs):
                self.index.add(rid, v)

    async def _load_entity_names(self) -> None:
        rows = await self.k.db.fetchall("SELECT key, COUNT(*) AS n FROM mem_ent WHERE role='entity' GROUP BY key")
        self._entity_names = {r["key"]: r["n"] for r in rows}

    # ================================================================== write path
    def _norm_refers(self, refers_to: Any, content: str, observed_at: float) -> Optional[dict]:
        tz = self.k.clock.tz
        if refers_to is None:
            ref = resolve_first(content, observed_at, tz)
            return ref.as_dict() if ref else None
        if isinstance(refers_to, str):
            refers_to = {"raw": refers_to}
        if not isinstance(refers_to, dict):
            return None
        raw = refers_to.get("raw") or refers_to.get("when") or ""
        start = refers_to.get("start")
        if start is None and refers_to.get("date"):
            raw = raw or str(refers_to["date"])
        if start is None:
            ref = resolve_first(str(raw or refers_to.get("date") or ""), observed_at, tz) if (raw or refers_to.get("date")) else None
            if ref is None:
                ref = resolve_first(content, observed_at, tz)
            if ref is None:
                return {"start": None, "end": None, "granularity": None, "raw": raw or None} if raw else None
            d = ref.as_dict()
            d["raw"] = raw or ref.raw
            return d
        try:
            st = float(start)
        except (TypeError, ValueError):
            return None
        end = refers_to.get("end")
        return {"start": st, "end": float(end) if end is not None else st + DAY,
                "granularity": refers_to.get("granularity") or "day", "raw": raw or None}

    @staticmethod
    def _norm_affect(affect: Optional[dict]) -> tuple:
        if not affect:
            return 0.0, 0.0, None, False
        v = affect.get("valence", affect.get("v", affect.get("P", 0.0))) or 0.0
        a = affect.get("arousal", affect.get("a", affect.get("A", 0.0))) or 0.0
        try:
            v, a = float(v), float(a)
        except (TypeError, ValueError):
            v, a = 0.0, 0.0
        return _clamp(v, -1.0, 1.0), _clamp(a, 0.0, 1.0), affect.get("label") or None, bool(affect.get("peak"))

    def _prepare(self, *, kind: str, content: str, importance: int = 3, affect: Optional[dict] = None,
                 source: str = "experienced", told_by: Optional[str] = None, trust: Any = Trust.SYSTEM,
                 evidence: Sequence[str] = (), entities: Sequence[str] = (), person_keys: Sequence[str] = (),
                 refers_to: Optional[dict] = None, derived_from: Sequence[str] = (), thread_id: Optional[str] = None,
                 valid_from: Optional[float] = None, observed_at: Optional[float] = None,
                 status: Optional[str] = None, pinned: bool = False, meta: Optional[dict] = None,
                 conflict_with: Sequence[str] = (), parent_version: Optional[str] = None, version: int = 1,
                 record_id: Optional[str] = None) -> dict:
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content is empty")
        content = truncate(content, 2000)
        now = self.k.clock.now()
        obs = float(observed_at) if observed_at is not None else now
        prov = apply_provenance_rules(kind, source, told_by, trust, int(self.cfg["semantic_min_trust"]))
        st = status or prov.status
        if pinned:
            st = "pinned"
        if prov.status == "pending" and st == "active":
            st = "pending"
        v, a, label, peak = self._norm_affect(affect)
        imp = int(_clamp(int(importance or 3), 1, 10))
        ref = self._norm_refers(refers_to, content, obs)
        ents: list = []
        for e in entities or ():
            e2 = norm(str(e))[:32]
            if e2 and e2 not in ents:
                ents.append(e2)
        ents = ents[:12]
        pks = [p for p in dict.fromkeys(str(p) for p in (person_keys or ()) if p)]
        m = dict(meta or {})
        if prov.downgraded_from:
            m["requested_kind"] = prov.downgraded_from
            m["downgrade"] = prov.notes
        if prov.url:
            m["url"] = prov.url
        strength = initial_strength(prov.kind, imp, v, a, peak, self.cfg["base_days"], pinned=(st == "pinned"))
        rid = record_id or new_id("m")
        deps = [d for d in dict.fromkeys(derived_from or ()) if d and d != rid]
        row = {
            "id": rid, "kind": prov.kind, "content": content, "entities": json.dumps(ents, ensure_ascii=False),
            "person_keys": json.dumps(pks, ensure_ascii=False), "observed_at": obs,
            "ref_start": ref.get("start") if ref else None, "ref_end": ref.get("end") if ref else None,
            "ref_granularity": ref.get("granularity") if ref else None, "ref_raw": ref.get("raw") if ref else None,
            "valid_from": float(valid_from) if valid_from is not None else obs, "valid_to": None,
            "recorded_at": now, "expired_at": None, "source": prov.source, "told_by": prov.told_by,
            "trust": prov.trust, "evidence": json.dumps(list(dict.fromkeys(str(e) for e in evidence or ())),
                                                        ensure_ascii=False),
            "importance": imp, "aff_valence": v, "aff_arousal": a, "aff_label": label, "aff_peak": int(peak),
            "strength": strength, "access_count": 0, "last_access": now,
            "derived_from": json.dumps(deps), "status": st, "version": int(version),
            "parent_version": parent_version, "thread_id": thread_id,
            "conflict_with": json.dumps(list(conflict_with or ())), "meta": json.dumps(m, ensure_ascii=False),
        }
        ent_rows = [(e, rid, "entity") for e in ents] + [(p, rid, "person") for p in pks]
        if prov.told_by:
            ent_rows.append((prov.told_by, rid, "told_by"))
        return {"row": row, "ents": ent_rows, "deps": [(d, rid) for d in deps], "prov": prov}

    async def _insert(self, preps: list, vecs: np.ndarray) -> None:
        tag, dim = self._embedder_tag, self.k.embedder.dim
        rows = [tuple(p["row"][c] for c in S.RECORD_COLS) for p in preps]
        ents = [e for p in preps for e in p["ents"]]
        deps = [d for p in preps for d in p["deps"]]
        vrows = [(p["row"]["id"], tag, dim, to_blob(v)) for p, v in zip(preps, vecs)]

        def _tx(c):
            c.executemany(S.INSERT_SQL, rows)
            if ents:
                c.executemany("INSERT OR IGNORE INTO mem_ent (key, record_id, role) VALUES (?,?,?)", ents)
            if deps:
                c.executemany("INSERT OR IGNORE INTO mem_deps (depends_on, record_id) VALUES (?,?)", deps)
            c.executemany("INSERT OR REPLACE INTO mem_vec (id, embedder, dim, vec) VALUES (?,?,?,?)", vrows)
            return len(rows)

        await self.k.db.transaction(_tx)
        assert self.index is not None
        new_ids = [p["row"]["id"] for p in preps]
        if len(new_ids) > 256:   # bulk imports: fill the (thread-safe) index off the event loop
            await asyncio.get_running_loop().run_in_executor(None, self.index.add_many, new_ids, vecs)
        else:
            self.index.add_many(new_ids, vecs)
        for p in preps:
            for e, _, role in p["ents"]:
                if role == "entity":
                    self._entity_names[e] = self._entity_names.get(e, 0) + 1
        self.counters["writes"] += len(preps)
        self.counters["downgrades"] += sum(1 for p in preps if p["prov"].downgraded_from)

    async def note(self, *, kind: str, content: str, importance: int = 3, affect: Optional[dict] = None,
                   source: str = "experienced", told_by: Optional[str] = None, trust: Trust = Trust.SYSTEM,
                   evidence: Sequence[str] = (), entities: Sequence[str] = (), person_keys: Sequence[str] = (),
                   refers_to: Optional[dict] = None, derived_from: Sequence[str] = (),
                   thread_id: Optional[str] = None, valid_from: Optional[float] = None,
                   observed_at: Optional[float] = None, status: Optional[str] = None, pinned: bool = False,
                   meta: Optional[dict] = None, conflict_with: Sequence[str] = (),
                   parent_version: Optional[str] = None, version: int = 1, emit: bool = True) -> str:
        """Write one record (see interface). Extra keyword args: observed_at, status, pinned, meta,
        conflict_with, parent_version, version, emit. A record derived from others (``derived_from``, a new
        version of ``parent_version``) keeps every origin of its sources: the most restrictive audience
        (alive/world/audience.py)."""
        meta = await self._with_origins(meta, thread_id, list(derived_from or ()) + [parent_version or ""])
        p = self._prepare(kind=kind, content=content, importance=importance, affect=affect, source=source,
                          told_by=told_by, trust=trust, evidence=evidence, entities=entities,
                          person_keys=person_keys, refers_to=refers_to, derived_from=derived_from,
                          thread_id=thread_id, valid_from=valid_from, observed_at=observed_at, status=status,
                          pinned=pinned, meta=meta, conflict_with=conflict_with, parent_version=parent_version,
                          version=version)
        vec = await self.k.embedder.embed([p["row"]["content"]])
        await self._insert([p], vec)
        if p["prov"].downgraded_from:
            log.info("memory %s downgraded %s -> %s (%s)", p["row"]["id"], p["prov"].downgraded_from,
                     p["row"]["kind"], "; ".join(p["prov"].notes))
        if emit:
            r = p["row"]
            payload = {"id": r["id"], "kind": r["kind"], "source": r["source"], "importance": r["importance"],
                       "status": r["status"]}
            if p["prov"].downgraded_from:
                payload["downgraded_from"] = p["prov"].downgraded_from
            if r["parent_version"]:
                payload["parent"] = r["parent_version"]
            await self.k.bus.emit(EV.MEMORY_WRITE, payload, source="memory", thread_id=thread_id,
                                  trust=Trust(min(int(r["trust"]), int(Trust.SYSTEM))))
        return p["row"]["id"]

    async def note_many(self, items: Sequence[dict], *, emit: bool = True) -> list:
        """Bulk write (seed memories, imports, benchmarks): one embedding batch, one transaction,
        one ``mem.write`` event. Items take the same keys as ``note``. Invalid items are skipped."""
        preps = []
        for it in items:
            if it.get("derived_from") or it.get("parent_version"):
                it = {**it, "meta": await self._with_origins(it.get("meta"), it.get("thread_id"),
                                                             list(it.get("derived_from") or ())
                                                             + [it.get("parent_version") or ""])}
            try:
                preps.append(self._prepare(**it))
            except (ValueError, TypeError) as e:
                log.debug("note_many skip: %s", e)
        if not preps:
            return []
        out: list = []
        for i in range(0, len(preps), 2000):
            chunk = preps[i:i + 2000]
            vecs = await self.k.embedder.embed([p["row"]["content"] for p in chunk])
            await self._insert(chunk, vecs)
            out.extend(p["row"]["id"] for p in chunk)
        if emit:
            await self.k.bus.emit(EV.MEMORY_WRITE, {"count": len(out), "bulk": True}, source="memory")
        return out

    async def _with_origins(self, meta: Optional[dict], thread_id: Optional[str], sources: Sequence[str]) -> Optional[dict]:
        """meta + ``origins`` inherited from the source records (unchanged when no source carries any)."""
        ids = [i for i in dict.fromkeys(sources or ()) if i]
        if not ids:
            return meta
        rows = await self.k.db.fetchall(f"SELECT id, thread_id, meta FROM mem_records WHERE id IN ({_qmarks(len(ids))})",
                                        ids)
        inherited = merge_origins(*(origins_of(r) for r in rows))
        own = list((meta or {}).get("origins") or []) if isinstance(meta, dict) else []
        if not inherited and not own:
            return meta
        out = dict(meta or {})
        out["origins"] = merge_origins([thread_id] if thread_id else [], own, inherited)
        return out

    async def pin(self, record_id: str, pinned: bool = True) -> bool:
        """Pin (never decays, never auto-rewritten) or unpin a record."""
        r = await self.k.db.fetchone("SELECT kind, importance, aff_valence, aff_arousal, aff_peak FROM mem_records "
                                     "WHERE id=?", (record_id,))
        if r is None:
            return False
        if pinned:
            n = await self.k.db.execute("UPDATE mem_records SET status='pinned', strength=? WHERE id=?",
                                        (initial_strength(r["kind"], 1, 0, 0, False, pinned=True), record_id))
        else:
            s = initial_strength(r["kind"], r["importance"], r["aff_valence"], r["aff_arousal"], bool(r["aff_peak"]),
                                 self.cfg["base_days"])
            n = await self.k.db.execute("UPDATE mem_records SET status='active', strength=?, last_access=? "
                                        "WHERE id=? AND status='pinned'", (s, self.k.clock.now(), record_id))
        return bool(n)

    async def get(self, record_id: str) -> Optional[Hit]:
        r = await self.k.db.fetchone("SELECT * FROM mem_records WHERE id=?", (record_id,))
        return row_to_hit(r) if r else None

    async def get_many(self, ids: Sequence[str]) -> list:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        rows = await self.k.db.fetchall(f"SELECT * FROM mem_records WHERE id IN ({_qmarks(len(ids))})", ids)
        by = {r["id"]: row_to_hit(r) for r in rows}
        return [by[i] for i in ids if i in by]

    # ================================================================== retrieval
    def _filters(self, statuses: Sequence[str], kinds: Optional[Sequence[str]], since: Optional[float],
                 until: Optional[float], alias: str = "r") -> tuple:
        # unary '+' keeps these low-selectivity columns from driving the query plan (the planner otherwise
        # prefers ix_mem_status over the primary key for "id IN (...)" lookups without ANALYZE stats)
        where = [f"+{alias}.status IN ({_qmarks(len(statuses))})"]
        params: list = list(statuses)
        if kinds:
            where.append(f"+{alias}.kind IN ({_qmarks(len(kinds))})")
            params += list(kinds)
        if since is not None:
            where.append(f"{alias}.observed_at >= ?")
            params.append(float(since))
        if until is not None:
            where.append(f"{alias}.observed_at < ?")
            params.append(float(until))
        return " AND ".join(where), params

    async def _route_text(self, cue: str, flt: tuple, k: int) -> list:
        """FTS5 trigram route (+ LIKE fallback for 1–2 char CJK terms). Returns full rows, best first."""
        tri, short = fts_terms(cue)
        where, params = flt
        if not tri and not short:
            return []

        def _q(c):
            out = []
            if tri:
                expr = fts_match_expr(tri)
                sql = (f"SELECT r.* FROM (SELECT rowid AS rid, rank FROM mem_fts WHERE mem_fts MATCH ? "
                       f"ORDER BY rank LIMIT ?) f JOIN mem_records r ON r.rowid = f.rid WHERE {where} "
                       f"ORDER BY f.rank LIMIT ?")
                try:
                    out = c.execute(sql, [expr, k * 4] + params + [k]).fetchall()
                except Exception as e:  # malformed query should never break retrieval
                    log.debug("fts query failed: %s", e)
            if short and len(out) < k:
                likes = " OR ".join(["r.content LIKE ? ESCAPE '\\'"] * len(short))
                sql = f"SELECT r.* FROM mem_records r WHERE ({likes}) AND {where} ORDER BY r.rowid DESC LIMIT ?"
                seen = {r["id"] for r in out}
                for r in c.execute(sql, [f"%{like_escape(s)}%" for s in short] + params + [k]).fetchall():
                    if r["id"] not in seen:
                        out.append(r)
            return out

        return await self.k.db.read(_q)

    def _cue_entities(self, cue: str, limit: int = 6) -> list:
        t = norm(cue)
        if not t or not self._entity_names:
            return []
        hits = [e for e in self._entity_names if len(e) >= 2 and e in t]
        hits.sort(key=lambda e: (-len(e), -self._entity_names.get(e, 0)))
        return hits[:limit]

    async def _route_entities(self, cue: str, person_key: Optional[str], flt: tuple, k: int, hops: int) -> tuple:
        """Spreading activation over the entity/person index: seeds -> records (hop 1) -> co-occurring
        entities -> records (hop 2). Returns (rows_hop1, rows_hop2)."""
        seeds = self._cue_entities(cue)
        pseeds = [person_key] if person_key else []
        if not seeds and not pseeds:
            return [], []
        where, params = flt

        per_key = max(100, k * 8)

        def records_for(c, keys: list, lim: int) -> list:
            # bounded: the newest ``per_key`` records of each key (ids are time ordered), then rank
            ids: list = []
            for key in keys:
                ids += [r[0] for r in c.execute("SELECT record_id FROM mem_ent WHERE key=? ORDER BY record_id DESC "
                                                "LIMIT ?", (key, per_key))]
            ids = list(dict.fromkeys(ids))
            if not ids:
                return []
            return c.execute(f"SELECT r.* FROM mem_records r WHERE r.id IN ({_qmarks(len(ids))}) AND {where} "
                             f"ORDER BY r.importance DESC, r.last_access DESC LIMIT ?", ids + params + [lim]).fetchall()

        def _q(c):
            keys = seeds + pseeds
            h1 = records_for(c, keys, k)
            h2: list = []
            if hops >= 2 and h1:
                ids = [r["id"] for r in h1[:30]]
                co = c.execute(
                    f"SELECT key, COUNT(*) AS n FROM mem_ent WHERE record_id IN ({_qmarks(len(ids))}) "
                    f"AND role='entity' AND key NOT IN ({_qmarks(len(keys))}) GROUP BY key ORDER BY n DESC LIMIT 5",
                    ids + keys).fetchall()
                ckeys = [r["key"] for r in co]
                if ckeys:
                    seen = set(ids)
                    for r in records_for(c, ckeys, max(4, k // 2)):
                        if r["id"] not in seen:
                            h2.append(r)
            return h1, h2

        return await self.k.db.read(_q)

    async def _route_time(self, ranges: list, flt: tuple, k: int) -> list:
        where, params = flt
        if not ranges:
            return []
        s, e = ranges[0]

        def _q(c):
            a = c.execute(f"SELECT r.* FROM mem_records r WHERE r.observed_at >= ? AND r.observed_at < ? AND {where} "
                          f"ORDER BY r.importance DESC, r.observed_at DESC LIMIT ?", [s, e] + params + [k]).fetchall()
            b = c.execute(f"SELECT r.* FROM mem_records r WHERE r.ref_start IS NOT NULL AND r.ref_start < ? "
                          f"AND COALESCE(r.ref_end, r.ref_start) > ? AND {where} "
                          f"ORDER BY r.importance DESC LIMIT ?", [e, s] + params + [k]).fetchall()
            seen, out = set(), []
            for r in list(b) + list(a):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
            return out[:k]

        return await self.k.db.read(_q)

    def _pre_route(self, cue: str, person_key: Optional[str]) -> list:
        bundles = self._precomputed.get(self.k.clock.today()) or []
        ids: list = []
        for b in bundles:
            if (person_key and b.get("person_key") == person_key) or jaccard(cue, b.get("topic", "")) >= 0.3:
                ids.extend(b.get("hit_ids") or [])
        return list(dict.fromkeys(ids))[:12]

    def _mood(self, mood: Optional[tuple]) -> tuple:
        if mood is not None:
            try:
                return float(mood[0]), float(mood[1]) if len(mood) > 1 else 0.0
            except Exception:
                return 0.0, 0.0
        aff = getattr(self.k, "affect", None)
        if aff is None:
            return 0.0, 0.0
        try:
            s = aff.snapshot()
            return float(s.P), float(s.A)
        except Exception:
            return 0.0, 0.0

    async def _candidates(self, cue: str, *, person_key: Optional[str], tier: str, kinds, since, until,
                          statuses: Sequence[str], k: int) -> tuple:
        """Multi-route recall + RRF. Returns (ranked [(id, rrf)], rows_by_id, cos_by_id, person_hop2, time_ids, qv)."""
        assert self.index is not None
        flt = self._filters(statuses, kinds, since, until)
        qv = (await self.k.embedder.embed([cue or " "]))[0]
        routes: dict = {}
        coros = [self._route_text(cue, flt, k), self.index.search_async(qv, k * 3)]
        time_ranges: list = []
        if tier != "fast":
            coros.append(self._route_entities(cue, person_key, flt, k, hops=2))
            time_ranges = [(t.start, t.end) for t in find_time_refs(cue, self.k.clock.now(), self.k.clock.tz, 2)]
            coros.append(self._route_time(time_ranges, flt, k))
        res = await asyncio.gather(*coros)
        rows: dict = {}
        text_rows, vec_hits = res[0], res[1]
        routes["text"] = [r["id"] for r in text_rows]
        rows.update({r["id"]: r for r in text_rows})
        routes["vec"] = [i for i, s in vec_hits if s > 0.05]
        hop2_ids: set = set()
        time_ids: set = set()
        if tier != "fast":
            h1, h2 = res[2]
            routes["ent"] = [r["id"] for r in h1] + [r["id"] for r in h2]
            hop2_ids = {r["id"] for r in h2}
            rows.update({r["id"]: r for r in h1})
            rows.update({r["id"]: r for r in h2})
            trows = res[3]
            routes["time"] = [r["id"] for r in trows]
            time_ids = set(routes["time"])
            rows.update({r["id"]: r for r in trows})
        pre = self._pre_route(cue, person_key)
        if pre:
            routes["pre"] = pre
        kk = float(self.cfg["rrf_k"])
        fused: dict = {}
        for name, ids in routes.items():
            for rank, i in enumerate(ids):
                fused[i] = fused.get(i, 0.0) + 1.0 / (kk + rank + 1)
        ranked = sorted(fused.items(), key=lambda x: -x[1])
        cap = int(self.cfg["fast_candidates"]) if tier == "fast" else max(int(self.cfg["standard_candidates"]), k)
        ranked = ranked[: cap * 2]
        missing = [i for i, _ in ranked if i not in rows]
        if missing:
            where, params = flt
            more = await self.k.db.fetchall(
                f"SELECT r.* FROM mem_records r WHERE r.id IN ({_qmarks(len(missing))}) AND {where}", missing + params)
            rows.update({r["id"]: r for r in more})
        ranked = [(i, s) for i, s in ranked if i in rows][:cap]
        cos = self.index.cosine_many(qv, [i for i, _ in ranked])
        return ranked, rows, cos, hop2_ids, time_ids, qv

    def _score(self, r, rrf_norm: float, cos: float, *, now: float, person_key: Optional[str], mood: tuple,
               hop2: bool, in_time: bool) -> float:
        rel = 0.6 * rrf_norm + 0.4 * max(0.0, cos)
        if in_time:
            rel = min(1.0, rel + 0.3)
        ret = retrievability(now, float(r["last_access"]), float(r["strength"]))
        imp = int(r["importance"]) / 10.0
        if r["aff_valence"] or r["aff_arousal"]:
            mc = 1.0 - abs(float(r["aff_valence"]) - mood[0]) / 2.0
        else:
            mc = 0.5
        per = 0.0
        if person_key:
            pks = r["person_keys"] or "[]"
            if r["told_by"] == person_key or f'"{person_key}"' in pks:
                per = 1.0
            elif hop2:
                per = 0.3
        pen = STATUS_PENALTY.get(r["status"], 0.0)
        if r["conflict_with"] and r["conflict_with"] != "[]":
            pen += CONFLICT_PENALTY
        w = self.w
        return (w["rel"] * rel + w["rec"] * ret + w["imp"] * imp + w["aff"] * mc + w["per"] * per - pen)

    def _pack(self, scored: list, budget_tokens: int, limit: int) -> list:
        out, used, seen = [], 0, set()
        for h in scored:
            key = norm(h.content)
            if key in seen:
                continue
            cost = estimate_tokens(h.content) + 8
            if used + cost > budget_tokens:
                continue
            seen.add(key)
            out.append(h)
            used += cost
            if len(out) >= limit:
                break
        return out

    async def _retrieve_once(self, cue: str, *, person_key, tier, kinds, since, until, mood, limit,
                             statuses) -> list:
        k = max(limit * 2, 16) if tier != "fast" else min(20, max(limit, 10))
        ranked, rows, cos, hop2, time_ids, _ = await self._candidates(
            cue, person_key=person_key, tier=tier, kinds=kinds, since=since, until=until, statuses=statuses, k=k)
        if not ranked:
            return []
        now = self.k.clock.now()
        md = self._mood(mood)
        top = ranked[0][1] or 1.0
        hits = []
        for i, rrf in ranked:
            r = rows[i]
            s = self._score(r, rrf / top, cos.get(i, 0.0), now=now, person_key=person_key, mood=md,
                            hop2=i in hop2, in_time=i in time_ids)
            hits.append(row_to_hit(r, s))
        hits.sort(key=lambda h: -h.score)
        return hits

    async def retrieve(self, cue: str, *, person_key: Optional[str] = None, thread_id: Optional[str] = None,
                       budget_tokens: int = 1200, tier: str = "standard", kinds: Optional[Sequence[str]] = None,
                       since: Optional[float] = None, until: Optional[float] = None,
                       mood: Optional[tuple] = None, limit: int = 12, statuses: Optional[Sequence[str]] = None,
                       touch: bool = True, present: Optional[Sequence[str]] = None,
                       withheld: Optional[list] = None) -> list:
        """Hybrid retrieval (see interface). Extra kwargs: ``statuses`` (override the tier's status set),
        ``touch`` (update access/strength, default True).

        Audience (INV-34): ``thread_id`` is the space this retrieval is for. Records whose audience does not cover
        everyone who would hear me there (that space's people, plus ``present``) are left out; their contents are
        appended to ``withheld`` (if given) so the outbound check can make sure they are not said anyway. Without
        ``thread_id`` / ``present`` (my own thinking, sleep) nothing is filtered."""
        t0 = time.perf_counter()
        tier = tier if tier in ("fast", "standard", "deep") else "standard"
        cue = (cue or "").strip()
        sts = tuple(statuses) if statuses else (DEEP_STATUSES if tier == "deep" else LIVE_STATUSES)
        if kinds:
            kinds = [x for x in kinds if x in KINDS]
            if not kinds:          # asked for kinds that do not exist: nothing matches
                return []
        else:
            kinds = None
        if tier == "deep":
            hits = await self._retrieve_deep(cue, person_key=person_key, kinds=kinds, since=since, until=until,
                                             mood=mood, limit=limit, statuses=sts, thread_id=thread_id)
        else:
            hits = await self._retrieve_once(cue, person_key=person_key, tier=tier, kinds=kinds, since=since,
                                             until=until, mood=mood, limit=limit, statuses=sts)
        scope = self.scope(thread_id, present) if (thread_id or present) else None
        if scope is not None:
            hits, out = scope.split(hits)
            self._note_withheld(out, hits, withheld, budget_tokens, limit)
        packed = self._pack(hits, budget_tokens, limit)
        if touch and packed:
            self._touch([h.id for h in packed])
        self.counters["retrievals"] += 1
        self._lat[tier].append((time.perf_counter() - t0) * 1000)
        return packed

    def scope(self, space: Optional[str], present: Optional[Sequence[str]] = None) -> Scope:
        """Who would hear me in ``space`` (alive/world/audience.py)."""
        return Scope(getattr(self.k, "world", None), space, list(present or ()))

    def _note_withheld(self, out: list, kept: list, withheld: Optional[list], budget_tokens: int, limit: int) -> None:
        if withheld is None or not out:
            return
        known = {norm(h.content) for h in kept}
        for h in self._pack(sorted(out, key=lambda h: -h.score), budget_tokens, limit):
            if norm(h.content) not in known and h.content not in withheld:
                withheld.append(h.content)

    async def _retrieve_deep(self, cue: str, *, person_key, kinds, since, until, mood, limit, statuses,
                             thread_id) -> list:
        pool: dict = {}

        def merge(hs):
            for h in hs:
                if h.id not in pool or pool[h.id].score < h.score:
                    pool[h.id] = h

        merge(await self._retrieve_once(cue, person_key=person_key, tier="standard", kinds=kinds, since=since,
                                        until=until, mood=mood, limit=limit * 2, statuses=statuses))
        asked: set = {norm(cue)}
        for _ in range(int(self.cfg["deep_rounds"])):
            cur = sorted(pool.values(), key=lambda h: -h.score)[: limit * 2]
            listing = "\n".join(f"{n + 1}. {truncate(h.content, 120)}" for n, h in enumerate(cur)) or "（还什么都没想起来）"
            try:
                plan = await self.k.llm.json("mem.deep", DEEP_USER.format(cue=cue, hits=listing), tier="deliberate",
                                             system=DEEP_SYSTEM, schema=DEEP_SCHEMA, thread_id=thread_id)
            except Exception as e:
                log.warning("deep retrieval planner failed: %s", e)
                break
            queries = [q for q in (plan.get("queries") or []) if isinstance(q, str) and q.strip()
                       and norm(q) not in asked][:3]
            hint = plan.get("time_hint") if isinstance(plan.get("time_hint"), str) else ""
            if plan.get("enough") or (not queries and not hint):
                break
            for q in queries:
                asked.add(norm(q))
                merge(await self._retrieve_once(q, person_key=person_key, tier="standard", kinds=kinds, since=since,
                                                until=until, mood=mood, limit=limit, statuses=statuses))
            if hint:
                refs = find_time_refs(hint, self.k.clock.now(), self.k.clock.tz, 1)
                if refs:
                    merge(await self._retrieve_once(cue, person_key=person_key, tier="standard", kinds=kinds,
                                                    since=refs[0].start, until=refs[0].end, mood=mood, limit=limit,
                                                    statuses=statuses))
        return sorted(pool.values(), key=lambda h: -h.score)

    def format_hits(self, hits: Sequence[MemoryHit], now: float) -> str:
        tz = self.k.clock.tz
        lines = []
        for h in hits:
            lines.append(f"- {h.content}{date_note(h, tz)}{provenance_tag(h, now, tz, self._names)}")
        return "\n".join(lines)

    def remember_name(self, person_key: str, name: str) -> None:
        """Cache a display name for provenance tags (also learned from observed conversations)."""
        if person_key and name:
            self._names[person_key] = name

    # ================================================================== passive recall
    def _recall_state(self) -> tuple:
        """(pleasure -1..1, arousal 0..1, fatigue 0..1) for spontaneous recall."""
        P, A = self._mood(None)
        energy = 0.7
        aff = getattr(self.k, "affect", None)
        if aff is not None:
            try:
                energy = float(aff.snapshot().energy)
            except Exception:
                pass
        return P, C.clamp((A + 1.0) / 2.0), C.clamp(1.0 - energy)

    async def recall_cues(self, texts: Sequence[str], limit: int = 3, *, rng=None) -> list:
        """Spontaneous "that reminds me" (docs/03 §5.3). Unlike a query, nothing here is a ranking: each candidate
        comes to mind with a probability that rises smoothly with how strongly the cue activates it (the centre of
        that curve drops a little when I am aroused), and which of the ones that came up I notice is a
        Plackett-Luce draw whose temperature rises with fatigue and falls with arousal; memories that match my
        mood come up more easily (mood congruence)."""
        cue = " ".join(t for t in texts if t)[-600:].strip()
        if not cue:
            return []
        now = self.k.clock.now()
        min_age = float(self.cfg["recall_min_age_s"])
        ranked, rows, cos, _, _, _ = await self._candidates(
            cue, person_key=None, tier="fast", kinds=None, since=None, until=now - min_age,
            statuses=("active", "pinned", "stale"), k=10)
        if not ranked:
            return []
        rng = rng if rng is not None else C.rng_of(self.k)
        rc = {**DEFAULTS["recall"], **(self.cfg.get("recall") or {})}
        P, arousal, fatigue = self._recall_state()
        centre = float(rc["centre"]) * (1.0 - 0.25 * (arousal - 0.5))
        width = float(rc["width"])
        temp = float(rc["temperature"]) * (1.0 + 0.8 * fatigue) * (1.3 - 0.6 * arousal)
        top = ranked[0][1] or 1.0
        came_up: dict = {}
        for i, rrf in ranked:
            r = rows[i]
            act = 0.7 * max(0.0, cos.get(i, 0.0)) + 0.3 * (rrf / top)
            mc = (1.0 - abs(float(r["aff_valence"]) - P) / 2.0) if (r["aff_valence"] or r["aff_arousal"]) else 0.5
            act_m = act + float(rc["mood_weight"]) * (mc - 0.5) * act
            if not C.bernoulli(rng, C.soft_step(act_m, centre, width)):
                continue
            ret = retrievability(now, float(r["last_access"]), float(r["strength"]))
            came_up[i] = (act_m * (0.5 + 0.5 * ret) + 0.1 * int(r["importance"]) / 10.0, r)
        if not came_up:
            return []
        picked = C.gumbel_topk(rng, {i: s for i, (s, _) in came_up.items()}, max(0, int(limit)), temp)
        return [row_to_hit(came_up[i][1], came_up[i][0]) for i in picked]

    # ================================================================== queries
    async def episodes_between(self, since: float, until: float, limit: int = 200) -> list:
        rows = await self.k.db.fetchall(
            "SELECT * FROM mem_records WHERE kind='episodic' AND status IN ('active','pinned','stale') "
            "AND observed_at >= ? AND observed_at < ? ORDER BY observed_at ASC LIMIT ?",
            (float(since), float(until), int(limit)))
        return [row_to_hit(r) for r in rows]

    async def about_person(self, person_key: str, limit: int = 30, *, thread_id: Optional[str] = None,
                           present: Optional[Sequence[str]] = None, withheld: Optional[list] = None) -> list:
        """What I remember about a person. With ``thread_id`` (the space I'm about to speak in) / ``present``, only
        what everyone there may hear (INV-34): what B told me one-to-one about A never comes up with A, nor in a
        group; withheld contents go to ``withheld``."""
        scope = self.scope(thread_id, present) if (thread_id or present) else None
        n = int(limit) * (4 if scope is not None else 1)
        rows = await self.k.db.fetchall(
            "SELECT DISTINCT r.* FROM mem_ent e JOIN mem_records r ON r.id = e.record_id "
            "WHERE e.key = ? AND e.role IN ('person','told_by') AND r.status IN ('active','pinned','stale','pending') "
            "ORDER BY CASE r.kind WHEN 'relational' THEN 0 WHEN 'semantic' THEN 1 WHEN 'opinion' THEN 2 "
            "WHEN 'self' THEN 3 ELSE 4 END, r.importance DESC, r.observed_at DESC LIMIT ?",
            (person_key, n))
        hits = [row_to_hit(r) for r in rows]
        if scope is None:
            return hits
        ok, out = scope.split(hits)
        if withheld is not None:
            known = {norm(h.content) for h in ok}
            withheld.extend(h.content for h in out[:int(limit)] if norm(h.content) not in known
                            and h.content not in withheld)
        return ok[:int(limit)]

    async def upcoming(self, start: float, end: float, limit: int = 30) -> list:
        """Live records whose ``refers_to`` range overlaps [start, end) (plans, appointments, exams...)."""
        rows = await self.k.db.fetchall(
            "SELECT * FROM mem_records WHERE ref_start IS NOT NULL AND ref_start < ? "
            "AND COALESCE(ref_end, ref_start) > ? AND status IN ('active','pinned','stale','pending') "
            "ORDER BY importance DESC, ref_start ASC LIMIT ?", (float(end), float(start), int(limit)))
        return [row_to_hit(r) for r in rows]

    async def stats(self) -> dict:
        rows = await self.k.db.fetchall("SELECT kind, status, COUNT(*) AS n FROM mem_records GROUP BY kind, status")
        by_kind: dict = {}
        by_status: dict = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["n"]
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
        recheck = await self.k.db.fetchval("SELECT COUNT(*) FROM mem_recheck", default=0)

        def pct(xs, q):
            xs = sorted(xs)
            return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2) if xs else None

        return {"total": sum(by_kind.values()), "by_kind": by_kind, "by_status": by_status,
                "indexed": len(self.index) if self.index is not None else 0,
                "index_mb": round(self.index.nbytes() / 1e6, 1) if self.index is not None else 0,
                "recheck_queue": recheck, "entities": len(self._entity_names),
                "latency_ms": {t: {"p50": pct(v, 0.5), "p95": pct(v, 0.95), "n": len(v)} for t, v in self._lat.items()},
                **self.status()}

    # ================================================================== access / strength
    def _touch(self, ids: Iterable[str]) -> None:
        now = self.k.clock.now()
        for i in ids:
            e = self._access.get(i)
            if e is None:
                self._access[i] = [1, now]
            else:
                e[0] += 1
                e[1] = now
        if len(self._access) >= int(self.cfg["access_flush_max"]) and self._started:
            self._spawn(self.flush_access, "mem.access_flush_now")

    async def flush_access(self) -> int:
        """Apply batched access updates: access_count, last_access, strength (spacing bonus)."""
        if not self._access:
            return 0
        batch, self._access = self._access, {}
        gain = float(self.cfg["recall_gain"])
        ids = list(batch)

        def _tx(c):
            upd = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in c.execute(f"SELECT id, strength, last_access FROM mem_records WHERE id IN ({_qmarks(len(chunk))})",
                                   chunk):
                    n, ts = batch[r["id"]]
                    s2 = recall_strength(float(r["strength"]), float(r["last_access"]), ts, gain)
                    upd.append((int(n), ts, s2, r["id"]))
            c.executemany("UPDATE mem_records SET access_count = access_count + ?, "
                          "last_access = MAX(last_access, ?), strength = ? WHERE id = ?", upd)
            return len(upd)

        self._active += 1
        try:
            return await self.k.db.transaction(_tx)
        finally:
            self._active -= 1

    # ================================================================== truth maintenance
    async def invalidate(self, record_id: str, *, reason: str = "", replaced_by: Optional[str] = None,
                         valid_to: Optional[float] = None, max_depth: int = 3) -> dict:
        """Bi-temporal invalidation + dependency propagation (docs/03 §4.3): the record gets
        status invalid, valid_to and expired_at; dependents (derived_from edges) become ``stale``
        recursively up to ``max_depth`` levels and are queued for sleep recheck."""
        now = self.k.clock.now()

        def _tx(c):
            r = c.execute("SELECT status, meta FROM mem_records WHERE id=?", (record_id,)).fetchone()
            if r is None or r["status"] in ("invalid", "tombstoned"):
                return None
            meta = json.loads(r["meta"] or "{}")
            meta["invalidated"] = {"at": now, "reason": reason[:200], "replaced_by": replaced_by}
            c.execute("UPDATE mem_records SET status='invalid', valid_to=COALESCE(valid_to, ?), expired_at=?, meta=? "
                      "WHERE id=?", (valid_to if valid_to is not None else now, now,
                                     json.dumps(meta, ensure_ascii=False), record_id))
            c.execute("DELETE FROM mem_recheck WHERE record_id=?", (record_id,))
            visited = {record_id}
            frontier = [record_id]
            stale: list = []
            for depth in range(1, max_depth + 1):
                if not frontier:
                    break
                deps = c.execute(f"SELECT DISTINCT d.record_id, r.status FROM mem_deps d JOIN mem_records r "
                                 f"ON r.id = d.record_id WHERE d.depends_on IN ({_qmarks(len(frontier))})",
                                 frontier).fetchall()
                nxt = []
                for d in deps:
                    did = d["record_id"]
                    if did in visited:
                        continue
                    visited.add(did)
                    if d["status"] not in ("active", "pending", "stale", "pinned"):
                        continue
                    if d["status"] != "pinned":
                        c.execute("UPDATE mem_records SET status='stale' WHERE id=?", (did,))
                    c.execute("INSERT OR REPLACE INTO mem_recheck (record_id, reason, depth, queued_at, attempts) "
                              "VALUES (?,?,?,?,0)", (did, f"dependency:{record_id}", depth, now))
                    stale.append(did)
                    nxt.append(did)
                frontier = nxt
            return stale

        stale = await self.k.db.transaction(_tx)
        if stale is None:
            return {"invalidated": None, "stale": []}
        self.counters["invalidations"] += 1
        await self.k.bus.emit(EV.MEMORY_INVALIDATE, {"id": record_id, "reason": reason[:120], "replaced_by": replaced_by,
                                                     "stale": len(stale), "stale_ids": stale[:20]}, source="memory")
        return {"invalidated": record_id, "stale": stale}

    async def mark_conflict(self, a: str, b: str, reason: str = "") -> None:
        """Keep both records, cross-link them as conflicting and queue both for sleep review."""
        now = self.k.clock.now()

        def _tx(c):
            for x, y in ((a, b), (b, a)):
                r = c.execute("SELECT conflict_with FROM mem_records WHERE id=?", (x,)).fetchone()
                if r is None:
                    continue
                cw = json.loads(r["conflict_with"] or "[]")
                if y not in cw:
                    cw.append(y)
                c.execute("UPDATE mem_records SET conflict_with=? WHERE id=?", (json.dumps(cw), x))
                c.execute("INSERT OR REPLACE INTO mem_recheck (record_id, reason, depth, queued_at, attempts) "
                          "VALUES (?,?,0,?,0)", (x, f"conflict:{y}", now))

        await self.k.db.transaction(_tx)

    async def supersede(self, old_id: str, new_id_: str) -> None:
        """Old version replaced by a refined new version: status superseded + expired_at; dependents of
        the old version now also depend on the new one (so propagation follows the lineage)."""
        now = self.k.clock.now()

        def _tx(c):
            c.execute("UPDATE mem_records SET status='superseded', expired_at=? WHERE id=? AND status != 'pinned'",
                      (now, old_id))
            c.execute("INSERT OR IGNORE INTO mem_deps (depends_on, record_id) "
                      "SELECT ?, record_id FROM mem_deps WHERE depends_on=? AND record_id != ?", (new_id_, old_id, new_id_))
            c.execute("DELETE FROM mem_recheck WHERE record_id=?", (old_id,))

        await self.k.db.transaction(_tx)

    # ================================================================== entity merge (docs/22 §3.3)
    async def rekey_entity(self, old: str, new: str) -> None:
        """``old`` was merged into ``new``: mem_* rows and kv entries were rewritten by alive/world/rekey.py;
        move the caches (names, conversation buffers, precomputed bundles)."""
        rekey_dict(self._names, old, new)
        await self._rekey_buffers(old, new)
        for bundles in self._precomputed.values():
            for b in bundles or ():
                if isinstance(b, dict) and b.get("person_key") == old:
                    b["person_key"] = new

    # ================================================================== privacy
    async def forget_person(self, person_key: str) -> int:
        """Privacy deletion (docs/03 §8) — implemented as *physical deletion* rather than crypto-shredding:
        records about / told by the person (and records derived from them, depth <= 3) are deleted with
        their FTS entries, vectors, entity and dependency rows; the person's message payloads in the
        event log are overwritten with a redacted stub; a ``mem.forget`` tombstone (hashed key) is emitted.
        SQLite ``secure_delete`` is enabled for the deletion and the FTS index is optimized afterwards so
        deleted tokens do not linger in old segments."""
        pk = (person_key or "").strip()
        if not pk:
            return 0
        await self._forget_in_buffers(pk)     # first: wait for in-flight extraction of their lines
        self._names.pop(pk, None)
        now = self.k.clock.now()
        channel, _, sender = pk.partition(":")
        like_pk = f'%"{like_escape(pk)}"%'

        def _tx(c):
            try:
                c.execute("PRAGMA secure_delete=ON")
            except Exception:
                pass
            ids = {r[0] for r in c.execute("SELECT record_id FROM mem_ent WHERE key=? AND role IN ('person','told_by')",
                                           (pk,))}
            ids |= {r[0] for r in c.execute("SELECT id FROM mem_records WHERE told_by=? OR person_keys LIKE ? ESCAPE '\\'",
                                            (pk, like_pk))}
            frontier = list(ids)
            for _ in range(3):  # derived records (opinions about them etc.)
                if not frontier:
                    break
                nxt = []
                for i in range(0, len(frontier), 500):
                    ch = frontier[i:i + 500]
                    for r in c.execute(f"SELECT record_id FROM mem_deps WHERE depends_on IN ({_qmarks(len(ch))})", ch):
                        if r[0] not in ids:
                            ids.add(r[0])
                            nxt.append(r[0])
                frontier = nxt
            idl = list(ids)
            for i in range(0, len(idl), 500):
                ch = idl[i:i + 500]
                q = _qmarks(len(ch))
                c.execute(f"DELETE FROM mem_records WHERE id IN ({q})", ch)   # FTS rows via trigger
                c.execute(f"DELETE FROM mem_vec WHERE id IN ({q})", ch)
                c.execute(f"DELETE FROM mem_ent WHERE record_id IN ({q})", ch)
                c.execute(f"DELETE FROM mem_deps WHERE record_id IN ({q}) OR depends_on IN ({q})", ch + ch)
                c.execute(f"DELETE FROM mem_recheck WHERE record_id IN ({q})", ch)
                c.execute(f"DELETE FROM mem_preimages WHERE record_id IN ({q})", ch)
            c.execute("DELETE FROM mem_ent WHERE key=?", (pk,))
            # redact the person's messages in the event log
            redacted = 0
            try:
                pats = [like_pk]
                if sender:
                    pats.append(f'%"sender_id":"{like_escape(sender)}"%')
                cand = c.execute("SELECT id, type, payload FROM events WHERE (" +
                                 " OR ".join(["payload LIKE ? ESCAPE '\\'"] * len(pats)) + ")", pats).fetchall()
                stub = json.dumps({"redacted": True, "reason": "forget_person", "at": now})
                for r in cand:
                    if r["type"].startswith(("mem.", "grow.", "rt.")):
                        continue
                    try:
                        p = json.loads(r["payload"] or "{}")
                    except Exception:
                        p = {}
                    if _payload_mentions(p, pk, channel, sender):
                        c.execute("UPDATE events SET payload=? WHERE id=?", (stub, r["id"]))
                        redacted += 1
            except Exception as e:  # events table missing in exotic setups
                log.warning("event redaction failed: %s", e)
            try:
                c.execute("PRAGMA secure_delete=OFF")
            except Exception:
                pass
            return idl, redacted

        idl, redacted = await self.k.db.transaction(_tx)
        try:
            await self.k.db.transaction(lambda c: c.execute("INSERT INTO mem_fts(mem_fts) VALUES ('optimize')"))
        except Exception as e:
            log.warning("fts optimize after forget failed: %s", e)
        for i in idl:
            if self.index is not None:
                self.index.remove(i)
            self._access.pop(i, None)
        for d, bundles in list(self._precomputed.items()):
            self._precomputed[d] = [b for b in bundles if b.get("person_key") != pk
                                    and not set(b.get("hit_ids") or []) & set(idl)]
        await self._load_entity_names()
        try:
            day = self.k.clock.today()
            await self.k.db.kv_set("mem.precompute", day, self._precomputed.get(day, []), ts=now)
        except Exception:
            pass
        await self.k.bus.emit(EV.MEMORY_FORGET, {"person_hash": hashlib.sha256(pk.encode("utf-8")).hexdigest()[:16],
                                                 "records": len(idl), "events_redacted": redacted,
                                                 "method": "physical_delete"}, source="memory")
        log.info("forget_person: deleted %d records, redacted %d events", len(idl), redacted)
        return len(idl)

    # ================================================================== seed memories (INV-8)
    async def load_seed_memories(self) -> int:
        """Load ``<persona>/seed_memories/*`` once per file content (yaml/yml/json/jsonl/md/txt).
        Everything is written with source ``seeded`` (never semantic, never 'really experienced')."""
        pdir = getattr(self.k, "persona_dir", None) or self.k.cfg.get("persona_dir")
        if not pdir:
            return 0
        sdir = os.path.join(pdir, "seed_memories")
        files = await asyncio.get_running_loop().run_in_executor(None, _read_seed_files, sdir)
        if not files:
            return 0
        done = await self.k.db.kv_get("mem", "seeded", {}) or {}
        total = 0
        for name, digest, items in files:
            if done.get(name) == digest:
                continue
            batch = []
            for it in items:
                it = dict(it)
                batch.append({"kind": it.get("kind", "episodic"), "content": it.get("content", ""),
                              "importance": int(it.get("importance", 4) or 4), "entities": it.get("entities") or (),
                              "refers_to": it.get("refers_to"), "source": "seeded", "trust": Trust.SYSTEM,
                              "meta": {"seed_file": name}})
            total += len(await self.note_many(batch, emit=False))
            done[name] = digest
        await self.k.db.kv_set("mem", "seeded", done, ts=self.k.clock.now())
        if total:
            await self.k.bus.emit(EV.MEMORY_WRITE, {"count": total, "seeded": True}, source="memory")
        return total


def _payload_mentions(p: Any, pk: str, channel: str, sender: str) -> bool:
    if isinstance(p, dict):
        if p.get("person_key") == pk:
            return True
        if sender and str(p.get("sender_id", "")) == sender and (not p.get("channel") or p.get("channel") == channel):
            return True
        return any(_payload_mentions(v, pk, channel, sender) for v in p.values() if isinstance(v, (dict, list)))
    if isinstance(p, list):
        return any(_payload_mentions(v, pk, channel, sender) for v in p)
    return False


def _read_seed_files(sdir: str) -> list:
    import yaml
    out = []
    if not os.path.isdir(sdir):
        return out
    for name in sorted(os.listdir(sdir)):
        path = os.path.join(sdir, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        items: list = []
        try:
            if ext in (".yaml", ".yml", ".json"):
                data = yaml.safe_load(text) or []
                if isinstance(data, dict):
                    data = data.get("memories") or []
                items = [d if isinstance(d, dict) else {"content": str(d)} for d in data]
            elif ext == ".jsonl":
                items = [json.loads(l) for l in text.splitlines() if l.strip()]
            elif ext in (".md", ".txt"):
                for l in text.splitlines():
                    l = l.strip()
                    if not l or l.startswith("#"):
                        continue
                    items.append({"content": l.lstrip("-*• ").strip()})
        except Exception:
            log.warning("bad seed memory file %s", path)
            continue
        out.append((name, digest, [i for i in items if isinstance(i, dict) and str(i.get("content", "")).strip()]))
    return out
