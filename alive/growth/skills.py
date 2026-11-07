"""Skill registry (docs/06 §4.1c, docs/12): SKILL.md directories with YAML frontmatter.

Sources: ``<persona>/skills/*/SKILL.md`` (default author ``author``) and ``<data_dir>/skills/*/SKILL.md``
(default author ``self``; frontmatter ``author: user`` marks user-installed skills).
Status: author/user skills are ``active``; self-written skills start as ``candidate`` and can only be
promoted through ``verify(name, passed=True)`` when they declare a deterministic verifier (frontmatter
``verifier:`` or a ``verify*`` file in the directory). A changed self-written skill drops back to
candidate. Status changes are versioned in grow_versions (artifact id ``skill:<name>``, INV-9).
Only metadata (L1) is listed; ``body`` returns the SKILL.md text (L2). Execution is out of scope.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Optional

import yaml

from alive.core.types import EV, new_id

log = logging.getLogger("alive.growth")

AUTHORS = ("author", "user", "self")


def parse_skill_md(text: str) -> tuple:
    """Return (frontmatter dict, body str). Missing/invalid frontmatter -> ({}, text)."""
    t = text.lstrip("﻿")
    if not t.startswith("---"):
        return {}, t
    end = t.find("\n---", 3)
    if end == -1:
        return {}, t
    try:
        fm = yaml.safe_load(t[3:end]) or {}
    except Exception:
        fm = {}
    body = t[end + 4:].lstrip("\n")
    return (fm if isinstance(fm, dict) else {}), body


def _scan(dirs: list) -> list:
    out = []
    for base, default_author in dirs:
        if not base or not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            md = os.path.join(d, "SKILL.md")
            if not os.path.isfile(md):
                continue
            try:
                with open(md, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                continue
            fm, body = parse_skill_md(text)
            files = os.listdir(d)
            verifier = fm.get("verifier") or next((x for x in sorted(files) if x.lower().startswith("verif")), None)
            author = str(fm.get("author") or default_author).lower()
            out.append({"name": str(fm.get("name") or name).strip(), "description": str(fm.get("description") or "").strip(),
                        "author": author if author in AUTHORS else default_author, "path": d,
                        "verifier": json.dumps(verifier, ensure_ascii=False) if verifier else None,
                        "digest": hashlib.sha1(text.encode("utf-8")).hexdigest(), "body": body,
                        "side_effect_level": fm.get("side_effect_level", "read_only")})
    return out


class SkillRegistry:
    def __init__(self, kernel):
        self.k = kernel
        self._skills: dict = {}

    def _dirs(self) -> list:
        dirs = []
        pdir = getattr(self.k, "persona_dir", None) or self.k.cfg.get("persona_dir")
        if pdir:
            dirs.append((os.path.join(pdir, "skills"), "author"))
        ddir = getattr(self.k, "data_dir", None) or self.k.cfg.get("data_dir")
        if ddir:
            dirs.append((os.path.join(ddir, "skills"), "self"))
        return dirs

    async def load(self) -> int:
        found = await asyncio.get_running_loop().run_in_executor(None, _scan, self._dirs())
        rows = {r["name"]: dict(r) for r in await self.k.db.fetchall("SELECT * FROM grow_skills")}
        now = self.k.clock.now()
        skills: dict = {}
        for s in found:
            old = rows.get(s["name"])
            if s["author"] in ("author", "user"):
                status = "active"
            elif old is not None and old["digest"] == s["digest"]:
                status = old["status"]
            else:
                status = "candidate"
            s["status"] = status
            skills[s["name"]] = s
            if old is None or old["digest"] != s["digest"] or old["status"] != status:
                await self.k.db.execute(
                    "INSERT OR REPLACE INTO grow_skills (name, path, author, status, description, verifier, digest, "
                    "updated, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["name"], s["path"], s["author"], status, s["description"], s["verifier"], s["digest"], now,
                     json.dumps({"side_effect_level": s["side_effect_level"]}, ensure_ascii=False)))
                if old is not None and old["status"] != status:
                    await self._version(s["name"], status, "reloaded: content changed")
        self._skills = skills
        return len(skills)

    def list(self) -> list:
        return [{"name": s["name"], "description": s["description"], "status": s["status"], "author": s["author"],
                 "path": s["path"], "has_verifier": bool(s["verifier"])}
                for s in sorted(self._skills.values(), key=lambda x: x["name"])]

    def get(self, name: str) -> Optional[dict]:
        s = self._skills.get(name)
        return dict(s) if s else None

    def body(self, name: str) -> Optional[str]:
        s = self._skills.get(name)
        return s["body"] if s else None

    async def _version(self, name: str, status: str, reason: str, eval_score: Optional[float] = None) -> None:
        aid = f"skill:{name}"
        mx = await self.k.db.fetchval("SELECT MAX(version) FROM grow_versions WHERE artifact_id=?", (aid,), default=None)
        parent = await self.k.db.fetchval("SELECT vid FROM grow_versions WHERE artifact_id=? ORDER BY version DESC LIMIT 1",
                                          (aid,), default=None)
        s = self._skills.get(name) or {}
        snap = {"name": name, "status": status, "author": s.get("author"), "digest": s.get("digest")}
        await self.k.db.execute(
            "INSERT INTO grow_versions (vid, artifact_id, version, parent_vid, merged_vid, ts, change, snapshot, evidence, "
            "eval_score, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("v"), aid, int(mx or 0) + 1, parent, None, self.k.clock.now(), "status",
             json.dumps(snap, ensure_ascii=False), "[]", eval_score, reason[:300]))
        await self.k.bus.emit(EV.LEARN_CHANGE, {"layer": "L2", "artifact": aid, "type": "skill", "change": "status",
                                                "status": status, "reason": reason[:120]}, source="growth")

    async def set_status(self, name: str, status: str, reason: str = "", eval_score: Optional[float] = None) -> bool:
        s = self._skills.get(name)
        if s is None or status not in ("candidate", "active", "retired") or s["status"] == status:
            return False
        s["status"] = status
        await self.k.db.execute("UPDATE grow_skills SET status=?, updated=? WHERE name=?",
                                (status, self.k.clock.now(), name))
        await self._version(name, status, reason, eval_score)
        return True

    async def verify(self, name: str, passed: bool, score: Optional[float] = None, note: str = "") -> dict:
        """Record a verifier run. Self-written skills are promoted only if they declare a verifier and it passed."""
        s = self._skills.get(name)
        if s is None:
            return {"ok": False, "reason": "unknown skill"}
        if s["author"] != "self":
            return {"ok": True, "status": s["status"], "reason": "author/user skill"}
        if not s["verifier"]:
            return {"ok": False, "status": s["status"], "reason": "self-written skill has no verifier; cannot promote"}
        if passed:
            await self.set_status(name, "active", f"verifier passed {note}".strip(), score)
            return {"ok": True, "status": "active"}
        if s["status"] == "active":
            await self.set_status(name, "candidate", f"verifier failed {note}".strip(), score)
        return {"ok": False, "status": self._skills[name]["status"], "reason": "verifier failed"}
