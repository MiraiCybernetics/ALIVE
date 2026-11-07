"""Entity merge migration (docs/22 §3.3, INV-27): after link()/declare(), nothing stays under the absorbed id."""
import json
import sqlite3

import pytest

import alive.core.dialogue.policy  # noqa: F401  (registers core_dialogue tables)
import alive.core.understanding  # noqa: F401
import alive.governance.engine  # noqa: F401
import alive.social.store  # noqa: F401
from alive.core.types import Trust
from alive.memory.extraction import _Buf
from alive.runtime.clock import ManualClock
from alive.testing import make_test_kernel
from alive.world import rekey as R

START = 1_794_000_000.0
MINE = ["memory", "growth", "motivation", "affect"]


async def _kernel(tmp_path, real=MINE, cfg=None):
    return await make_test_kernel(tmp_path, real=list(real), clock=ManualClock(start=START), cfg_overrides=cfg)


async def _merge(k, ha: str, hb: str) -> str:
    into = await k.world.link(ha, hb)
    await k.bus.drain()
    return into


async def _one(k, sql, params=()):
    return await k.db.fetchone(sql, params)


async def _count(k, sql, params=()):
    return await k.db.fetchval(sql, params, default=0)


# ============================================================================ mechanics (plain sqlite)
def _db():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE stats (person TEXT NOT NULL, day TEXT NOT NULL, n INTEGER, best REAL, note TEXT, ts REAL,
                            PRIMARY KEY (person, day));
        CREATE TABLE ents (key TEXT NOT NULL, rec TEXT NOT NULL, role TEXT NOT NULL,
                           PRIMARY KEY (key, rec, role)) WITHOUT ROWID;
        CREATE TABLE docs (id INTEGER PRIMARY KEY, people TEXT, blob TEXT, scope TEXT);
        CREATE TABLE moves (key TEXT PRIMARY KEY, alpha REAL, beta REAL, n INTEGER, updated REAL);
    """)
    return c


def test_primary_key_conflicts_merge_per_strategy():
    c = _db()
    c.executemany("INSERT INTO stats VALUES (?,?,?,?,?,?)", [
        ("old", "d1", 2, 0.5, None, 10.0), ("new", "d1", 3, 0.9, "kept", 5.0),   # conflict
        ("old", "d2", 7, 0.1, "moved", 1.0)])                                         # no conflict
    c.executemany("INSERT INTO ents VALUES (?,?,?)", [("old", "r1", "person"), ("new", "r1", "person"),
                                                      ("old", "r2", "person"), ("old", "r3", "entity")])
    c.executemany("INSERT INTO moves VALUES (?,?,?,?,?)", [
        ("p:old|chat|vent|ask", 3.0, 2.0, 3, 1.0), ("p:new|chat|vent|ask", 2.0, 4.0, 4, 2.0),
        ("p:old|chat|vent|joke", 1.0, 3.0, 2, 1.0), ("p:old2|chat|vent|ask", 9.0, 9.0, 16, 1.0),
        ("g:chat|vent|ask", 5.0, 5.0, 8, 1.0)])
    specs = [
        R.EntityColumn("stats", "person", on_conflict={"n": "sum", "best": "max", "ts": "max"}),
        R.EntityColumn("ents", "key", where="role = 'person'", on_conflict="keep"),
        R.EntityColumn("moves", "key", R.PREFIX, prefixes=("p:",), sep="|", on_conflict=R._beta_merge),
    ]
    with c:
        moved = R.run_sql(c, "old", "new", 100.0, specs)
    assert moved == {"stats.person": 2, "ents.key": 2, "moves.key": 2}
    rows = {(r["person"], r["day"]): dict(r) for r in c.execute("SELECT * FROM stats")}
    assert set(rows) == {("new", "d1"), ("new", "d2")}
    assert rows[("new", "d1")]["n"] == 5 and rows[("new", "d1")]["best"] == 0.9 and rows[("new", "d1")]["ts"] == 10.0
    assert rows[("new", "d1")]["note"] == "kept" and rows[("new", "d2")]["note"] == "moved"
    ents = {tuple(r) for r in c.execute("SELECT key, rec, role FROM ents")}
    assert ents == {("new", "r1", "person"), ("new", "r2", "person"), ("old", "r3", "entity")}  # role filter
    mv = {r["key"]: (r["alpha"], r["beta"], r["n"]) for r in c.execute("SELECT * FROM moves")}
    assert mv["p:new|chat|vent|ask"] == (4.0, 5.0, 7)          # Beta(1,1) prior counted once
    assert mv["p:new|chat|vent|joke"] == (1.0, 3.0, 2)
    assert "p:old2|chat|vent|ask" in mv and "g:chat|vent|ask" in mv   # other ids / global keys untouched
    with c:
        assert R.run_sql(c, "old", "new", 100.0, specs) == {}         # idempotent


def test_late_pass_does_not_count_a_stale_snapshot_twice():
    c = _db()
    c.executemany("INSERT INTO stats VALUES (?,?,?,?,?,?)", [("old", "d1", 2, 0.5, None, 1.0),
                                                              ("new", "d1", 3, 0.9, None, 1.0)])
    snap = [R.EntityColumn("stats", "person", on_conflict={"n": "sum"})]
    incr = [R.EntityColumn("stats", "person", on_conflict={"n": "sum"}, late_conflict="same")]
    with c:
        R.run_sql(c, "old", "new", 1.0, snap)
        c.execute("INSERT INTO stats VALUES ('old', 'd1', 2, 0.5, NULL, 1.0)")   # stale cache re-saved the old row
        assert R.run_sql(c, "old", "new", 1.0, snap, late=True) == {"stats.person": 1}
    assert [tuple(r) for r in c.execute("SELECT person, n FROM stats")] == [("new", 5)]
    with c:
        c.execute("INSERT INTO stats VALUES ('old', 'd1', 1, NULL, NULL, 2.0)")   # an increment-style upsert
        R.run_sql(c, "old", "new", 1.0, incr, late=True)
    assert [tuple(r) for r in c.execute("SELECT person, n FROM stats")] == [("new", 6)]


def test_json_lists_documents_and_scopes():
    c = _db()
    c.execute("INSERT INTO docs VALUES (1, ?, ?, ?)", (json.dumps(["old", "x", "new"]),
                                                      json.dumps({"old": {"n": 1, "tags": ["a"]}, "new": {"n": 2},
                                                                  "by": ["old", "x", "new"], "who": "old"}),
                                                      "person:old"))
    c.execute("INSERT INTO docs VALUES (2, ?, ?, ?)", (json.dumps(["阿明"]), json.dumps(["old2"]),
                                                      "channel:old2"))
    specs = [R.EntityColumn("docs", "people", R.JSON_LIST), R.EntityColumn("docs", "blob", R.JSON),
             R.EntityColumn("docs", "scope", R.PREFIX, prefixes=("person:", "channel:"))]
    with c:
        moved = R.run_sql(c, "old", "new", 1.0, specs)
    assert moved == {"docs.people": 1, "docs.blob": 1, "docs.scope": 1}
    r = c.execute("SELECT * FROM docs WHERE id=1").fetchone()
    assert json.loads(r["people"]) == ["new", "x"]                    # id lists are deduplicated
    blob = json.loads(r["blob"])
    assert blob["new"] == {"n": 2, "tags": ["a"]}                    # key collision: surviving value wins, merged
    assert blob["by"] == ["new", "x", "new"]                         # nested lists keep their alignment
    assert blob["who"] == "new" and "old" not in blob
    assert r["scope"] == "person:new"
    r2 = c.execute("SELECT * FROM docs WHERE id=2").fetchone()
    assert r2["scope"] == "channel:old2" and json.loads(r2["blob"]) == ["old2"]   # no prefix collisions


def test_value_helpers():
    assert R.rekey_str("p:a|x", "a", "b", ("p:",), "|") == "p:b|x"
    assert R.rekey_str("p:ab|x", "a", "b", ("p:",), "|") is None
    d = {"a": 1, "b": 2}
    assert R.rekey_dict(d, "a", "b", lambda o, n: o + n) and d == {"b": 3}
    assert not R.rekey_dict(d, "a", "b")
    assert R.rekey_obj({"t": "a", "l": ["a", "b"]}, "a", "b") == {"t": "b", "l": ["b", "b"]}
    assert R.rekey_obj(["a", "b"], "a", "b", dedupe=True) == ["b"]


# ============================================================================ through the kernel
async def test_link_moves_everything_to_the_surviving_entity(tmp_path):
    k = await _kernel(tmp_path)
    try:
        A = k.world.person("sim", "ming")
        B = k.world.person("sim2", "ming_alt", name="明")
        G = k.world.group("sim", "g1", title="爬山群")
        k.world.note_member(G, B, START)
        k.world.seen("sim2", "ming_alt", "private", B, START)
        await k.world.flush()
        day = k.clock.today()

        # --- memory (real)
        m1 = await k.memory.note(kind="episodic", content="明说周末去爬山", person_keys=[B], thread_id=B)
        m2 = await k.memory.note(kind="semantic", content="明有一只猫叫团子", source="told_by", told_by=B,
                                 trust=Trust.CONTACT, person_keys=[B, A], thread_id=G)
        k.memory.remember_name(B, "明")
        # --- growth (real): a person-scoped active entry, its speakers, and evidence about him
        art, _ = await k.growth.artifacts.create("playbook", "他难过时先听，别急着给建议", kind="strategy",
                                                 scope=f"person:{B}", status="active", examples=["唉今天好累"],
                                                 speakers=[B])
        head0 = art.head_vid
        ev = await k.growth.record_evidence(level="S4", polarity="helpful", about=f"interpretation:{B}", subject=B)
        # --- motivation (real): an intention aimed at him, annoyance under both ids
        it = await k.motivation.add_intention({"goal": "问问明爬山怎么样", "level": "day", "kind": "outreach",
                                               "target_person": B})
        k.motivation._annoy[B] = {"ignored_streak": 2, "backoff_until": START + 100, "warm": 1, "flat": 0,
                                  "ignored": 2, "last": START}
        k.motivation._annoy[A] = {"ignored_streak": 0, "backoff_until": 0.0, "warm": 3, "flat": 1, "ignored": 0,
                                  "last": START - 50}
        await k.motivation._persist_annoy()
        # --- affect (real): an emotion aimed at him
        k.affect.apply_deltas([{"type": "joy", "intensity": 0.6, "target": B, "cause": "e_test"}])
        assert any(e.target == B for e in k.affect.emotions)
        # --- social / governance / core: rows only (those subsystems are faked here)
        tx = [
            ("INSERT INTO soc_messages (thread_id, sender_key, text, ts) VALUES (?,?,?,?)", (B, B, "我在 B 平台", START)),
            ("INSERT INTO soc_messages (thread_id, sender_key, text, ts) VALUES (?,?,?,?)", (A, A, "我在 A 平台", START + 1)),
            ("INSERT INTO soc_threads (thread_id, channel, chat_id, last_in, participants, updated) VALUES (?,?,?,?,?,?)",
             (B, "sim2", "ming_alt", START + 5, json.dumps([B]), START + 5)),
            ("INSERT INTO soc_threads (thread_id, channel, chat_id, last_in, participants, updated) VALUES (?,?,?,?,?,?)",
             (A, "sim", "ming", START + 1, json.dumps([A]), START + 1)),
            ("INSERT INTO soc_relationships (person_key, closeness, closeness_ts, last_contact, interactions, updated) "
             "VALUES (?,?,?,?,?,?)", (B, 0.4, START + 5, START + 5, 7, START + 5)),
            ("INSERT INTO soc_relationships (person_key, closeness, closeness_ts, last_contact, interactions, updated) "
             "VALUES (?,?,?,?,?,?)", (A, 0.2, START, START, 3, START)),
            ("INSERT INTO gov_usage (person_key, day, minutes, msgs) VALUES (?,?,?,?)", (B, day, 10.0, 4)),
            ("INSERT INTO gov_usage (person_key, day, minutes, msgs) VALUES (?,?,?,?)", (A, day, 5.0, 2)),
            ("INSERT INTO gov_contacts (person_key, trust, proactive_ok, updated) VALUES (?,?,?,?)", (B, 2, 1, START)),
            ("INSERT INTO core_renqing (person_key, care_given, favors_received) VALUES (?,?,?)", (B, 2, 1)),
            ("INSERT INTO core_renqing (person_key, care_given, favors_received) VALUES (?,?,?)", (A, 1, 0)),
            ("INSERT INTO core_move_stats VALUES (?,?,?,?,?)", (f"p:{B}|chat|vent|listen", 3.0, 1.0, 2, START)),
            ("INSERT INTO kv (ns, k, v, updated) VALUES ('dialogue', ?, ?, ?)",
             (B, json.dumps({"thread_id": B, "goal": "陪伴"}), START)),
            # what I'm after with each identity (per person): the newer goal survives, my moves interleave
            ("INSERT INTO kv (ns, k, v, updated) VALUES ('dialogue.person', ?, ?, ?)",
             (B, json.dumps({"person_key": B, "goal": {"aim": "陪他聊聊"}, "my_moves": ["react"],
                             "updated": START + 5}), START + 5)),
            ("INSERT INTO kv (ns, k, v, updated) VALUES ('dialogue.person', ?, ?, ?)",
             (A, json.dumps({"person_key": A, "goal": {"aim": "旧的"}, "my_moves": ["open_q"], "updated": START}),
              START)),
        ]
        await k.db.transaction(lambda c: [c.execute(q, p) for q, p in tx])

        into = await _merge(k, "sim:ming", "sim2:ming_alt")
        assert into == A and k.world.entity(B) is None
        assert k.world.person("sim2", "ming_alt") == A

        # nothing anywhere still points at B
        assert await _count(k, "SELECT COUNT(*) FROM mem_records WHERE told_by=? OR thread_id=? OR person_keys LIKE ?",
                            (B, B, f'%"{B}"%')) == 0
        assert await _count(k, "SELECT COUNT(*) FROM mem_ent WHERE key=?", (B,)) == 0
        assert await _count(k, "SELECT COUNT(*) FROM grow_evidence WHERE subject=?", (B,)) == 0
        for t, col in [("soc_messages", "thread_id"), ("soc_messages", "sender_key"), ("soc_threads", "thread_id"),
                       ("soc_relationships", "person_key"), ("gov_usage", "person_key"),
                       ("gov_contacts", "person_key"), ("core_renqing", "person_key"),
                       ("mot_intentions", "target_person"), ("world_members", "member"),
                       ("world_handles", "entity_id")]:
            assert await _count(k, f"SELECT COUNT(*) FROM {t} WHERE {col}=?", (B,)) == 0, (t, col)

        # memory: one person, about_person finds both records once
        about = {h.id for h in await k.memory.about_person(A)}
        assert {m1, m2} <= about
        pk2 = json.loads((await _one(k, "SELECT person_keys FROM mem_records WHERE id=?", (m2,)))["person_keys"])
        assert pk2 == [A]
        assert k.memory._names.get(A) == "明" and B not in k.memory._names
        # growth: the scope change is a new immutable version whose parent is the old head (INV-9)
        row = await _one(k, "SELECT * FROM grow_artifacts WHERE id=?", (art.id,))
        assert row["scope"] == f"person:{A}" and row["head_vid"] != head0
        v = await _one(k, "SELECT * FROM grow_versions WHERE vid=?", (row["head_vid"],))
        assert v["change"] == "rescope" and v["parent_vid"] == head0 and json.loads(v["snapshot"])["scope"] == f"person:{A}"
        assert json.loads(row["meta"])["speakers"] == [A]
        cached = k.growth.artifacts._active[art.id]
        assert cached.scope == f"person:{A}" and cached.head_vid == row["head_vid"]
        assert [a.id for a in k.growth.artifacts.active("playbook", [f"person:{A}"])] == [art.id]
        assert (await _one(k, "SELECT about FROM grow_evidence WHERE id=?", (ev,)))["about"] == f"interpretation:{A}"
        # motivation: live and stored intention, merged annoyance
        assert k.motivation._ints[it.id].target_person == A
        assert (await _one(k, "SELECT target_person FROM mot_intentions WHERE id=?", (it.id,)))["target_person"] == A
        an = k.motivation._annoy[A]
        assert B not in k.motivation._annoy
        assert (an["warm"], an["ignored"]) == (4, 2) and "ignored_streak" not in an and "backoff_until" not in an
        # affect
        assert all(e.target != B for e in k.affect.emotions) and any(e.target == A for e in k.affect.emotions)
        # social / governance / core rows: primary-key conflicts merged
        th = await _one(k, "SELECT * FROM soc_threads WHERE thread_id=?", (A,))
        assert (th["channel"], th["chat_id"], th["last_in"]) == ("sim2", "ming_alt", START + 5)  # latest route
        assert json.loads(th["participants"]) == [A]
        assert await _count(k, "SELECT COUNT(*) FROM soc_messages WHERE thread_id=?", (A,)) == 2
        rel = await _one(k, "SELECT * FROM soc_relationships WHERE person_key=?", (A,))
        assert (rel["closeness"], rel["interactions"], rel["last_contact"]) == (0.4, 10, START + 5)
        us = await _one(k, "SELECT * FROM gov_usage WHERE person_key=? AND day=?", (A, day))
        assert (us["minutes"], us["msgs"]) == (15.0, 6)
        assert (await _one(k, "SELECT trust FROM gov_contacts WHERE person_key=?", (A,)))["trust"] == 2
        rq = await _one(k, "SELECT * FROM core_renqing WHERE person_key=?", (A,))
        assert (rq["care_given"], rq["favors_received"]) == (3, 1)
        assert await _count(k, "SELECT COUNT(*) FROM core_move_stats WHERE key=?", (f"p:{A}|chat|vent|listen",)) == 1
        dv = await k.db.kv_get("dialogue", A)
        assert dv == {"thread_id": A, "goal": "陪伴"} and await k.db.kv_get("dialogue", B) is None
        pdv = await k.db.kv_get("dialogue.person", A)
        assert pdv["person_key"] == A and pdv["goal"] == {"aim": "陪他聊聊"} and pdv["my_moves"] == ["open_q", "react"]
        assert await k.db.kv_get("dialogue.person", B) is None
        # world: membership follows the person
        assert A in k.world.members_of(G) and B not in k.world.members_of(G)
        # the migration is recorded
        evs = await k.bus.query(types=[R.REKEYED])
        assert len(evs) == 1 and evs[0].payload["from"] == B and evs[0].payload["into"] == A
        moved = evs[0].payload["moved"]
        assert moved["mem_records.person_keys"] == 2 and moved["soc_threads.thread_id"] == 1
        assert moved["grow_artifacts.scope"] == 1 and not evs[0].payload["errors"]
        merged_ev = (await k.bus.query(types=[R.MERGED]))[-1]
        assert evs[0].causal_parent == merged_ev.id
    finally:
        await k.stop()


async def test_rekey_is_idempotent(tmp_path):
    k = await _kernel(tmp_path)
    try:
        A, B = k.world.person("sim", "a"), k.world.person("sim2", "b")
        await k.memory.note(kind="episodic", content="b 说他明天考试", person_keys=[B], thread_id=B)
        await k.db.transaction(lambda c: c.executemany(
            "INSERT INTO gov_usage (person_key, day, minutes, msgs) VALUES (?,?,?,?)",
            [(B, "2026-11-01", 1.0, 1), (A, "2026-11-01", 2.0, 2)]))
        await _merge(k, "sim:a", "sim2:b")
        snap = [tuple(r) for r in await k.db.fetchall("SELECT * FROM gov_usage ORDER BY person_key, day")]
        recs = [tuple(r) for r in await k.db.fetchall("SELECT id, person_keys, thread_id FROM mem_records")]
        again = await R.rekey(k, B, A)              # run twice: nothing left to move, nothing double counted
        assert again["rows"] == 0 and again["moved"] == {}
        assert [tuple(r) for r in await k.db.fetchall("SELECT * FROM gov_usage ORDER BY person_key, day")] == snap
        assert [tuple(r) for r in await k.db.fetchall("SELECT id, person_keys, thread_id FROM mem_records")] == recs
        assert snap == [(A, "2026-11-01", 3.0, 3, 0, 0)]
        # linking the same pair again is a no-op (no second merge event)
        assert await _merge(k, "sim:a", "sim2:b") == A
        assert len(await k.bus.query(types=[R.MERGED])) == 1
        assert await R.rekey(k, A, A) == {}
        with pytest.raises(ValueError):
            await R.rekey(k, R.SELF, A)
    finally:
        await k.stop()


async def test_declared_identity_at_start_migrates_existing_data(tmp_path):
    k = await _kernel(tmp_path, real=["memory", "motivation"])
    X, Y = k.world.person("sim", "x"), k.world.person("sim2", "y")
    rid = await k.memory.note(kind="episodic", content="y 说他换了新工作", person_keys=[Y], thread_id=Y)
    it = await k.motivation.add_intention({"goal": "问问新工作", "level": "project", "kind": "outreach",
                                           "target_person": Y})
    await k.stop()

    cfg = {"world": {"identities": [{"name": "小X", "handles": ["sim:x", "sim2:y"]}]}}
    k2 = await _kernel(tmp_path, real=["memory", "motivation"], cfg=cfg)
    try:
        assert k2.world.person("sim2", "y") == X and k2.world.entity(Y) is None
        # migrated before the subsystems loaded their state
        assert rid in {h.id for h in await k2.memory.about_person(X)}
        assert k2.motivation._ints[it.id].target_person == X
        assert await _count(k2, "SELECT COUNT(*) FROM mem_records WHERE person_keys LIKE ?", (f'%"{Y}"%',)) == 0
        assert await R.pending_merges(k2) == []
        assert len(await k2.bus.query(types=[R.REKEYED])) == 1
    finally:
        await k2.stop()
    k3 = await _kernel(tmp_path, real=["memory"], cfg=cfg)   # declared again: already one entity, nothing to do
    try:
        assert len(await k3.bus.query(types=[R.MERGED])) == 1
        assert len(await k3.bus.query(types=[R.REKEYED])) == 1
    finally:
        await k3.stop()


async def test_interrupted_merge_is_completed_at_next_start(tmp_path):
    k = await _kernel(tmp_path, real=["memory"])
    A, B = k.world.person("sim", "a"), k.world.person("sim2", "b")
    await k.memory.note(kind="episodic", content="b 养了一只狗", person_keys=[B], thread_id=B)
    k._rekey_sub.cancel()                         # simulate a crash right after world.merged was persisted
    await k.world.link("sim:a", "sim2:b")
    assert await _count(k, "SELECT COUNT(*) FROM mem_records WHERE thread_id=?", (B,)) == 1
    await k.stop()
    k2 = await _kernel(tmp_path, real=["memory"])
    try:
        assert await _count(k2, "SELECT COUNT(*) FROM mem_records WHERE thread_id=?", (B,)) == 0
        assert await _count(k2, "SELECT COUNT(*) FROM mem_records WHERE thread_id=?", (A,)) == 1
        assert await R.pending_merges(k2) == []
    finally:
        await k2.stop()


async def test_hooks_merge_live_buffers_and_registered_callbacks(tmp_path):
    from alive.core.types import ChatLine
    k = await _kernel(tmp_path, real=["memory", "growth"])
    try:
        A, B = k.world.person("sim", "a"), k.world.person("sim2", "b")
        la = [ChatLine(sender="阿A", person_key=A, text="在A这边说一句", ts=START + 10, msg_id="a1")]
        lb = [ChatLine(sender="阿B", person_key=B, text="在B那边说一句", ts=START + 5, msg_id="b1")]
        await k.memory.observe_conversation(A, la, [A])
        await k.memory.observe_conversation(B, lb, [B])
        await k.growth.observe_messages(B, "private", lb)
        seen = []

        async def hook(old, new):
            seen.append((old, new))
        R.register_entity_hook(k, hook)
        await _merge(k, "sim:a", "sim2:b")
        assert seen == [(B, A)]
        buf = k.memory._buffers[A]
        assert B not in k.memory._buffers
        assert [l.msg_id for l in buf.lines] == ["b1", "a1"] and buf.participants == {A}
        assert all(l.person_key == A for l in buf.lines)
        assert B not in k.growth.learner._bufs and k.growth.learner._bufs[A].lines[0].person_key == A
    finally:
        await k.stop()


async def test_extraction_trust_is_the_persons_not_the_spaces(tmp_path):
    """docs/09 §5.1: how far I trust a statement is how far I trust the person who made it - the same in a group
    segment and in a one-to-one one. A person seen in a shared space is known everywhere; someone with no history
    with me anywhere is a stranger everywhere."""
    k = await _kernel(tmp_path, real=["memory", "gov"])
    try:
        P = k.world.person("sim", "p")
        Q = k.world.person("sim", "q")
        G = k.world.group("sim", "g9")
        k.world.note_member(G, P, START)
        gb = _Buf(G)
        gb.participants.add(P)
        db = _Buf(P)                     # a one-to-one space's id is the person's entity id
        db.participants.add(P)
        assert k.memory._trust_for(P, gb, None) == k.memory._trust_for(P, db, None) == int(Trust.PUBLIC)
        qb = _Buf(G)
        qb.participants.add(Q)
        assert k.memory._trust_for(Q, qb, None) == k.memory._trust_for(Q, _Buf(Q), None) == int(Trust.UNTRUSTED)
    finally:
        await k.stop()


async def test_live_caches_in_social_governance_and_core_follow_a_merge(tmp_path):
    """The in-memory side of a merge (docs/22 §3.4): gov safety state, the mind's workspace and caches, dialogue
    stances and move statistics switch to the surviving entity; social drops cached rows to reload merged ones."""
    from alive.core.workspace import WorkspaceItem
    k = await _kernel(tmp_path, real=["gov", "mind", "selfm", "social"],
                      cfg={"channels": [{"kind": "sim", "name": "sim"}, {"kind": "sim", "name": "sim2"}]})
    try:
        a = k.world.person("sim", "ming", "阿明")
        b = k.world.person("sim2", "ming_alt", "阿明")
        k.gov.set_trust(b, Trust.CONTACT)
        k.gov.record_initiative(b, START)
        k.gov._disc[b] = {"first_at": START, "last_at": START, "count": 1}
        k.mind.ws.offer(WorkspaceItem(key=f"space:{b}", kind="space", text="想接着说", salience=0.5, ttl=600,
                                      ref={"thread_id": b}), k.clock.now())
        k.mind._outreach_blocked[b] = START + 99
        k.mind.dialogue.stances[b] = ["agree", "agree"]
        k.mind.dialogue.stances[a] = ["disagree"]
        into = await _merge(k, f"sim:{'ming'}", "sim2:ming_alt")
        old = b if into == a else a
        assert into in (a, b) and old != into
        g = k.gov
        assert old not in g._contacts and old not in g._inits and old not in g._disc
        assert g._inits.get(into) and (g._disc.get(into) or {}).get("count", 0) >= 1
        assert f"space:{old}" not in k.mind.ws.items and k.mind.ws.items[f"space:{into}"].ref["thread_id"] == into
        assert old not in k.mind._outreach_blocked
        st = k.mind.dialogue.stances
        assert old not in st and sorted(st[into]) == ["agree", "agree", "disagree"]
        assert old not in k.social.relations._persons and old not in k.social._threads
    finally:
        await k.stop()
