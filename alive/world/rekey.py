"""Entity re-keying after an identity merge (docs/22 §3.3, INV-27).

When ``World.link`` (or a ``world.identities`` declaration at start) folds entity ``old`` into ``new``, every
piece of state stored under ``old`` has to move to ``new``; otherwise the merged person is still two people.

Where entity ids live in SQL is declared as data, centrally (bottom of this file), with
``register_entity_column(table, column, mode, ...)``:

    TEXT       the value is an entity id                         soc_messages.sender_key
    SPACE      a space / thread id (a group, or a one-to-one     soc_messages.thread_id
               space, whose id IS the counterpart's person id)
    PREFIX     scope strings: ``prefix + id`` (or, with ``sep``,  grow_evidence.about "interpretation:<id>",
               ``prefix + id + sep + rest``)                      core_move_stats.key "p:<id>|genre|need|move"
    JSON_LIST  a JSON list of ids                                 mem_records.person_keys
    JSON       any JSON document: string values equal to the id   mot_intentions.trigger, kv values
               and dict keys are rewritten (dict-key collisions
               are combined with ``json_merge(old_val, cur_val)``)

Columns that belong to a PRIMARY KEY or UNIQUE index are detected from the schema. A row whose rewritten key
already exists is merged into the existing row with ``on_conflict`` and then deleted:

    None / "coalesce"   keep the surviving row, fill its NULLs from the absorbed row (default)
    "keep"              keep the surviving row as is
    "newest:<col>"      keep whichever row has the larger <col>
    {col: op}           per column: sum | max | min | or | and | union (JSON) | old | new | coalesce |
                        by:<col> (value from the row with the larger <col>) | callable(old, cur); "*" = default
    callable            fn(old_row: dict, cur_row: dict) -> {col: value} for the surviving row

The second SQL pass (after the hooks) catches rows written under the old id while caches were switching over.
A conflict there uses ``late_conflict``: by default "keep" (the surviving row wins and the late row is dropped:
it is normally a stale full-state snapshot from a cache the hook has since merged and re-saved, and merging it
again would count it twice); "same" reuses ``on_conflict`` for tables written as increments (core_renqing).

Anything that is not a column rewrite (e.g. growth's versioned scope change, INV-9) registers a custom
statement function with ``register_entity_sql(label, "module:function")``; it runs on the writer connection
inside the same transaction: ``fn(conn, old, new, now) -> rows changed``.

``rekey(kernel, old, new)`` runs every registration in ONE write job on the single writer thread (one
transaction, all or nothing), then awaits the in-memory hooks (``World.rekey_entity``, the optional
``Subsystem.rekey_entity`` of every built subsystem, and per-kernel ``register_entity_hook`` callbacks), runs
the SQL pass once more for rows written with the old id while caches were switching over, and emits
``world.rekeyed`` (causal parent: the ``world.merged`` event) with the counts per table. Every step is
idempotent: running it again finds nothing under the old id and moves nothing.

The event log keeps its payloads verbatim (history); only its indexed ``thread_id`` column is re-pointed, so
space-scoped queries see the whole history of the merged space.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger("alive.world.rekey")

MERGED = "world.merged"
REKEYED = "world.rekeyed"
SELF = "self"

TEXT, SPACE, PREFIX, JSON_LIST, JSON = "text", "space", "prefix", "json_list", "json"
MODES = (TEXT, SPACE, PREFIX, JSON_LIST, JSON)
_SCALAR = (TEXT, SPACE, PREFIX)
_ROWID = "__rk_rowid"


# ============================================================================ value rewriting (shared with hooks)
def rekey_str(s: Any, old: str, new: str, prefixes: Sequence[str] = ("",), sep: Optional[str] = None) -> Optional[str]:
    """The rewritten string, or None when ``s`` does not refer to ``old``."""
    if not isinstance(s, str):
        return None
    for p in prefixes:
        head = p + old
        if s == head:
            return p + new
        if sep is not None and s.startswith(head + sep):
            return p + new + s[len(head):]
    return None


def merge_json(old: Any, cur: Any) -> Any:
    """Default combination of two values that end up under the same key: the surviving value wins, dicts are
    merged (surviving keys win), lists are unioned (surviving items first), missing values are filled."""
    if cur is None:
        return old
    if old is None:
        return cur
    if isinstance(old, dict) and isinstance(cur, dict):
        out = dict(old)
        out.update(cur)
        return out
    if isinstance(old, list) and isinstance(cur, list):
        return list(cur) + [x for x in old if x not in cur]
    return cur


def _rewrite(obj: Any, fn: Callable[[str], Optional[str]], merge: Callable[[Any, Any], Any],
             dedupe: bool = False) -> tuple:
    """(new_obj, changed). Strings are mapped by ``fn``; dict keys too (collisions combined with ``merge``).
    ``dedupe`` (top-level list of ids only): a rewritten id that is already in the list is dropped. Nested
    lists are never deduplicated (they may be aligned with other lists, e.g. growth's meta.example_by)."""
    if isinstance(obj, str):
        r = fn(obj)
        return (r, True) if r is not None else (obj, False)
    if isinstance(obj, list):
        out, made, changed = [], set(), False
        for x in obj:
            y, c = _rewrite(x, fn, merge)
            if c:
                changed = True
                if isinstance(y, str):
                    made.add(y)
            out.append(y)
        if dedupe and made:
            seen: set = set()
            ded = []
            for y in out:
                if isinstance(y, str) and y in made:
                    if y in seen:
                        continue
                    seen.add(y)
                ded.append(y)
            out = ded
        return (out, True) if changed else (obj, False)
    if isinstance(obj, dict):
        keep: dict = {}
        moved: list = []
        changed = False
        for k, v in obj.items():
            v2, c = _rewrite(v, fn, merge)
            changed = changed or c
            k2 = fn(k) if isinstance(k, str) else None
            if k2 is None:
                keep[k] = v2
            else:
                moved.append((k2, v2))
                changed = True
        for k2, v2 in moved:
            keep[k2] = merge(v2, keep[k2]) if k2 in keep else v2
        return (keep, True) if changed else (obj, False)
    return obj, False


def rekey_obj(obj: Any, old: str, new: str, *, merge: Optional[Callable[[Any, Any], Any]] = None,
              prefixes: Sequence[str] = ("",), sep: Optional[str] = None, dedupe: bool = False) -> Any:
    """Deep copy-on-write rewrite of a JSON-like object (for in-memory hooks); unchanged objects are returned
    as is. ``dedupe``: ``obj`` is a list of ids, drop duplicates created by the rewrite."""
    return _rewrite(obj, lambda s: rekey_str(s, old, new, prefixes, sep), merge or merge_json, dedupe)[0]


def rekey_dict(d: dict, old: str, new: str, merge: Optional[Callable[[Any, Any], Any]] = None) -> bool:
    """In-place: move ``d[old]`` to ``d[new]``; if both exist, ``d[new] = merge(old_val, new_val)``
    (default: keep the surviving value). Returns True if something moved."""
    if old not in d:
        return False
    v = d.pop(old)
    if new in d:
        d[new] = merge(v, d[new]) if merge is not None else d[new]
    else:
        d[new] = v
    return True


# ============================================================================ registry
@dataclass(frozen=True)
class EntityColumn:
    table: str
    column: str
    mode: str = TEXT
    prefixes: tuple = ("",)
    sep: Optional[str] = None
    where: str = ""                   # static, trusted SQL filter (e.g. "ns='dialogue'")
    on_conflict: Any = None
    json_merge: Optional[Callable] = None
    label: str = ""
    owner: str = ""
    late_conflict: Any = "keep"       # conflict strategy in the second pass; "same" = on_conflict

    @property
    def key(self) -> tuple:
        return (self.table, self.column, self.mode, self.prefixes, self.sep, self.where)

    @property
    def name(self) -> str:
        return self.label or f"{self.table}.{self.column}"


@dataclass(frozen=True)
class EntitySQL:
    label: str
    fn: Any                           # callable(conn, old, new, now) -> int, or "module:function"
    tables: tuple = ()                # skipped while any of these tables does not exist
    owner: str = ""

    @property
    def key(self) -> tuple:
        return ("sql", self.label)

    @property
    def name(self) -> str:
        return self.label


_REGISTRY: list = []


def _add(spec) -> Any:
    for i, s in enumerate(_REGISTRY):
        if s.key == spec.key:
            _REGISTRY[i] = spec
            return spec
    _REGISTRY.append(spec)
    return spec


def register_entity_column(table: str, column: str, mode: str = TEXT, *, prefixes: Optional[Sequence[str]] = None,
                           sep: Optional[str] = None, where: str = "", on_conflict: Any = None,
                           json_merge: Optional[Callable] = None, label: str = "", owner: str = "",
                           late_conflict: Any = "keep") -> EntityColumn:
    """Declare that ``table.column`` holds entity ids (see module docstring for modes and merge strategies).
    Registrations run in order; register key columns before JSON columns of the same table. Idempotent."""
    if mode not in MODES:
        raise ValueError(f"unknown rekey mode {mode!r}")
    if mode == PREFIX and not prefixes:
        raise ValueError("PREFIX mode needs prefixes")
    pf = tuple(prefixes) if prefixes else ("",)
    return _add(EntityColumn(table, column, mode, pf, sep, where, on_conflict, json_merge, label, owner,
                             late_conflict))


def register_entity_sql(label: str, fn: Any, *, tables: Sequence[str] = (), owner: str = "") -> EntitySQL:
    """Custom statements run inside the rekey transaction: ``fn(conn, old, new, now) -> rows changed``.
    ``fn`` may be a ``"module:function"`` string, resolved when a merge happens."""
    return _add(EntitySQL(label, fn, tuple(tables), owner))


def registry() -> list:
    return list(_REGISTRY)


def register_entity_hook(kernel, fn: Callable[[str, str], Any]) -> None:
    """Per-kernel in-memory hook ``(async) fn(old, new)``, awaited after the SQL rewrite. Subsystems can instead
    override ``Subsystem.rekey_entity`` (alive/interfaces.py), which is called the same way."""
    hooks = kernel.__dict__.setdefault("_entity_hooks", [])
    if fn not in hooks:
        hooks.append(fn)


# ============================================================================ SQL execution (writer thread)
@dataclass
class _TInfo:
    columns: set
    pk: list
    uniques: list
    rowid: bool


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_info(c: sqlite3.Connection, t: str) -> _TInfo:
    rows = c.execute(f"PRAGMA table_info({_q(t)})").fetchall()
    cols = {r[1] for r in rows}
    pk = [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])]
    uniques: list = [tuple(pk)] if pk else []
    for ix in c.execute(f"PRAGMA index_list({_q(t)})").fetchall():
        if not ix[2]:
            continue
        ic = tuple(r[2] for r in c.execute(f"PRAGMA index_info({_q(ix[1])})").fetchall())
        if ic and None not in ic and ic not in uniques:
            uniques.append(ic)
    try:
        c.execute(f"SELECT rowid FROM {_q(t)} LIMIT 0")
        has_rowid = True
    except sqlite3.OperationalError:
        has_rowid = False
    return _TInfo(cols, pk, uniques, has_rowid)


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _match(s: EntityColumn, old: str) -> tuple:
    """Candidate filter (SQL, params) for rows of ``s`` that may refer to ``old``."""
    col = _q(s.column)
    parts, params = [], []
    for p in s.prefixes:
        head = p + old
        if s.mode in _SCALAR:
            parts.append(f"{col} = ?")
            params.append(head)
            if s.sep is not None:
                h = head + s.sep
                parts.append(f"substr({col}, 1, ?) = ?")
                params += [len(h), h]
        else:   # JSON: the id as a quoted JSON string, in both encodings (ensure_ascii on / off)
            forms = {'"' + head + '"', json.dumps(head)}
            if s.sep is not None:
                forms |= {'"' + head + s.sep, json.dumps(head + s.sep)[:-1]}
            for f in sorted(forms):
                parts.append(f"{col} LIKE ? ESCAPE '\\'")
                params.append("%" + _like(f) + "%")
    return " OR ".join(parts), params


def _transform(s: EntityColumn, v: Any, old: str, new: str) -> Optional[str]:
    if v is None:
        return None
    if s.mode in _SCALAR:
        return rekey_str(v, old, new, s.prefixes, s.sep)
    try:
        obj = json.loads(v)
    except (TypeError, ValueError):
        return None
    obj2, changed = _rewrite(obj, lambda x: rekey_str(x, old, new, s.prefixes, s.sep), s.json_merge or merge_json,
                             dedupe=(s.mode == JSON_LIST))
    return json.dumps(obj2, ensure_ascii=False) if changed else None


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("-inf")


def _jload(x: Any) -> Any:
    if x is None or isinstance(x, (dict, list)):
        return x
    try:
        return json.loads(x)
    except (TypeError, ValueError):
        return None


def _merge_value(op: Any, k: str, old: dict, cur: dict) -> Any:
    a, b = old.get(k), cur.get(k)
    if callable(op):
        return op(a, b)
    if op in ("coalesce", None):
        return b if b is not None else a
    if op in ("new", "keep"):
        return b
    if op == "old":
        return a if a is not None else b
    if a is None or b is None:
        return b if b is not None else a
    if op == "sum":
        return a + b
    if op == "max":
        return max(a, b)
    if op == "min":
        return min(a, b)
    if op == "or":
        return int(bool(a) or bool(b))
    if op == "and":
        return int(bool(a) and bool(b))
    if op == "union":
        ja, jb = _jload(a), _jload(b)
        if ja is None or jb is None:
            return b
        return json.dumps(merge_json(ja, jb), ensure_ascii=False)
    if isinstance(op, str) and op.startswith("by:"):
        tc = op[3:]
        return a if _num(old.get(tc)) > _num(cur.get(tc)) else b
    raise ValueError(f"unknown merge op {op!r} for column {k}")


def _merge_rows(strategy: Any, old: dict, cur: dict, protected: set) -> dict:
    cols = [k for k in old if k not in protected]
    if strategy == "keep":
        return {}
    if strategy == "replace":
        return {k: old[k] for k in cols}
    if isinstance(strategy, str) and strategy.startswith("newest:"):
        tc = strategy.split(":", 1)[1]
        return {k: old[k] for k in cols} if _num(old.get(tc)) > _num(cur.get(tc)) else {}
    if callable(strategy):
        out = strategy(dict(old), dict(cur)) or {}
        return {k: v for k, v in out.items() if k not in protected and k in cur}
    spec = strategy if isinstance(strategy, dict) else {}
    out = {}
    for k in cols:
        v = _merge_value(spec.get(k, spec.get("*", "coalesce")), k, old, cur)
        if v != cur.get(k):
            out[k] = v
    return out


def _ident(info: _TInfo, row: dict) -> tuple:
    if info.rowid:
        return "rowid = ?", [row[_ROWID]]
    return " AND ".join(f"{_q(x)} IS ?" for x in info.pk), [row[x] for x in info.pk]


def _apply_column(c: sqlite3.Connection, s: EntityColumn, info: _TInfo, old: str, new: str,
                  late: bool = False) -> int:
    t, col = _q(s.table), _q(s.column)
    w = f" AND ({s.where})" if s.where else ""
    uniq = [u for u in info.uniques if s.column in u]
    if s.mode in _SCALAR and not uniq:          # plain column: set-based UPDATE, no per-row work
        n = 0
        for p in s.prefixes:
            n += c.execute(f"UPDATE {t} SET {col} = ? WHERE {col} = ?{w}", (p + new, p + old)).rowcount
            if s.sep is not None:
                ho, hn = p + old + s.sep, p + new + s.sep
                n += c.execute(f"UPDATE {t} SET {col} = ? || substr({col}, ?) WHERE substr({col}, 1, ?) = ?{w}",
                               (hn, len(ho) + 1, len(ho), ho)).rowcount
        return n
    sel = f"rowid AS {_ROWID}, *" if info.rowid else "*"
    msql, mparams = _match(s, old)
    rows = c.execute(f"SELECT {sel} FROM {t} WHERE ({msql}){w}", mparams).fetchall()
    protected = {s.column, _ROWID, *info.pk}
    for u in uniq:
        protected.update(u)
    n = 0
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        v2 = _transform(s, d.get(s.column), old, new)
        if v2 is None:
            continue
        idw, idp = _ident(info, d)
        target = None
        for u in uniq:
            q = " AND ".join(f"{_q(x)} IS ?" for x in u)
            for cand in c.execute(f"SELECT {sel} FROM {t} WHERE {q}", [v2 if x == s.column else d[x] for x in u]):
                cd = {k: cand[k] for k in cand.keys()}
                if _ident(info, cd)[1] != idp:
                    target = cd
                    break
            if target is not None:
                break
        if target is None:
            c.execute(f"UPDATE {t} SET {col} = ? WHERE {idw}", [v2] + idp)
        else:
            strategy = s.on_conflict if not late or s.late_conflict == "same" else s.late_conflict
            upd = _merge_rows(strategy, d, target, protected)
            if upd:
                tw, tp = _ident(info, target)
                sets = ", ".join(f"{_q(k)} = ?" for k in upd)
                c.execute(f"UPDATE {t} SET {sets} WHERE {tw}", list(upd.values()) + tp)
            c.execute(f"DELETE FROM {t} WHERE {idw}", idp)
        n += 1
    return n


def _resolve(fn: Any) -> Callable:
    if callable(fn):
        return fn
    mod, _, attr = str(fn).partition(":")
    return getattr(importlib.import_module(mod), attr)


def run_sql(c: sqlite3.Connection, old: str, new: str, now: float, specs: Optional[Sequence] = None,
            late: bool = False) -> dict:
    """All registered rewrites on one connection (call inside a transaction). Returns {label: rows}.
    ``late``: the second pass (conflicts resolved with each column's ``late_conflict``)."""
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    infos: dict = {}
    counts: dict = {}
    for s in (specs if specs is not None else list(_REGISTRY)):
        if isinstance(s, EntityColumn):
            if s.table not in tables:
                continue
            info = infos.get(s.table)
            if info is None:
                info = infos[s.table] = _table_info(c, s.table)
            if s.column not in info.columns:
                continue
            n = _apply_column(c, s, info, old, new, late)
        else:
            if any(t not in tables for t in s.tables):
                continue
            n = int(_resolve(s.fn)(c, old, new, now) or 0)
        if n:
            counts[s.name] = counts.get(s.name, 0) + n
    return counts


# ============================================================================ runner
def _hook_targets(kernel, subsystems: bool) -> list:
    out = []
    w = getattr(kernel, "world", None)
    if w is not None and hasattr(w, "rekey_entity"):
        out.append(("world", w.rekey_entity))
    if subsystems:
        for name in list(getattr(kernel, "_order", []) or []):
            fn = getattr(getattr(kernel, name, None), "rekey_entity", None)
            if fn is not None:
                out.append((name, fn))
        for fn in list(kernel.__dict__.get("_entity_hooks", [])):
            out.append((getattr(fn, "__qualname__", "hook"), fn))
    return out


async def rekey(kernel, old: str, new: str, *, cause: Optional[str] = None, hooks: bool = True,
                emit: bool = True) -> dict:
    """Move everything stored under entity ``old`` to entity ``new`` (see module docstring). Idempotent.
    ``hooks=False`` re-keys only the World's in-memory state (used at start, before subsystems loaded anything).
    Returns the report that is emitted as ``world.rekeyed``."""
    old, new = str(old or ""), str(new or "")
    if not old or not new or old == new:
        return {}
    if old == SELF:
        raise ValueError("the self entity cannot be merged away")
    lock = kernel.__dict__.setdefault("_rekey_lock", asyncio.Lock())
    async with lock:
        now = kernel.clock.now()
        specs = list(_REGISTRY)
        moved = await kernel.db.transaction(lambda c: run_sql(c, old, new, now, specs))
        ran, errors = 0, []
        for name, fn in _hook_targets(kernel, hooks):
            try:
                r = fn(old, new)
                if inspect.isawaitable(r):
                    await r
                ran += 1
            except Exception as e:
                log.exception("rekey hook %s failed (%s -> %s)", name, old, new)
                errors.append(f"{name}: {e!r}"[:200])
        # rows written under the old id while caches were switching over (write-behind queues, in-flight jobs)
        late = await kernel.db.transaction(lambda c: run_sql(c, old, new, now, specs, late=True))
        for k, v in late.items():
            moved[k] = moved.get(k, 0) + v
        report = {"from": old, "into": new, "moved": moved, "rows": sum(moved.values()), "hooks": ran,
                  "errors": errors}
        log.info("rekey %s -> %s: %d rows %s", old, new, report["rows"], moved)
        if emit and getattr(kernel, "bus", None) is not None:
            await kernel.bus.emit(REKEYED, report, source="world", causal_parent=cause)
        return report


async def pending_merges(kernel) -> list:
    """``world.merged`` events without a ``world.rekeyed`` follow-up (crash between the two, or merges declared
    at start before the subsystems' tables were open), oldest first: [(event_id, from, into)]."""
    rows = await kernel.db.fetchall(
        "SELECT m.id, m.payload FROM events m WHERE m.type = ? AND NOT EXISTS (SELECT 1 FROM events r "
        "WHERE r.type = ? AND r.causal_parent = m.id) ORDER BY m.ts, m.rowid", (MERGED, REKEYED))
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        if p.get("from") and p.get("into"):
            out.append((r["id"], p["from"], p["into"]))
    return out


# ============================================================================ central registry (all subsystems)
# Merge helpers for primary-key conflicts --------------------------------------------------------------------
def _beta_merge(old: dict, cur: dict) -> dict:
    """Two Beta(alpha, beta) posteriors that both started from the Beta(1, 1) prior."""
    a = (old.get("alpha") or 1.0) + (cur.get("alpha") or 1.0) - 1.0
    b = (old.get("beta") or 1.0) + (cur.get("beta") or 1.0) - 1.0
    return {"alpha": a, "beta": b, "n": int(old.get("n") or 0) + int(cur.get("n") or 0),
            "updated": max(_num(old.get("updated")), _num(cur.get("updated")), 0.0)}


def _aspect_merge(old: dict, cur: dict) -> dict:
    """Weighted attitude averages (soc_aspects): the weights add, the distributions average by weight."""
    import math
    wa, wb = float(old.get("weight") or 0.0), float(cur.get("weight") or 0.0)
    out = {"weight": wa + wb, "n": int(old.get("n") or 0) + int(cur.get("n") or 0),
           "discarded": int(old.get("discarded") or 0) + int(cur.get("discarded") or 0),
           "updated": max(_num(old.get("updated")), _num(cur.get("updated")), 0.0),
           "summary": cur.get("summary") or old.get("summary")}
    if wa + wb > 0:
        ps = [(float(old.get(k) or 0.0) * wa + float(cur.get(k) or 0.0) * wb) / (wa + wb)
              for k in ("pos", "neg", "neu")]
        out.update(pos=ps[0], neg=ps[1], neu=ps[2])
        out["entropy"] = -sum(p * math.log(p) for p in ps if p > 0) / math.log(3)
    return out


def _consent_merge(a: Any, b: Any) -> Any:
    """Consent of the merged person: a permission holds only if both identities gave it."""
    ja, jb = _jload(a), _jload(b)
    if not isinstance(ja, dict) or not isinstance(jb, dict):
        return b if b is not None else a
    out = dict(ja)
    out.update(jb)
    for k in set(ja) & set(jb):
        if isinstance(ja[k], bool) and isinstance(jb[k], bool):
            out[k] = ja[k] and jb[k]
    return json.dumps(out, ensure_ascii=False)


def merge_annoyance(old: Any, cur: Any) -> Any:
    """motivation reception state per person (decayed warm / flat / ignored counts, open waits): the counts add,
    the open waits are united, ``last`` takes the max. Legacy streak / backoff fields are dropped."""
    if not isinstance(old, dict) or not isinstance(cur, dict):
        return merge_json(old, cur)
    out = dict(old)
    out.update(cur)
    for k in ("warm", "flat", "ignored"):
        out[k] = round(float(old.get(k) or 0) + float(cur.get(k) or 0), 4)
    out["last"] = max(old.get("last") or 0, cur.get("last") or 0)
    pend = dict(old.get("pending") or {}) if isinstance(old.get("pending"), dict) else {}
    pend.update(cur.get("pending") or {} if isinstance(cur.get("pending"), dict) else {})
    out["pending"] = pend
    for k in ("ignored_streak", "backoff_until"):
        out.pop(k, None)
    return out


def _dialogue_person_merge(old: dict, cur: dict) -> dict:
    """Per-person dialogue state (kv ns 'dialogue.person', alive/core/dialogue/engine.py): the newer goal wins,
    my recent moves / topic owners / stances toward the two identities interleave (newest kept)."""
    a, b = _jload(old.get("v")), _jload(cur.get("v"))
    if not isinstance(a, dict) or not isinstance(b, dict):
        return {"v": cur.get("v") if cur.get("v") is not None else old.get("v"),
                "updated": max(_num(old.get("updated")), _num(cur.get("updated")))}
    newer, older = (b, a) if _num(b.get("updated")) >= _num(a.get("updated")) else (a, b)
    out = dict(older)
    out.update(newer)
    for key, n in (("my_moves", 8), ("topic_owners", 10), ("stances", 30)):
        out[key] = (list(older.get(key) or []) + list(newer.get(key) or []))[-n:]
    out["goal"] = newer.get("goal") or older.get("goal")
    return {"v": json.dumps(out, ensure_ascii=False),
            "updated": max(_num(old.get("updated")), _num(cur.get("updated")))}


def _gov_outcome_merge(old: Any, cur: Any) -> Any:
    """gov kv outcomes per person: keep the state of the most recently ignored identity."""
    if not isinstance(old, dict) or not isinstance(cur, dict):
        return merge_json(old, cur)
    return old if _num(old.get("last_ignored")) > _num(cur.get("last_ignored")) else cur


def _mem_buffer_merge(old: Any, cur: Any) -> Any:
    """memory conversation buffers of two spaces that became one: lines interleave by time."""
    if not isinstance(old, dict) or not isinstance(cur, dict):
        return merge_json(old, cur)
    out = dict(cur)
    for k in ("lines", "context"):
        ls = list(old.get(k) or []) + list(cur.get(k) or [])
        ls.sort(key=lambda l: _num(l.get("ts")) if isinstance(l, dict) else 0.0)
        out[k] = ls
    out["participants"] = sorted(set(old.get("participants") or []) | set(cur.get("participants") or []))
    out["summary"] = cur.get("summary") or old.get("summary") or ""
    return out


def _kv(ns: str, key: Optional[str] = None) -> str:
    return f"ns = '{ns}'" + (f" AND k = '{key}'" if key is not None else "")


def _register_builtin() -> None:
    R = register_entity_column
    # --- world (alive/world) ------------------------------------------------------------------------------
    R("world_handles", "entity_id", owner="world")
    R("world_members", "space", SPACE, on_conflict={"since": "min", "last_seen": "max"}, owner="world")
    R("world_members", "member", on_conflict={"since": "min", "last_seen": "max"}, owner="world")
    # --- event log: payloads stay verbatim; the indexed space column follows the merge -------------------
    R("events", "thread_id", SPACE, owner="runtime")
    # --- memory (alive/memory) -----------------------------------------------------------------------------
    R("mem_records", "person_keys", JSON_LIST, owner="memory")
    R("mem_records", "told_by", owner="memory")
    R("mem_records", "thread_id", SPACE, owner="memory")
    R("mem_records", "meta", JSON, owner="memory")                       # meta.origins: the spaces it came from
    R("mem_ent", "key", where="role IN ('person', 'told_by')", on_conflict="keep", owner="memory")
    R("kv", "v", JSON, where=_kv("mem", "names"), label="kv:mem/names", owner="memory")
    R("kv", "v", JSON, where=_kv("mem", "buffers"), json_merge=_mem_buffer_merge, label="kv:mem/buffers",
      owner="memory")
    R("kv", "v", JSON, where=_kv("mem.precompute"), label="kv:mem.precompute", owner="memory")
    # --- growth (alive/growth) -----------------------------------------------------------------------------
    R("grow_evidence", "subject", owner="growth")
    R("grow_evidence", "about", PREFIX, prefixes=("interpretation:",), owner="growth")
    # scope "person:<id>" / "channel:<space>": a versioned change (INV-9), done by growth itself
    register_entity_sql("grow_artifacts.scope", "alive.growth.artifacts:rekey_scopes_sql",
                        tables=("grow_artifacts", "grow_versions"), owner="growth")
    R("grow_artifacts", "meta", JSON, owner="growth")                    # speakers / example_by (private meta)
    R("kv", "v", JSON, where=_kv("grow", "names"), label="kv:grow/names", owner="growth")
    # --- motivation (alive/motivation) ---------------------------------------------------------------------
    R("mot_intentions", "target_person", owner="motivation")
    R("mot_intentions", "trigger", JSON, owner="motivation")             # {"type": "online", "person": ...}
    R("mot_intentions", "meta", JSON, owner="motivation")                # requested_by, ...
    R("mot_outreach", "person_key", owner="motivation")
    R("mot_outreach", "thread_id", SPACE, owner="motivation")
    R("mot_outreach", "meta", JSON, owner="motivation")
    R("kv", "v", JSON, where=_kv("motivation", "annoyance"), json_merge=merge_annoyance,
      label="kv:motivation/annoyance", owner="motivation")
    # --- affect (alive/affect): emotion targets inside the persisted state ----------------------------------
    R("kv", "v", JSON, where=_kv("affect", "state"), label="kv:affect/state", owner="affect")
    # --- social (alive/social) -----------------------------------------------------------------------------
    R("soc_threads", "thread_id", SPACE, owner="social", on_conflict={
        "channel": "by:updated", "chat_id": "by:updated", "chat_type": "by:updated", "title": "by:updated",
        "last_in": "max", "last_out": "max", "participants": "union", "meta": "union", "updated": "max"})
    R("soc_threads", "participants", JSON_LIST, owner="social")
    R("soc_messages", "thread_id", SPACE, owner="social")
    R("soc_messages", "sender_key", owner="social")
    R("soc_persons", "person_key", owner="social", on_conflict={
        "names": "union", "handles": "union", "consent": _consent_merge, "common_ground": "union",
        "private_from_others": "union", "mind_model": "union", "paired": "or", "blocked": "or",
        "first_seen": "min", "meta": "union", "updated": "max"})
    R("soc_persons", "meta", JSON, owner="social")                        # meta.dm_thread is the 1:1 space id
    R("soc_relationships", "person_key", owner="social", on_conflict={
        "closeness": "by:closeness_ts", "closeness_ts": "max", "warmth": "by:warmth_ts", "warmth_ts": "max",
        "trust": "max", "reciprocity": "by:updated", "rhythm": "by:last_contact", "last_contact": "max",
        "last_in": "max", "last_out": "max", "last_initiated_by": "by:last_contact", "ignored_streak": "by:last_out",
        "initiatives": "union", "interactions": "sum", "milestones": "union", "open_loops": "union",
        "updated": "max"})
    R("soc_aspects", "person_key", on_conflict=_aspect_merge, owner="social")
    R("soc_opinions", "person_key", owner="social")
    R("soc_pairing", "person_key", owner="social")
    R("soc_initiatives", "person_key", owner="social")
    R("soc_initiatives", "thread_id", SPACE, owner="social")
    # --- governance (alive/governance) ---------------------------------------------------------------------
    R("gov_contacts", "person_key", on_conflict={"trust": "max", "proactive_ok": "and", "updated": "max"},
      owner="governance")
    R("gov_initiatives", "person_key", owner="governance")
    # keyed by PERSON entity despite the column name (docs/22: a person told once knows)
    R("gov_disclosure", "thread_id", on_conflict={"first_at": "min", "last_at": "max", "count": "sum"},
      owner="governance")
    R("gov_usage", "person_key", on_conflict={"minutes": "sum", "msgs": "sum", "late_msgs": "sum", "cues": "sum"},
      owner="governance")
    R("gov_approvals", "person_key", owner="governance")
    R("gov_approvals", "thread_id", SPACE, owner="governance")
    R("gov_approvals", "request", JSON, owner="governance")
    R("kv", "v", JSON, where=_kv("gov", "outcomes"), json_merge=_gov_outcome_merge, label="kv:gov/outcomes",
      owner="governance")
    R("kv", "v", JSON, where=_kv("gov", "crisis"), label="kv:gov/crisis", owner="governance")
    # --- core (alive/core, core/dialogue, core/understanding) ----------------------------------------------
    R("core_move_stats", "key", PREFIX, prefixes=("p:",), sep="|", on_conflict=_beta_merge, owner="core")
    R("core_moves", "thread_id", SPACE, owner="core")
    R("core_moves", "person_key", owner="core")
    R("core_renqing", "person_key", owner="core", late_conflict="same", on_conflict={   # written as increments
        "care_given": "sum", "care_received": "sum", "favors_given": "sum", "favors_received": "sum",
        "updated": "max"})
    R("core_readings", "person_key", owner="core")
    R("core_readings", "thread_id", SPACE, owner="core")
    # dialogue facts per space: kv key = space id (keep the more recently updated state), value has thread_id
    R("kv", "k", SPACE, where=_kv("dialogue"), on_conflict="newest:updated", label="kv:dialogue[key]", owner="core")
    R("kv", "v", JSON, where=_kv("dialogue"), label="kv:dialogue", owner="core")
    # what I'm after with each PERSON (goal + next, my recent moves toward them, topic owners, stances): key = person
    R("kv", "k", TEXT, where=_kv("dialogue.person"), on_conflict=_dialogue_person_merge,
      label="kv:dialogue.person[key]", owner="core")
    R("kv", "v", JSON, where=_kv("dialogue.person"), label="kv:dialogue.person", owner="core")


_register_builtin()
