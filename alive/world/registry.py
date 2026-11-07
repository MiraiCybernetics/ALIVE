"""Entity registry (docs/22 §3).

- An entity is a person, a group (a collective with members), the individual itself, or later things,
  places and topics. Each has a stable opaque id (``p_…``, ``g_…``, ``self``).
- A handle is how a transport refers to something: ``telegram:12345``, ``qq:g678``. Many handles can point
  to one entity (the same friend on Telegram and QQ). Handles never leak above the transport layer.
- A route is where to reach an entity right now: the handle it was most recently active on.
- Membership records which persons were seen in which group (who is present = audience).

Resolution is synchronous from an in-memory cache (cheap on the hot path); writes are write-behind.
Identities are only merged explicitly (config ``world.identities`` or ``link()``), never guessed. Every merge
emits ``world.merged``; the kernel then moves all data stored under the absorbed id (alive/world/rekey.py).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from alive.runtime.store import register_schema

log = logging.getLogger("alive.world")
SELF = "self"

register_schema("world", 1, [
    """CREATE TABLE IF NOT EXISTS world_entities (
        id TEXT PRIMARY KEY, kind TEXT, name TEXT, aliases TEXT, attrs TEXT, created REAL, updated REAL)""",
    """CREATE TABLE IF NOT EXISTS world_handles (
        handle TEXT PRIMARY KEY, entity_id TEXT, kind TEXT, first_seen REAL, last_seen REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_world_handles_entity ON world_handles(entity_id)",
    """CREATE TABLE IF NOT EXISTS world_members (
        space TEXT, member TEXT, since REAL, last_seen REAL, PRIMARY KEY (space, member))""",
])


@dataclass
class Entity:
    id: str
    kind: str                    # person | group | self | agent | thing | place | topic
    name: str = ""
    aliases: list = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    created: float = 0.0
    updated: float = 0.0


class World:
    def __init__(self, kernel):
        self.k = kernel
        self.entities: dict = {}       # id -> Entity
        self.handles: dict = {}        # handle -> entity id
        self.last_seen: dict = {}      # handle -> ts
        self.members: dict = {}        # group id -> {member id: last_seen}
        self._pending: list = []
        self._merges: list = []        # (into, from) merges not yet announced as world.merged
        self._starting = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._starting = True     # merges declared during start are announced at its end, in order
        try:
            await self._load_and_declare()
        finally:
            self._starting = False
        await self._emit_merges()    # declarations that folded existing entities together (migrated by the kernel)

    async def _load_and_declare(self) -> None:
        rows = await self.k.db.fetchall("SELECT * FROM world_entities")
        for r in rows:
            self.entities[r["id"]] = Entity(r["id"], r["kind"], r["name"] or "", json.loads(r["aliases"] or "[]"),
                                            json.loads(r["attrs"] or "{}"), r["created"] or 0.0, r["updated"] or 0.0)
        for r in await self.k.db.fetchall("SELECT * FROM world_handles"):
            self.handles[r["handle"]] = r["entity_id"]
            self.last_seen[r["handle"]] = r["last_seen"] or 0.0
        for r in await self.k.db.fetchall("SELECT * FROM world_members"):
            self.members.setdefault(r["space"], {})[r["member"]] = r["last_seen"] or 0.0
        if SELF not in self.entities:
            self._put(Entity(SELF, "self", name=""))
        # declared identities: one entity for several handles (e.g. the owner on Telegram and the CLI)
        for ident in self.k.cfg.get("world.identities", []) or []:
            hs = [h for h in ident.get("handles", []) if h]
            if hs:
                self.declare(hs, name=ident.get("name", ""), kind=ident.get("kind", "person"))
        owner_handles = list(self.k.cfg.get("owner.person_keys") or [])
        if owner_handles:
            oid = self.declare(owner_handles, name=self.k.cfg.get("owner.name", ""), kind="person")
            self.entities[oid].attrs["owner"] = True
            # governance compares person keys with the owner list: give it the entity id as well
            keys = list(owner_handles)
            if oid not in keys:
                keys.append(oid)
            self.k.cfg.set("owner.person_keys", keys)
        await self.flush()

    async def flush(self) -> None:
        if not self._pending:
            return
        jobs, self._pending = self._pending, []

        def run(c):
            for sql, params in jobs:
                c.execute(sql, params)
        await self.k.db.transaction(run)

    def _write(self, sql: str, params: tuple) -> None:
        self._pending.append((sql, params))
        if len(self._pending) == 1:
            try:
                import asyncio
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.flush()))
            except RuntimeError:
                pass

    def _put(self, e: Entity) -> None:
        now = self.k.clock.now()
        e.created = e.created or now
        e.updated = now
        self.entities[e.id] = e
        self._write("INSERT OR REPLACE INTO world_entities VALUES (?,?,?,?,?,?,?)",
                    (e.id, e.kind, e.name, json.dumps(e.aliases, ensure_ascii=False),
                     json.dumps(e.attrs, ensure_ascii=False), e.created, e.updated))

    def _bind(self, handle: str, eid: str, kind: str) -> None:
        now = self.k.clock.now()
        self.handles[handle] = eid
        self.last_seen.setdefault(handle, 0.0)
        self._write("INSERT OR REPLACE INTO world_handles VALUES (?,?,?,?,?)",
                    (handle, eid, kind, now, self.last_seen.get(handle, 0.0)))

    # ------------------------------------------------------------------ resolution (hot path, sync)
    def _fresh_id(self, preferred: str, kind: str) -> str:
        """Entity ids are opaque and stable. By default an entity is named after the first handle it was met on
        (readable, and stable across restarts); later handles of the same entity keep pointing to it."""
        eid = preferred
        while eid in self.entities and self.entities[eid].kind != kind:
            eid += "#"
        return eid

    def _resolve(self, handle: str, kind: str, name: str = "", preferred: str = "") -> str:
        eid = self.handles.get(handle)
        if eid is None:
            eid = self._fresh_id(preferred or handle, kind)
            if eid not in self.entities:
                self._put(Entity(eid, kind, name=name or ""))
            self._bind(handle, eid, kind)
        elif name:
            e = self.entities.get(eid)
            if e is not None and name != e.name and name not in e.aliases:
                if not e.name:
                    e.name = name
                else:
                    e.aliases = (e.aliases + [name])[-8:]
                self._put(e)
        return eid

    def person(self, channel: str, native_id: str, name: str = "") -> str:
        return self._resolve(f"{channel}:{native_id}", "person", name)

    def group(self, channel: str, chat_id: str, title: str = "") -> str:
        return self._resolve(f"{channel}:#{chat_id}", "group", title, preferred=f"{channel}:{chat_id}")

    def seen(self, channel: str, chat_id: str, chat_type: str, entity_id: str, ts: float) -> None:
        """Record that an entity was reachable at this route (latest route wins for replies/outreach)."""
        h = f"{channel}:#{chat_id}" if chat_type == "group" else f"{channel}:@{chat_id}"
        if h not in self.handles:
            self._bind(h, entity_id, "route")
        self.last_seen[h] = ts

    def note_member(self, group_id: str, member_id: str, ts: float) -> None:
        m = self.members.setdefault(group_id, {})
        new = member_id not in m
        m[member_id] = ts
        if new or int(ts) % 3600 < 60:
            self._write("INSERT OR REPLACE INTO world_members VALUES (?,?,?,?)", (group_id, member_id, ts, ts))

    # ------------------------------------------------------------------ queries
    def entity(self, eid: str) -> Optional[Entity]:
        return self.entities.get(eid)

    def kind(self, eid: str) -> str:
        e = self.entities.get(eid)
        return e.kind if e else "person"

    def name(self, eid: str) -> str:
        e = self.entities.get(eid)
        return (e.name or (e.aliases[-1] if e.aliases else "")) if e else ""

    def handles_of(self, eid: str) -> list:
        return [h for h, e in self.handles.items() if e == eid]

    def route(self, eid: str) -> Optional[tuple]:
        """(channel, chat_id, chat_type) of the most recently active route for this entity."""
        best, best_ts = None, -1.0
        for h, e in self.handles.items():
            if e != eid:
                continue
            ch, _, rest = h.partition(":")
            if rest.startswith("#"):
                cand = (ch, rest[1:], "group")
            elif rest.startswith("@"):
                cand = (ch, rest[1:], "private")
            else:
                continue          # identity handles are not routes
            ts = self.last_seen.get(h, 0.0)
            if ts > best_ts:
                best, best_ts = cand, ts
        return best

    def members_of(self, gid: str, active_within: Optional[float] = None) -> list:
        m = self.members.get(gid, {})
        if active_within is None:
            return list(m)
        now = self.k.clock.now()
        return [x for x, ts in m.items() if now - ts <= active_within]

    def spaces_of(self, member: str) -> list:
        """Shared spaces (groups) this entity has been seen in, most recently active first."""
        out = [(ts.get(member, 0.0), gid) for gid, ts in self.members.items() if member in ts]
        return [gid for _, gid in sorted(out, reverse=True)]

    def is_owner(self, eid: str) -> bool:
        e = self.entities.get(eid)
        return bool(e and e.attrs.get("owner"))

    # ------------------------------------------------------------------ identity management (explicit only)
    def declare(self, handles: Sequence[str], *, name: str = "", kind: str = "person") -> str:
        """Bind several handles to one entity (creates it if needed). Returns the entity id.

        If the handles already belong to different entities, those entities are the same one: the others are
        folded into the first (all their handles included) and ``world.merged`` is emitted for each."""
        hs = [h for h in handles if h]
        if not hs:
            raise ValueError("declare() needs at least one handle")
        existing = list(dict.fromkeys(self.handles[h] for h in hs if h in self.handles))
        eid = existing[0] if existing else self._fresh_id(hs[0], kind)
        if eid not in self.entities:
            self._put(Entity(eid, kind, name=name))
        elif name and not self.entities[eid].name:
            self.entities[eid].name = name
            self._put(self.entities[eid])
        for other in existing[1:]:
            if other != eid and SELF not in (eid, other):
                self._absorb(eid, other)
        for h in hs:
            if self.handles.get(h) != eid:
                self._bind(h, eid, kind)
        if self._merges:
            self._schedule_merges()
        return eid

    async def link(self, handle_a: str, handle_b: str) -> Optional[str]:
        """Owner-confirmed identity merge: everything that pointed to b's entity now points to a's.
        Emits ``world.merged``; the kernel migrates every subsystem's data from b to a (docs/22 §3.3)."""
        a, b = self.handles.get(handle_a), self.handles.get(handle_b)
        if a is None or b is None or a == b or SELF in (a, b):
            return a
        self._absorb(a, b)
        await self._emit_merges()
        return a

    def _absorb(self, a: str, b: str) -> None:
        """Fold entity b into a (sync): handles, names, attributes, membership. Data migration of everything
        else keyed by b happens on ``world.merged`` (alive/world/rekey.py)."""
        for h, e in list(self.handles.items()):
            if e == b:
                self._bind(h, a, self.kind(a))
        eb = self.entities.pop(b, None)
        ea = self.entities.get(a)
        if eb is not None:
            if ea is not None:
                names = [n for n in [eb.name] + list(eb.aliases) if n and n != ea.name]
                if not ea.name and names:
                    ea.name = names.pop(0)
                ea.aliases = list(dict.fromkeys(ea.aliases + names))[:12]
                for k, v in (eb.attrs or {}).items():
                    ea.attrs.setdefault(k, v)
                self._put(ea)
            self._write("DELETE FROM world_entities WHERE id=?", (b,))
        self.rekey_entity(b, a)
        self._merges.append((a, b))

    def rekey_entity(self, old: str, new: str) -> None:
        """In-memory part of a merge (the world_* rows are rewritten by alive/world/rekey.py). Idempotent."""
        if old == new:
            return
        m = self.members.pop(old, None)
        if m:
            tgt = self.members.setdefault(new, {})
            for x, ts in m.items():
                tgt[x] = max(tgt.get(x, 0.0), ts)
        for mm in self.members.values():
            if old in mm:
                ts = mm.pop(old)
                mm[new] = max(mm.get(new, 0.0), ts)
        for h, e in list(self.handles.items()):
            if e == old:
                self.handles[h] = new

    def _schedule_merges(self) -> None:
        if self._starting:
            return
        try:
            import asyncio
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self._emit_merges()))
        except RuntimeError:
            pass      # no loop: announced by the next start()/link()

    async def _emit_merges(self) -> None:
        if not self._merges:
            return
        await self.flush()
        from alive.world.rekey import MERGED
        while self._merges:
            a, b = self._merges.pop(0)
            await self.k.bus.emit(MERGED, {"into": a, "from": b}, source="world")

    def status(self) -> dict:
        kinds: dict = {}
        for e in self.entities.values():
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        return {"entities": kinds, "handles": len(self.handles)}
