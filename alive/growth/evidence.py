"""Learning evidence and promotion thresholds (docs/06 §3).

Levels: S1 execution-verified, S2 explicit human feedback, S3 implicit social signal, S4 self-assessment.
- S4 can only create candidates (never satisfies a promotion gate on its own).
- S3 needs >= 3 consistent pieces across different subjects and days.
- S1/S2 can trigger promotion alone.
- Untrusted sources never count as S1/S2: an explicit ``trust`` argument of UNTRUSTED, or any referenced
  event (context_refs) carrying trust UNTRUSTED, downgrades the evidence to S4.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from alive.core.types import EV, Trust, new_id

log = logging.getLogger("alive.growth")

LEVELS = ("S1", "S2", "S3", "S4")
POLARITIES = ("helpful", "harmful", "neutral")


@dataclass
class Evidence:
    id: str
    ts: float
    day: str
    level: str
    polarity: str
    about: str
    entry_id: Optional[str] = None
    subject: Optional[str] = None
    confidence: float = 0.5
    context_refs: list = field(default_factory=list)
    note: str = ""
    trust: Optional[int] = None
    requested_level: Optional[str] = None
    processed: bool = False

    def __getitem__(self, k):
        return getattr(self, k)


def _row(r) -> Evidence:
    return Evidence(id=r["id"], ts=float(r["ts"]), day=r["day"], level=r["level"], polarity=r["polarity"],
                    about=r["about"], entry_id=r["entry_id"], subject=r["subject"], confidence=float(r["confidence"]),
                    context_refs=json.loads(r["context_refs"] or "[]"), note=r["note"] or "",
                    trust=r["trust"], requested_level=r["requested_level"], processed=bool(r["processed"]))


@dataclass
class Gate:
    ok: bool
    reason: str
    score: float = 0.0


def evidence_gate(rows: Sequence[Evidence], s3_cfg: dict) -> Gate:
    """Promotion gate for a playbook entry from its evidence (docs/06 §3.1)."""
    counted = [r for r in rows if r.level in ("S1", "S2", "S3")]
    help12 = [r for r in counted if r.level in ("S1", "S2") and r.polarity == "helpful"]
    harm12 = [r for r in counted if r.level in ("S1", "S2") and r.polarity == "harmful"]
    help3 = [r for r in counted if r.level == "S3" and r.polarity == "helpful"]
    harm3 = [r for r in counted if r.level == "S3" and r.polarity == "harmful"]
    n_help = len(help12) + len(help3)
    n_harm = len(harm12) + len(harm3)
    score = round((n_help - n_harm) / (n_help + n_harm + 1.0), 3)
    if harm12 and len(harm12) >= len(help12):
        return Gate(False, "explicit negative feedback outweighs positive", score)
    if help12:
        return Gate(True, f"{len(help12)} S1/S2 helpful", max(score, 0.5))
    subjects = {r.subject for r in help3 if r.subject}
    days = {r.day for r in help3}
    need = int(s3_cfg.get("min", 3))
    if (len(help3) - len(harm3) >= need and len(harm3) * 3 <= len(help3)
            and len(subjects) >= int(s3_cfg.get("min_subjects", 2)) and len(days) >= int(s3_cfg.get("min_days", 2))):
        return Gate(True, f"{len(help3)} consistent S3 across {len(subjects)} subjects / {len(days)} days", score)
    if not counted:
        return Gate(False, "only S4 / no evidence: candidates only", score)
    return Gate(False, f"insufficient S3 evidence ({len(help3)} helpful, {len(subjects)} subjects, {len(days)} days)",
                score)


class EvidenceStore:
    def __init__(self, kernel):
        self.k = kernel

    async def _refs_untrusted(self, refs: Sequence[str]) -> bool:
        ids = [r for r in refs if isinstance(r, str) and r.startswith("e")][:50]
        if not ids:
            return False
        try:
            v = await self.k.db.fetchval(
                f"SELECT MIN(trust) FROM events WHERE id IN ({','.join('?' * len(ids))})", ids, default=None)
        except Exception:
            return False
        return v is not None and int(v) <= int(Trust.UNTRUSTED)

    async def record(self, *, level: str, polarity: str, about: str, context_refs: Sequence[str] = (),
                     subject: Optional[str] = None, confidence: float = 0.5, entry_id: Optional[str] = None,
                     note: str = "", trust: Optional[int] = None) -> Evidence:
        lv = str(level or "S4").upper()
        lv = lv if lv in LEVELS else "S4"
        pol = str(polarity or "neutral").lower()
        pol = pol if pol in POLARITIES else "neutral"
        requested = None
        untrusted = (trust is not None and int(trust) <= int(Trust.UNTRUSTED)) or await self._refs_untrusted(context_refs)
        if lv in ("S1", "S2") and untrusted:
            requested, lv = lv, "S4"
            log.info("evidence from untrusted source downgraded %s -> S4 (%s)", requested, about)
        now = self.k.clock.now()
        ev = Evidence(id=new_id("ev"), ts=now, day=self.k.clock.today(), level=lv, polarity=pol,
                      about=(about or "general").strip()[:80], entry_id=entry_id, subject=subject,
                      confidence=max(0.0, min(1.0, float(confidence))), context_refs=list(context_refs)[:50],
                      note=(note or "")[:500], trust=int(trust) if trust is not None else None,
                      requested_level=requested)
        await self.k.db.execute(
            "INSERT INTO grow_evidence (id, ts, day, level, requested_level, polarity, about, entry_id, subject, "
            "confidence, context_refs, note, trust, processed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (ev.id, ev.ts, ev.day, ev.level, ev.requested_level, ev.polarity, ev.about, ev.entry_id, ev.subject,
             ev.confidence, json.dumps(ev.context_refs, ensure_ascii=False), ev.note, ev.trust))
        await self.k.bus.emit(EV.EVIDENCE, {"id": ev.id, "level": ev.level, "polarity": ev.polarity, "about": ev.about,
                                            "entry_id": entry_id, "downgraded_from": requested}, source="growth")
        return ev

    async def for_entry(self, entry_id: str) -> list:
        rows = await self.k.db.fetchall("SELECT * FROM grow_evidence WHERE entry_id=? ORDER BY ts", (entry_id,))
        return [_row(r) for r in rows]

    async def unprocessed(self, limit: int = 2000) -> list:
        rows = await self.k.db.fetchall("SELECT * FROM grow_evidence WHERE processed=0 ORDER BY ts LIMIT ?", (limit,))
        return [_row(r) for r in rows]

    async def attach(self, ids: Sequence[str], entry_id: str) -> None:
        if ids:
            await self.k.db.executemany("UPDATE grow_evidence SET entry_id=? WHERE id=? AND entry_id IS NULL",
                                        [(entry_id, i) for i in ids])

    async def mark_processed(self, ids: Sequence[str]) -> None:
        if ids:
            await self.k.db.executemany("UPDATE grow_evidence SET processed=1 WHERE id=?", [(i,) for i in ids])

    async def since(self, ts: float, levels: Sequence[str] = LEVELS, polarity: Optional[str] = None) -> list:
        sql = f"SELECT * FROM grow_evidence WHERE ts >= ? AND level IN ({','.join('?' * len(levels))})"
        params: list = [ts] + list(levels)
        if polarity:
            sql += " AND polarity=?"
            params.append(polarity)
        rows = await self.k.db.fetchall(sql + " ORDER BY ts", params)
        return [_row(r) for r in rows]

    async def counts(self) -> dict:
        rows = await self.k.db.fetchall("SELECT level, polarity, COUNT(*) AS n FROM grow_evidence GROUP BY level, polarity")
        out: dict = {}
        for r in rows:
            out.setdefault(r["level"], {})[r["polarity"]] = r["n"]
        return out

    async def delete_subject(self, subject: str) -> int:
        return await self.k.db.execute("DELETE FROM grow_evidence WHERE subject=?", (subject,))
