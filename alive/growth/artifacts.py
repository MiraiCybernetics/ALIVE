"""L2 procedural artifacts (docs/06 §4): playbook entries and expressions (incl. jargon) sharing one
lifecycle  candidate -> quarantined -> active -> retired  (active -> quarantined on auto-demote).

Every change goes through ``_commit`` which appends an immutable row to ``grow_versions`` (parent
pointer, evidence ids, eval score) and moves ``head_vid`` (INV-9). ``rollback`` is a pointer switch to an
older version's snapshot; nothing is deleted. Merges are deterministic (counts summed, provenance
unioned, both parents recorded). Active artifacts are cached in memory with embeddings so prompt-time
retrieval needs no DB I/O.

Examples (observed original sentences) and their speakers are evidence, not part of the artifact
definition: they live only in the mutable row, never in version snapshots, so privacy deletion can
remove them without rewriting the append-only log.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from alive.core.types import EV, new_id

log = logging.getLogger("alive.growth")

TYPES = ("playbook", "expression")
STATUSES = ("candidate", "quarantined", "active", "retired")
PLAYBOOK_KINDS = ("strategy", "habit", "pitfall", "preference")
EXPRESSION_KINDS = ("style", "jargon")
_PRIVATE_META = ("speakers", "example_by")
SNAPSHOT_FIELDS = ("id", "type", "kind", "text", "situation", "tag", "scope", "jargon", "helpful", "harmful", "status",
                   "provenance", "source", "author", "version", "seen_count", "norm_key")


def norm_key(*parts: str) -> str:
    s = "|".join(unicodedata.normalize("NFKC", p or "").lower() for p in parts)
    return re.sub(r"[\s，。,.!！?？~～、…\"'“”‘’（）()【】\[\]]+", "", s)


def scope_class(scope: str) -> str:
    return (scope or "global").split(":", 1)[0]


@dataclass
class Artifact:
    id: str
    type: str
    kind: str
    text: str
    situation: str = ""
    tag: Optional[str] = None
    scope: str = "global"
    examples: list = field(default_factory=list)
    jargon: Optional[dict] = None
    helpful: int = 0
    harmful: int = 0
    status: str = "candidate"
    provenance: list = field(default_factory=list)
    source: Optional[str] = None
    author: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used: Optional[float] = None
    use_count: int = 0
    seen_count: int = 1
    version: int = 1
    head_vid: Optional[str] = None
    norm_key: Optional[str] = None
    meta: dict = field(default_factory=dict)

    def __getitem__(self, k: str) -> Any:
        return getattr(self, k)

    def get(self, k: str, default: Any = None) -> Any:
        return getattr(self, k, default)

    def to_dict(self) -> dict:
        return asdict(self)

    def snapshot(self) -> dict:
        d = {f: getattr(self, f) for f in SNAPSHOT_FIELDS}
        d["meta"] = {k: v for k, v in (self.meta or {}).items() if k not in _PRIVATE_META}
        return d

    @property
    def net(self) -> int:
        return self.helpful - self.harmful

    @property
    def embed_text(self) -> str:
        if self.type == "expression":
            term = (self.jargon or {}).get("term", "")
            return f"{self.situation} {term} {self.text}".strip()
        return f"{self.situation} {self.text}".strip()


def _add_examples(a: "Artifact", examples: Sequence[str], speakers: Sequence[str]) -> None:
    """Append observed sentences with their speaker attribution (meta.example_by, aligned with examples)."""
    by = list(a.meta.get("example_by") or [])
    by = (by + [""] * len(a.examples))[: len(a.examples)]
    for i, e in enumerate(examples or ()):
        if not e or e in a.examples:
            continue
        a.examples.append(e)
        by.append(speakers[i] if i < len(speakers) else (speakers[0] if speakers else ""))
    a.examples, by = a.examples[-5:], by[-5:]
    a.meta["example_by"] = by
    if speakers:
        a.meta["speakers"] = list(dict.fromkeys(list(a.meta.get("speakers") or []) + [x for x in speakers if x]))[-20:]


def _row_to_art(r) -> Artifact:
    def jl(v, d):
        try:
            return json.loads(v) if v else d
        except Exception:
            return d
    return Artifact(id=r["id"], type=r["type"], kind=r["kind"], text=r["text"], situation=r["situation"] or "",
                    tag=r["tag"], scope=r["scope"], examples=jl(r["examples"], []), jargon=jl(r["jargon"], None),
                    helpful=int(r["helpful"]), harmful=int(r["harmful"]), status=r["status"],
                    provenance=jl(r["provenance"], []), source=r["source"], author=r["author"],
                    created_at=float(r["created_at"]), updated_at=float(r["updated_at"]),
                    last_used=r["last_used"], use_count=int(r["use_count"]), seen_count=int(r["seen_count"]),
                    version=int(r["version"]), head_vid=r["head_vid"], norm_key=r["norm_key"],
                    meta=jl(r["meta"], {}))


_COLS = ("id", "type", "kind", "text", "situation", "tag", "scope", "examples", "jargon", "helpful", "harmful",
         "status", "provenance", "source", "author", "created_at", "updated_at", "last_used", "use_count",
         "seen_count", "version", "head_vid", "norm_key", "meta")


def _art_row(a: Artifact) -> tuple:
    return (a.id, a.type, a.kind, a.text, a.situation, a.tag, a.scope, json.dumps(a.examples, ensure_ascii=False),
            json.dumps(a.jargon, ensure_ascii=False) if a.jargon else None, a.helpful, a.harmful, a.status,
            json.dumps(a.provenance, ensure_ascii=False), a.source, a.author, a.created_at, a.updated_at,
            a.last_used, a.use_count, a.seen_count, a.version, a.head_vid, a.norm_key,
            json.dumps(a.meta, ensure_ascii=False))


RESCOPE_PREFIXES = ("person:", "channel:")
_UPSERT = "INSERT OR REPLACE INTO grow_artifacts (%s) VALUES (%s)" % (",".join(_COLS), ",".join("?" * len(_COLS)))


def rekey_scopes_sql(c, old: str, new: str, now: float) -> int:
    """Entity merge (alive/world/rekey.py, inside its write transaction): scopes ``person:<old>`` and
    ``channel:<old>`` (an old one-to-one space) become ``...:<new>``. A scope change is a change of the artifact,
    so each one appends an immutable version whose parent is the current head (INV-9); the old scope is kept in
    ``meta.rescoped_from`` so a later rollback does not resurrect the absorbed entity's scope."""
    n = 0
    for p in RESCOPE_PREFIXES:
        for r in c.execute("SELECT * FROM grow_artifacts WHERE scope = ?", (p + old,)).fetchall():
            a = _row_to_art(r)
            mx = c.execute("SELECT MAX(version) FROM grow_versions WHERE artifact_id = ?", (a.id,)).fetchone()[0]
            parent = a.head_vid
            a.version = int(mx or a.version) + 1
            a.meta["rescoped_from"] = list(dict.fromkeys(list(a.meta.get("rescoped_from") or []) + [a.scope]))[-8:]
            a.scope = p + new
            a.updated_at = now
            a.head_vid = vid = new_id("v")
            c.execute("INSERT INTO grow_versions (vid, artifact_id, version, parent_vid, merged_vid, ts, change, "
                      "snapshot, evidence, eval_score, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (vid, a.id, a.version, parent, None, now, "rescope",
                       json.dumps(a.snapshot(), ensure_ascii=False), "[]", None, f"entity merged: {old} -> {new}"[:300]))
            c.execute(_UPSERT, _art_row(a))
            n += 1
    return n


class ArtifactStore:
    def __init__(self, kernel, cfg: dict):
        self.k = kernel
        self.cfg = cfg
        self._active: dict = {}       # id -> Artifact (status active)
        self._vecs: dict = {}         # id -> np.ndarray
        self._usage: dict = {}        # id -> [count, last_ts]
        self._global_sel: Optional[list] = None

    # ------------------------------------------------------------------ loading / cache
    async def load(self) -> None:
        rows = await self.k.db.fetchall("SELECT * FROM grow_artifacts WHERE status='active'")
        arts = [_row_to_art(r) for r in rows]
        self._active = {a.id: a for a in arts}
        self._vecs = {}
        if arts:
            vecs = await self.k.embedder.embed([a.embed_text for a in arts])
            self._vecs = {a.id: v for a, v in zip(arts, vecs)}
        self._global_sel = None

    async def _cache(self, a: Artifact, change: str = "") -> None:
        if a.status == "active":
            self._active[a.id] = a
            self._vecs[a.id] = (await self.k.embedder.embed([a.embed_text]))[0]
        else:
            self._active.pop(a.id, None)
            self._vecs.pop(a.id, None)
        # the global selection is the cached prompt prefix: only membership/text changes recompute it,
        # counter bumps during the day do not (docs/06 §4.3 — the prefix changes once per sleep)
        if a.type == "playbook" and a.scope == "global" and change not in ("counters", "seen", "examples"):
            self._global_sel = None

    def active(self, type_: Optional[str] = None, scopes: Optional[Iterable[str]] = None) -> list:
        sc = set(scopes) if scopes is not None else None
        return [a for a in self._active.values() if (type_ is None or a.type == type_)
                and (sc is None or a.scope in sc)]

    def vec(self, aid: str) -> Optional[np.ndarray]:
        return self._vecs.get(aid)

    def global_selection(self, slots: int) -> list:
        """Stable, cue-independent choice of global playbook entries for the cached prompt prefix:
        best by (net helpful, use) then ordered by id so the text is byte-stable between sleeps."""
        if self._global_sel is None or len(self._global_sel) != min(slots, len(self.active("playbook", ["global"]))):
            g = self.active("playbook", ["global"])
            g.sort(key=lambda a: (-a.net, -a.use_count, a.created_at, a.id))
            self._global_sel = sorted(g[:slots], key=lambda a: a.id)
        return list(self._global_sel)

    # ------------------------------------------------------------------ queries
    async def get(self, aid: str) -> Optional[Artifact]:
        r = await self.k.db.fetchone("SELECT * FROM grow_artifacts WHERE id=?", (aid,))
        return _row_to_art(r) if r else None

    async def list(self, *, type_: Optional[str] = None, status: Optional[str] = None,
                   scope: Optional[str] = None, limit: int = 1000) -> list:
        where, params = [], []
        if type_:
            where.append("type=?")
            params.append(type_)
        if status:
            where.append("status=?")
            params.append(status)
        if scope:
            where.append("scope=?")
            params.append(scope)
        sql = "SELECT * FROM grow_artifacts" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY created_at, id LIMIT ?"
        rows = await self.k.db.fetchall(sql, params + [int(limit)])
        return [_row_to_art(r) for r in rows]

    async def find_key(self, type_: str, key: str, scope: Optional[str] = None) -> Optional[Artifact]:
        if scope is None:
            r = await self.k.db.fetchone("SELECT * FROM grow_artifacts WHERE type=? AND norm_key=? AND status != 'retired' "
                                         "ORDER BY created_at LIMIT 1", (type_, key))
        else:
            r = await self.k.db.fetchone("SELECT * FROM grow_artifacts WHERE type=? AND norm_key=? AND scope=? "
                                         "AND status != 'retired' LIMIT 1", (type_, key, scope))
        return _row_to_art(r) if r else None

    async def find_tag(self, tag: str, scope: Optional[str] = None) -> Optional[Artifact]:
        sql = "SELECT * FROM grow_artifacts WHERE type='playbook' AND tag=? AND status != 'retired'"
        params: list = [tag]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        r = await self.k.db.fetchone(sql + " ORDER BY created_at LIMIT 1", params)
        return _row_to_art(r) if r else None

    async def history(self, aid: str) -> list:
        rows = await self.k.db.fetchall("SELECT * FROM grow_versions WHERE artifact_id=? ORDER BY ts, version", (aid,))
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot"] = json.loads(d["snapshot"])
            d["evidence"] = json.loads(d["evidence"] or "[]")
            out.append(d)
        return out

    # ------------------------------------------------------------------ versioned writes
    async def _commit(self, a: Artifact, change: str, *, evidence: Sequence[str] = (), eval_score: Optional[float] = None,
                      reason: str = "", merged_vid: Optional[str] = None, new: bool = False) -> Artifact:
        now = self.k.clock.now()
        parent = a.head_vid
        vid = new_id("v")
        if not new:
            mx = await self.k.db.fetchval("SELECT MAX(version) FROM grow_versions WHERE artifact_id=?", (a.id,),
                                          default=None)
            a.version = int(mx or a.version) + 1
        a.updated_at = now
        a.head_vid = vid
        snap = json.dumps(a.snapshot(), ensure_ascii=False)
        row = _art_row(a)
        vrow = (vid, a.id, a.version, parent, merged_vid, now, change, snap,
                json.dumps(list(dict.fromkeys(evidence)), ensure_ascii=False), eval_score, reason[:300])

        def _tx(c):
            c.execute("INSERT INTO grow_versions (vid, artifact_id, version, parent_vid, merged_vid, ts, change, snapshot,"
                      " evidence, eval_score, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vrow)
            c.execute("INSERT OR REPLACE INTO grow_artifacts (%s) VALUES (%s)" % (",".join(_COLS), ",".join("?" * len(_COLS))),
                      row)

        await self.k.db.transaction(_tx)
        await self._cache(a, change)
        if change not in ("counters", "seen", "usage", "examples"):
            await self.k.bus.emit(EV.LEARN_CHANGE, {"layer": "L2", "artifact": a.id, "type": a.type, "change": change,
                                                    "status": a.status, "version": a.version, "vid": vid,
                                                    "eval_score": eval_score, "reason": reason[:120]}, source="growth")
        return a

    async def create(self, type_: str, text: str, *, kind: str, situation: str = "", scope: str = "global",
                     tag: Optional[str] = None, examples: Sequence[str] = (), jargon: Optional[dict] = None,
                     provenance: Sequence[str] = (), source: str = "learner", author: str = "self",
                     status: str = "candidate", meta: Optional[dict] = None, speakers: Sequence[str] = (),
                     dedupe: bool = True) -> tuple:
        """Create a new artifact (version 1) or, if an equivalent one exists in the same scope, record the
        re-observation on it. Returns (artifact, created: bool)."""
        if type_ not in TYPES:
            raise ValueError(f"unknown artifact type {type_}")
        text = (text or "").strip()
        situation = (situation or "").strip()
        key = norm_key(situation, text) if type_ == "playbook" or kind != "jargon" else \
            norm_key("jargon", (jargon or {}).get("term", text))
        if dedupe:
            ex = await self.find_key(type_, key, scope)
            if ex is not None:
                return await self.observe_again(ex, examples=examples, speakers=speakers, provenance=provenance), False
        now = self.k.clock.now()
        m = dict(meta or {})
        m["seen_days"] = [self.k.clock.today()]
        a = Artifact(id=new_id("a"), type=type_, kind=kind, text=text, situation=situation, tag=tag, scope=scope,
                     jargon=jargon, status=status if status in STATUSES else "candidate",
                     provenance=list(dict.fromkeys(provenance)), source=source, author=author, created_at=now,
                     updated_at=now, norm_key=key, meta=m)
        _add_examples(a, list(examples), list(speakers))
        await self._commit(a, "create", evidence=provenance, reason=source, new=True)
        return a, True

    async def observe_again(self, a: Artifact, *, examples: Sequence[str] = (), speakers: Sequence[str] = (),
                            provenance: Sequence[str] = ()) -> Artifact:
        a.seen_count += 1
        _add_examples(a, list(examples), list(speakers))
        for p in provenance:
            if p not in a.provenance:
                a.provenance.append(p)
        days = set(a.meta.get("seen_days") or [])
        days.add(self.k.clock.today())
        a.meta["seen_days"] = sorted(days)[-30:]
        return await self._commit(a, "seen", evidence=provenance)

    async def set_status(self, aid: str, status: str, *, reason: str = "", evidence: Sequence[str] = (),
                         eval_score: Optional[float] = None, change: Optional[str] = None) -> Optional[Artifact]:
        a = await self.get(aid)
        if a is None or status not in STATUSES or a.status == status:
            return a
        old = a.status
        a.status = status
        if status == "active":
            a.meta["promoted_at"] = self.k.clock.now()
        if status == "quarantined":
            a.meta["quarantined_at"] = self.k.clock.now()
        ch = change or {("quarantined", "active"): "promote", ("active", "quarantined"): "demote",
                        ("candidate", "quarantined"): "quarantine"}.get((old, status), "status")
        if status == "retired":
            ch = change or "retire"
        a.meta["status_reason"] = reason[:200]
        return await self._commit(a, ch, evidence=evidence, eval_score=eval_score, reason=reason)

    async def edit(self, aid: str, text: str, *, situation: Optional[str] = None, evidence: Sequence[str] = (),
                   reason: str = "edit", eval_score: Optional[float] = None) -> Optional[Artifact]:
        a = await self.get(aid)
        if a is None or not (text or "").strip():
            return a
        a.text = text.strip()
        if situation is not None:
            a.situation = situation.strip()
        a.norm_key = norm_key(a.situation, a.text) if a.kind != "jargon" else a.norm_key
        return await self._commit(a, "edit", evidence=evidence, reason=reason, eval_score=eval_score)

    async def add_counts(self, aid: str, *, helpful: int = 0, harmful: int = 0, evidence_id: str = "") -> Optional[Artifact]:
        """Evidence-driven counter update + auto-demote (harmful > helpful and harmful >= 2)."""
        a = await self.get(aid)
        if a is None or (helpful == 0 and harmful == 0):
            return a
        a.helpful += int(helpful)
        a.harmful += int(harmful)
        if evidence_id and evidence_id not in a.provenance:
            a.provenance.append(evidence_id)
        a = await self._commit(a, "counters", evidence=[evidence_id] if evidence_id else [])
        thr = int(self.cfg["auto_demote"]["min_harmful"])
        if a.status == "active" and a.harmful > a.helpful and a.harmful >= thr:
            a = await self.set_status(a.id, "quarantined", reason=f"auto-demote: harmful {a.harmful} > helpful {a.helpful}",
                                      evidence=[evidence_id] if evidence_id else [])
            if a is not None:
                a.meta["needs_review"] = True
        return a

    async def merge(self, keep_id: str, drop_id: str, *, text: Optional[str] = None,
                    reason: str = "near-duplicate") -> Optional[Artifact]:
        """Deterministic merge: survivor keeps the sum of both counters, both provenances and examples, and
        a version whose parent is its own head and whose merged_vid is the other's head. The other is retired."""
        a, b = await self.get(keep_id), await self.get(drop_id)
        if a is None or b is None or a.id == b.id or a.type != b.type:
            return a
        b_head = b.head_vid
        a.helpful += b.helpful
        a.harmful += b.harmful
        a.seen_count += b.seen_count
        a.use_count += b.use_count
        a.provenance = list(dict.fromkeys(a.provenance + b.provenance))
        bby = list(b.meta.get("example_by") or [])
        _add_examples(a, b.examples, (bby + [""] * len(b.examples))[: len(b.examples)])
        sp = list(dict.fromkeys(list(a.meta.get("speakers") or []) + list(b.meta.get("speakers") or [])))
        if sp:
            a.meta["speakers"] = sp[-20:]
        a.meta["seen_days"] = sorted(set(a.meta.get("seen_days") or []) | set(b.meta.get("seen_days") or []))[-30:]
        a.meta["merged_from"] = list(dict.fromkeys(list(a.meta.get("merged_from") or []) + [b.id]))
        if text and text.strip():
            a.text = text.strip()
        a = await self._commit(a, "merge", evidence=b.provenance, reason=f"{reason}: {b.id}", merged_vid=b_head)
        b.meta["merged_into"] = a.id
        b.status = "retired"
        await self._commit(b, "retire", reason=f"merged into {a.id}")
        return a

    async def rollback(self, aid: str, vid: str) -> Optional[Artifact]:
        """INV-9 rollback: switch the head pointer to version ``vid`` and rematerialise its snapshot.
        History is untouched; the next change will branch from ``vid``."""
        cur = await self.get(aid)
        r = await self.k.db.fetchone("SELECT * FROM grow_versions WHERE vid=? AND artifact_id=?", (vid, aid))
        if cur is None or r is None:
            return None
        snap = json.loads(r["snapshot"])
        prev_head = cur.head_vid
        scope_now, rescoped = cur.scope, list((cur.meta or {}).get("rescoped_from") or [])
        for f in SNAPSHOT_FIELDS:
            if f in snap and f != "id":
                setattr(cur, f, snap[f])
        meta = dict(snap.get("meta") or {})
        for k in _PRIVATE_META:
            if k in cur.meta:
                meta[k] = cur.meta[k]
        if rescoped:   # entity merges are not undone by a rollback: the absorbed entity's scope is gone
            meta["rescoped_from"] = rescoped
            if cur.scope in rescoped:
                cur.scope = scope_now
        cur.meta = meta
        cur.head_vid = vid
        cur.updated_at = self.k.clock.now()
        row = _art_row(cur)
        await self.k.db.transaction(lambda c: c.execute(
            "INSERT OR REPLACE INTO grow_artifacts (%s) VALUES (%s)" % (",".join(_COLS), ",".join("?" * len(_COLS))), row))
        await self._cache(cur)
        await self.k.bus.emit(EV.LEARN_CHANGE, {"layer": "L2", "artifact": aid, "type": cur.type, "change": "rollback",
                                                "from_vid": prev_head, "to_vid": vid, "status": cur.status,
                                                "version": cur.version}, source="growth")
        return cur

    async def enforce_caps(self, type_: str, scope: str) -> list:
        """Retire the weakest active artifacts of a scope above its cap (global 60, person 15, ...)."""
        caps = self.cfg["caps"]
        cap = int(caps.get(scope_class(scope), caps.get("default", 30)))
        act = [a for a in self.active(type_, [scope])]
        if len(act) <= cap:
            return []
        act.sort(key=lambda a: (a.net, a.last_used or a.created_at, a.created_at))
        retired = []
        for a in act[: len(act) - cap]:
            await self.set_status(a.id, "retired", reason=f"capacity cap {cap} for {scope}")
            retired.append(a.id)
        return retired

    # ------------------------------------------------------------------ usage
    def mark_used(self, ids: Iterable[str]) -> None:
        now = self.k.clock.now()
        for i in ids:
            e = self._usage.setdefault(i, [0, now])
            e[0] += 1
            e[1] = now

    async def flush_usage(self) -> int:
        """Usage counters are bookkeeping, not behaviour changes: written in place (no version row)."""
        if not self._usage:
            return 0
        batch, self._usage = self._usage, {}
        rows = [(n, ts, i) for i, (n, ts) in batch.items()]
        await self.k.db.executemany("UPDATE grow_artifacts SET use_count = use_count + ?, last_used = ? WHERE id = ?",
                                    rows)
        for n, ts, i in rows:
            a = self._active.get(i)
            if a is not None:
                a.use_count += n
                a.last_used = ts
        return len(rows)

    # ------------------------------------------------------------------ entity merge (docs/22 §3.3)
    async def refresh_entity(self, old: str, new: str) -> int:
        """After ``rekey_scopes_sql`` and the meta rewrite: re-read cached active artifacts that referred to
        ``old`` (scope, speakers) and update the cached objects in place (head_vid / version moved too)."""
        scopes = {p + old for p in RESCOPE_PREFIXES}
        ids = [a.id for a in self._active.values()
               if a.scope in scopes or old in (a.meta.get("speakers") or []) or old in (a.meta.get("example_by") or [])]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        rows = await self.k.db.fetchall(f"SELECT * FROM grow_artifacts WHERE id IN ({q})", ids)
        names = [f.name for f in fields(Artifact)]
        for r in rows:
            fresh = _row_to_art(r)
            a = self._active.get(fresh.id)
            if a is None:
                continue
            if fresh.status != "active":
                self._active.pop(fresh.id, None)
                self._vecs.pop(fresh.id, None)
                continue
            for f in names:
                setattr(a, f, getattr(fresh, f))
        return len(rows)

    # ------------------------------------------------------------------ privacy
    async def remove_speaker(self, person_key: str) -> int:
        """Drop examples observed from a person (privacy). Examples are not part of version snapshots."""
        rows = await self.k.db.fetchall("SELECT * FROM grow_artifacts WHERE meta LIKE ?", (f'%"{person_key}"%',))
        n = 0
        for r in rows:
            a = _row_to_art(r)
            sp = list(a.meta.get("speakers") or [])
            by = list(a.meta.get("example_by") or [])
            by = (by + [""] * len(a.examples))[: len(a.examples)]
            if person_key not in sp and person_key not in by:
                continue
            a.meta["speakers"] = [x for x in sp if x != person_key]
            keep = [(e, b) for e, b in zip(a.examples, by) if b != person_key]
            a.examples = [e for e, _ in keep]
            a.meta["example_by"] = [b for _, b in keep]
            await self.k.db.execute("UPDATE grow_artifacts SET examples=?, meta=? WHERE id=?",
                                    (json.dumps(a.examples, ensure_ascii=False), json.dumps(a.meta, ensure_ascii=False),
                                     a.id))
            if a.id in self._active:
                self._active[a.id] = a
            n += 1
        return n
