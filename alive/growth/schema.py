"""Growth tables (docs/06). Registered at import.

grow_evidence   learning evidence (S1–S4, polarity, behaviour tag, subject, context refs)
grow_artifacts  materialised current state of every L2 artifact (playbook entries, expressions, jargon)
grow_versions   append-only version log (INV-9): every change is an immutable row with parent pointer,
                evidence ids and eval score; ``grow_artifacts.head_vid`` points at the current version and a
                rollback is a pointer switch. UPDATE/DELETE on this table are rejected by triggers, except a
                privacy redaction (reason 'privacy-redacted') that replaces a snapshot with a stub.
grow_skills     installed SKILL.md skills: author, status (self-written skills need a verifier to be promoted)
"""
from __future__ import annotations

from alive.runtime.store import register_schema

register_schema("growth", 1, [
    """CREATE TABLE IF NOT EXISTS grow_evidence (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        day TEXT NOT NULL,
        level TEXT NOT NULL,
        requested_level TEXT,
        polarity TEXT NOT NULL,
        about TEXT NOT NULL,
        entry_id TEXT,
        subject TEXT,
        confidence REAL NOT NULL DEFAULT 0.5,
        context_refs TEXT NOT NULL DEFAULT '[]',
        note TEXT,
        trust INTEGER,
        processed INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS ix_grow_ev_entry ON grow_evidence(entry_id)",
    "CREATE INDEX IF NOT EXISTS ix_grow_ev_about ON grow_evidence(about)",
    "CREATE INDEX IF NOT EXISTS ix_grow_ev_proc ON grow_evidence(processed, ts)",
    """CREATE TABLE IF NOT EXISTS grow_artifacts (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        situation TEXT NOT NULL DEFAULT '',
        tag TEXT,
        scope TEXT NOT NULL DEFAULT 'global',
        examples TEXT NOT NULL DEFAULT '[]',
        jargon TEXT,
        helpful INTEGER NOT NULL DEFAULT 0,
        harmful INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        provenance TEXT NOT NULL DEFAULT '[]',
        source TEXT,
        author TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_used REAL,
        use_count INTEGER NOT NULL DEFAULT 0,
        seen_count INTEGER NOT NULL DEFAULT 1,
        version INTEGER NOT NULL DEFAULT 1,
        head_vid TEXT,
        norm_key TEXT,
        meta TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS ix_grow_art_ts ON grow_artifacts(type, status)",
    "CREATE INDEX IF NOT EXISTS ix_grow_art_scope ON grow_artifacts(scope)",
    "CREATE INDEX IF NOT EXISTS ix_grow_art_key ON grow_artifacts(type, norm_key)",
    """CREATE TABLE IF NOT EXISTS grow_versions (
        vid TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        parent_vid TEXT,
        merged_vid TEXT,
        ts REAL NOT NULL,
        change TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '[]',
        eval_score REAL,
        reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_grow_ver_art ON grow_versions(artifact_id, version)",
    # the only permitted UPDATE is privacy redaction (forget_person): snapshot replaced by a stub
    """CREATE TRIGGER IF NOT EXISTS grow_versions_no_update BEFORE UPDATE ON grow_versions
        WHEN NEW.reason IS NOT 'privacy-redacted' OR NEW.vid != OLD.vid OR NEW.artifact_id != OLD.artifact_id
        OR NEW.version != OLD.version OR NEW.parent_vid IS NOT OLD.parent_vid BEGIN
        SELECT RAISE(ABORT, 'grow_versions is append-only (INV-9)');
    END""",
    """CREATE TRIGGER IF NOT EXISTS grow_versions_no_delete BEFORE DELETE ON grow_versions BEGIN
        SELECT RAISE(ABORT, 'grow_versions is append-only (INV-9)');
    END""",
    """CREATE TABLE IF NOT EXISTS grow_skills (
        name TEXT PRIMARY KEY,
        path TEXT,
        author TEXT,
        status TEXT NOT NULL,
        description TEXT,
        verifier TEXT,
        digest TEXT,
        updated REAL,
        meta TEXT NOT NULL DEFAULT '{}'
    )""",
])
