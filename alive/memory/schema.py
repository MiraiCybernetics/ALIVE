"""Memory tables (docs/03 §3.1). Registered at import; applied by the kernel's Database at start.

mem_records   one row per memory record (all MemoryRecord fields; JSON for list fields)
mem_fts       FTS5 trigram index over content (external content = mem_records, kept in sync by triggers)
mem_vec       embedding blobs (float32), loaded into an in-memory index at start
mem_ent       entity / person index (role: entity | person | told_by) for spreading activation
mem_deps      derived_from edges (record -> depends_on) for invalidation propagation (§4.3)
mem_recheck   queue of records to re-evaluate during sleep (stale dependents, conflicts)
mem_preimages pre-images of rewritten summaries (rollback; 25% max-loss rule, §7)
"""
from __future__ import annotations

from alive.runtime.store import register_schema

SCHEMA_V1 = [
    """CREATE TABLE IF NOT EXISTS mem_records (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        entities TEXT NOT NULL DEFAULT '[]',
        person_keys TEXT NOT NULL DEFAULT '[]',
        observed_at REAL NOT NULL,
        ref_start REAL, ref_end REAL, ref_granularity TEXT, ref_raw TEXT,
        valid_from REAL, valid_to REAL,
        recorded_at REAL NOT NULL, expired_at REAL,
        source TEXT NOT NULL,
        told_by TEXT,
        trust INTEGER NOT NULL DEFAULT 4,
        evidence TEXT NOT NULL DEFAULT '[]',
        importance INTEGER NOT NULL DEFAULT 3,
        aff_valence REAL NOT NULL DEFAULT 0,
        aff_arousal REAL NOT NULL DEFAULT 0,
        aff_label TEXT,
        aff_peak INTEGER NOT NULL DEFAULT 0,
        strength REAL NOT NULL,
        access_count INTEGER NOT NULL DEFAULT 0,
        last_access REAL NOT NULL,
        derived_from TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'active',
        version INTEGER NOT NULL DEFAULT 1,
        parent_version TEXT,
        thread_id TEXT,
        conflict_with TEXT NOT NULL DEFAULT '[]',
        meta TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS ix_mem_kind_status ON mem_records(kind, status)",
    "CREATE INDEX IF NOT EXISTS ix_mem_observed ON mem_records(observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_mem_ref ON mem_records(ref_start) WHERE ref_start IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_mem_thread ON mem_records(thread_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_mem_status ON mem_records(status)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
        content, content='mem_records', content_rowid='rowid', tokenize='trigram')""",
    """CREATE TRIGGER IF NOT EXISTS mem_records_ai AFTER INSERT ON mem_records BEGIN
        INSERT INTO mem_fts(rowid, content) VALUES (new.rowid, new.content);
    END""",
    """CREATE TRIGGER IF NOT EXISTS mem_records_ad AFTER DELETE ON mem_records BEGIN
        INSERT INTO mem_fts(mem_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    END""",
    """CREATE TRIGGER IF NOT EXISTS mem_records_au AFTER UPDATE OF content ON mem_records BEGIN
        INSERT INTO mem_fts(mem_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
        INSERT INTO mem_fts(rowid, content) VALUES (new.rowid, new.content);
    END""",
    """CREATE TABLE IF NOT EXISTS mem_vec (
        id TEXT PRIMARY KEY,
        embedder TEXT NOT NULL,
        dim INTEGER NOT NULL,
        vec BLOB NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS mem_ent (
        key TEXT NOT NULL,
        record_id TEXT NOT NULL,
        role TEXT NOT NULL,
        PRIMARY KEY (key, record_id, role)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS ix_mem_ent_rec ON mem_ent(record_id)",
    """CREATE TABLE IF NOT EXISTS mem_deps (
        depends_on TEXT NOT NULL,
        record_id TEXT NOT NULL,
        PRIMARY KEY (depends_on, record_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS ix_mem_deps_rec ON mem_deps(record_id)",
    """CREATE TABLE IF NOT EXISTS mem_recheck (
        record_id TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        depth INTEGER NOT NULL DEFAULT 1,
        queued_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS mem_preimages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT NOT NULL,
        content TEXT NOT NULL,
        meta TEXT,
        saved_at REAL NOT NULL,
        reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_mem_pre_rec ON mem_preimages(record_id)",
]

register_schema("memory", 1, SCHEMA_V1)

RECORD_COLS = ("id", "kind", "content", "entities", "person_keys", "observed_at", "ref_start", "ref_end",
               "ref_granularity", "ref_raw", "valid_from", "valid_to", "recorded_at", "expired_at", "source",
               "told_by", "trust", "evidence", "importance", "aff_valence", "aff_arousal", "aff_label",
               "aff_peak", "strength", "access_count", "last_access", "derived_from", "status", "version",
               "parent_version", "thread_id", "conflict_with", "meta")

INSERT_SQL = "INSERT INTO mem_records (%s) VALUES (%s)" % (",".join(RECORD_COLS), ",".join("?" * len(RECORD_COLS)))
