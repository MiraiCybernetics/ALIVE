"""Who may hear what (docs/03 §8, docs/22 §1, INV-34): privacy follows the information, not the channel.

Every piece of information I hold came from somewhere: a one-to-one space (its id is the counterpart's person
entity), a shared space (a group entity), or nowhere in particular (my own thoughts, things I read). That origin
defines its audience:

- told to me one-to-one by X           -> {X}: usable only with X, one-to-one
- said in a shared space G              -> G's members: semi-public within G
- no origin (my own idea, a web page)   -> anyone

Something derived from several pieces (a fact merged from two sources, a nightly summary, a reflection) keeps all
their origins, i.e. the most restrictive audience of its sources.

Before I speak in a space, everything that would reach that space's audience is checked against the origins of
what I am about to use: a record is usable iff every origin's audience covers everyone who would hear me. A shared
space is never fully known (people read without speaking), so nothing told one-to-one is ever usable in one.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional, Sequence

SELF = "self"


def _get(rec: Any, key: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(key, default)
    try:
        return rec[key]                       # sqlite3.Row
    except (KeyError, IndexError, TypeError):
        return getattr(rec, key, default)


def origins_of(rec: Any) -> list:
    """Spaces a memory record's information came from: ``meta.origins`` (records derived from others) or its own
    ``thread_id``. An empty list means the record is nobody's private information."""
    meta = _get(rec, "meta", None)
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            meta = {}
    if isinstance(meta, dict) and isinstance(meta.get("origins"), list):
        return [str(o) for o in meta["origins"] if o]
    tid = _get(rec, "thread_id", None)
    return [str(tid)] if tid else []


def merge_origins(*groups: Iterable[str]) -> list:
    out: list = []
    for g in groups:
        for o in g or ():
            if o and o not in out:
                out.append(o)
    return out


class Scope:
    """The audience of what I say in ``space``: the counterpart of a one-to-one space; the known members of a
    shared space (plus readers I have never seen). ``present`` adds people known to be there right now."""

    def __init__(self, world, space: Optional[str], present: Sequence[str] = ()):
        self.world = world
        self.space = space or ""
        self.kind = self._kind(self.space) if self.space else ""
        people = {p for p in present or () if p and p != SELF}
        if self.kind == "person":
            people.add(self.space)
        elif self.kind == "group":
            people |= self._members(self.space)
        people.discard(SELF)
        self.people = frozenset(people)
        self._cache: dict = {}

    # ------------------------------------------------------------------ world lookups
    def _kind(self, sid: str) -> str:
        w = self.world
        if w is None or not sid:
            return ""
        try:
            e = w.entity(sid)
        except Exception:
            return ""
        if e is None:
            return ""
        return "group" if e.kind == "group" else ("person" if e.kind in ("person", "agent") else e.kind)

    def _members(self, gid: str) -> set:
        try:
            return {m for m in self.world.members_of(gid) if m and m != SELF}
        except Exception:
            return set()

    @property
    def shared(self) -> bool:
        """A space where more people than one counterpart may read what I say."""
        return self.kind == "group"

    # ------------------------------------------------------------------ the rule
    def covers_space(self, origin: Optional[str]) -> bool:
        """May information that came from ``origin`` be used where everyone in this scope hears it?"""
        if not origin or origin == self.space:
            return True
        hit = self._cache.get(origin)
        if hit is not None:
            return hit
        ok = False
        if self._kind(origin) == "group" and self.people:
            ok = self.people <= self._members(origin)      # everyone here could have heard it there
        # told one-to-one (or an origin I cannot place): only in that same space
        self._cache[origin] = ok
        return ok

    def covers(self, origins: Iterable[str]) -> bool:
        return all(self.covers_space(o) for o in origins or ())

    def allows(self, rec: Any) -> bool:
        return self.covers(origins_of(rec))

    def split(self, recs: Iterable[Any]) -> tuple:
        """-> (usable here, withheld)."""
        ok, out = [], []
        for r in recs:
            (ok if self.allows(r) else out).append(r)
        return ok, out

    def private_in(self, origins: Iterable[str]) -> bool:
        """Does information from these origins count as private data (rule of two, docs/09 §3.2)? Anything told
        one-to-one (or from a space I cannot place); what was said in a shared space is semi-public."""
        return any(o and self._kind(o) != "group" for o in origins or ())


def scope_for(kernel, space: Optional[str], present: Sequence[str] = ()) -> Scope:
    return Scope(getattr(kernel, "world", None), space, present)
