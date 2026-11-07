"""SelfModel implementation (docs/08).

- Single writer of the self state (INV-3): all mutations go through this object on the event loop.
- ``state_text()`` is sync and cheap: it renders cached values; async sources (open loops) are
  refreshed by a background job.
- Today's digest is built ONLY from real outcomes recorded via ``record_outcome`` (docs/08 §3).
- Narrative (slow zone) updates happen in sleep under gates: change-size cap, a readiness hazard since the last edit (~weekly),
  identity-consistency check, and an append-only version table (INV-9).
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
from collections import deque
from typing import Optional, Sequence

from alive.core.types import EV, new_id
from alive.interfaces import SelfModelSystem
from alive.runtime import chance
from alive.runtime.store import register_schema
from alive.self_model import prompts
from alive.self_model.identity import Identity

log = logging.getLogger("alive.self")

VOICE_POOL = 60             # learned voice samples kept for the daily rotation (technical bound)
VOICE_TEMPERATURE = 0.3     # persona samples (1.0) vs learned (~0.55): ~4.5:1 per line
# Narrative edits (slow zone): readiness to revise the self-narrative grows with the days since the last edit -
# Weibull CDF, scale 7 days (the former fixed weekly gate is now its centre), shape 3: ~12% after 3.5 days, ~50%
# after 6.2, ~63% after 7, ~95% after 10. A night apart is the technical floor (one sleep update per night).
NARRATIVE_SCALE_DAYS = 7.0
NARRATIVE_SHAPE = 3.0
NARRATIVE_FLOOR_DAYS = 1.0

register_schema("self", 1, [
    """CREATE TABLE IF NOT EXISTS self_outcomes (
        id TEXT PRIMARY KEY, ts REAL, day TEXT, summary TEXT, event_id TEXT, important INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_self_outcomes_day ON self_outcomes(day, ts)",
    """CREATE TABLE IF NOT EXISTS self_notes (
        id TEXT PRIMARY KEY, ts REAL, note TEXT, source_event TEXT, status TEXT DEFAULT 'open')""",
    """CREATE TABLE IF NOT EXISTS self_versions (
        id TEXT PRIMARY KEY, ts REAL, file TEXT, content TEXT, parent TEXT, reason TEXT, score REAL)""",
    """CREATE TABLE IF NOT EXISTS self_voice (
        id TEXT PRIMARY KEY, ts REAL, text TEXT, score REAL)""",
])


class SelfModel(SelfModelSystem):
    def __init__(self, kernel):
        super().__init__(kernel)
        self.override_dir = os.path.join(kernel.data_dir, "identity")
        self.identity = Identity.load(kernel.persona_dir, self.override_dir)
        self._digest = self.identity.digest()
        self.focus: str = ""
        self.today: deque = deque(maxlen=60)
        self.today_day = ""
        self.recent_notes: deque = deque(maxlen=5)
        self.open_loops_cache: list = []
        self.learned_voice: list = []
        self._voice_scores: dict = {}
        self._voice_cache: dict = {}
        self.cfg = kernel.cfg.section("self_model")

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        day = self.k.clock.today()
        rows = await self.k.db.fetchall("SELECT summary FROM self_outcomes WHERE day=? ORDER BY ts", (day,))
        self.today_day = day
        self.today.extend(r["summary"] for r in rows)
        rows = await self.k.db.fetchall("SELECT note FROM self_notes WHERE status='open' ORDER BY ts DESC LIMIT 5")
        self.recent_notes.extend(reversed([r["note"] for r in rows]))
        await self._load_voice()
        self.k.scheduler.every(60.0, self._refresh_loops, "self.refresh_open_loops", initial_delay=0.0)

    async def _load_voice(self) -> None:
        # the pool the daily rotation draws from (VOICE_POOL is a technical bound on what is kept in memory)
        rows = await self.k.db.fetchall("SELECT text, score FROM self_voice ORDER BY score DESC, ts DESC LIMIT ?",
                                        (VOICE_POOL,))
        self.learned_voice = [r["text"] for r in rows]
        self._voice_scores = {r["text"]: float(r["score"] if r["score"] is not None else 0.5) for r in rows}
        self._voice_cache = {}

    async def _refresh_loops(self) -> None:
        if self.k.motivation is None:
            return
        try:
            self.open_loops_cache = list(await self.k.motivation.open_loops(7))
        except Exception:
            log.debug("open loops refresh failed", exc_info=True)

    # ------------------------------------------------------------------ sync reads
    def identity_digest(self) -> str:
        return self._digest

    def name(self) -> str:
        return self.identity.name

    def voice_samples(self, n: int = 6) -> list:
        """Which of my typical lines I have "in my ear" today: a Gumbel-top-n draw over the persona's samples
        (score 1.0) and the learned ones (their score, ~0.5-0.6), temperature 0.3 - persona lines dominate (~2/3)
        but learned ones rotate in. Re-drawn once per local day (chance.daily seed), so the cached prompt prefix
        stays stable within a day."""
        base = list(self.identity.voice_samples)
        learned = [v for v in self.learned_voice if v not in base]
        pool = list(dict.fromkeys(base + learned))
        if len(pool) <= n:
            return pool
        day = self.k.clock.today()
        key = (day, n, len(pool), len(self.learned_voice))
        hit = self._voice_cache.get(key)
        if hit is not None:
            return list(hit)
        seed = chance.daily(self.k, "self.voice_seed", lambda r: r.random())
        scores = {v: (1.0 if v in base else float(self._voice_scores.get(v, 0.5))) for v in pool}
        picked = chance.gumbel_topk(random.Random(f"{seed}|{n}"), scores, n, VOICE_TEMPERATURE)
        self._voice_cache = {key: tuple(picked)}
        return picked

    def set_focus(self, text: str) -> None:
        self.focus = text[:80]

    def _roll_day(self) -> None:
        d = self.k.clock.today()
        if d != self.today_day:
            self.today_day = d
            self.today.clear()

    def today_digest(self, max_items: int = 8) -> str:
        self._roll_day()
        items = list(self.today)
        if not items:
            return "今天到现在还没做什么特别的事。"
        head = f"（今天共 {len(items)} 件事，最近的几件）" if len(items) > max_items else ""
        return head + "；".join(items[-max_items:])

    def state_text(self) -> str:
        k = self.k
        parts = [f"现在是 {k.clock.fmt()}。"]
        if k.affect is not None:
            lab = k.affect.labels()
            mood = lab.get("mood") or ""
            emo = lab.get("emotion") or ""
            energy = lab.get("energy") or ""
            parts.append(f"我的状态：{mood}{('，' + emo) if emo else ''}{('；' + energy) if energy else ''}。")
        if k.mind is not None:
            try:
                mode = k.mind.mode()
                if mode in ("focus", "drowsy", "overwhelmed"):
                    parts.append({"focus": "我正专注在自己的事情上。", "drowsy": "我有点困了。",
                                  "overwhelmed": "我现在有点忙不过来。"}[mode])
            except Exception:
                pass
        if self.focus:
            parts.append(f"我刚才在：{self.focus}。")
        tex = getattr(k, "texture", None)
        if tex is not None:
            d = tex.describe()
            if d:
                parts.append(f"最近的我：{d}。")
            ints = tex.top_interests(2)
            if ints:
                parts.append("最近感兴趣的：" + "、".join(ints) + "。")
        parts.append("今天：" + self.today_digest())
        if self.open_loops_cache:
            parts.append("心里挂着的事：" + "；".join(self.open_loops_cache[:5]))
        if self.recent_notes:
            parts.append("最近对自己的观察：" + "；".join(list(self.recent_notes)[-2:]))
        return "\n".join(parts)

    def status(self) -> dict:
        return {"name": self.name(), "focus": self.focus, "today": list(self.today)[-10:],
                "open_loops": self.open_loops_cache, "notes": list(self.recent_notes)}

    # ------------------------------------------------------------------ writes (single writer)
    async def record_outcome(self, summary: str, event_id: str = "", important: bool = False) -> None:
        summary = (summary or "").strip()
        if not summary:
            return
        self._roll_day()
        now = self.k.clock.now()
        self.today.append(summary[:120])
        await self.k.db.execute("INSERT INTO self_outcomes VALUES (?,?,?,?,?,?)",
                                (new_id("o"), now, self.k.clock.today(now), summary[:300], event_id, int(important)))

    async def add_self_notes(self, notes: Sequence[str], source_event: str = "") -> None:
        now = self.k.clock.now()
        rows = []
        for n in notes:
            n = (n or "").strip()
            if not n or n in self.recent_notes:
                continue
            self.recent_notes.append(n[:120])
            rows.append((new_id("sn"), now, n[:500], source_event, "open"))
        if rows:
            await self.k.db.executemany("INSERT INTO self_notes VALUES (?,?,?,?,?)", rows)
            await self.k.bus.emit(EV.SELF_NOTE, {"notes": [r[2] for r in rows]}, source="selfm",
                                  causal_parent=source_event or None)

    async def add_voice_sample(self, text: str, score: float = 0.5) -> None:
        text = text.strip()
        if 2 <= len(text) <= 60:
            await self.k.db.execute("INSERT INTO self_voice VALUES (?,?,?,?)",
                                    (new_id("v"), self.k.clock.now(), text, score))

    async def notes_since(self, since: float) -> list:
        rows = await self.k.db.fetchall("SELECT note FROM self_notes WHERE ts>=? ORDER BY ts", (since,))
        return [r["note"] for r in rows]

    async def outcomes_between(self, since: float, until: float) -> list:
        rows = await self.k.db.fetchall("SELECT ts, summary FROM self_outcomes WHERE ts>=? AND ts<? ORDER BY ts",
                                        (since, until))
        return [(r["ts"], r["summary"]) for r in rows]

    # ------------------------------------------------------------------ sleep stage 'self'
    async def sleep_update(self, ctx: dict) -> dict:
        """Update slow-zone narrative + voice samples + daily journal under gates (docs/08, 06 §8)."""
        k = self.k
        now = ctx.get("now", k.clock.now())
        since = ctx.get("since", now - 86400)
        report: dict = {"journal": False, "narrative": "unchanged", "voice": 0}
        if k.gov is not None and getattr(k.gov, "learning_paused", False):
            report["skipped"] = "learning_paused"
            return report
        outcomes = await self.outcomes_between(since, now)
        notes = await self.notes_since(since)
        reflections = ctx.get("reflections") or []
        # 1) daily journal entry (append-only file, human readable)
        jdir = os.path.join(k.data_dir, "journal")
        os.makedirs(jdir, exist_ok=True)
        day = k.clock.today(since + 1)
        data = await k.llm.json("self.journal", prompts.JOURNAL.format(
            name=self.name(), day=day,
            outcomes="\n".join(f"- {s}" for _, s in outcomes[-40:]) or "（几乎没发生什么）",
            notes="\n".join(f"- {n}" for n in notes[-10:]) or "（无）",
            reflections="\n".join(f"- {r}" for r in reflections[:8]) or "（无）"),
            tier="deliberate", schema={"entry": "", "self_observations": [], "narrative_change": "",
                                        "change_reason": ""}, identity_critical=True)
        entry = (data.get("entry") or "").strip()
        if entry:
            week = k.clock.local(since + 1).strftime("%G-W%V")
            with open(os.path.join(jdir, f"{week}.md"), "a", encoding="utf-8") as f:
                f.write(f"\n## {day}\n\n{entry}\n")
            report["journal"] = True
        obs = [o for o in data.get("self_observations", []) if isinstance(o, str)]
        if obs:
            await self.add_self_notes(obs[:3], "sleep")
        # 2) narrative change (gated): readiness grows with the days since the last edit; must pass consistency check
        change = (data.get("narrative_change") or "").strip()
        if change and await self._narrative_change_allowed(now):
            ok = await self._consistent_with_core(change)
            if ok:
                await self._apply_narrative_change(change, data.get("change_reason", ""), now)
                report["narrative"] = "changed"
            else:
                report["narrative"] = "rejected_inconsistent"
        # 3) voice samples: my own short messages from the day that people answered warmly
        for txt in (ctx.get("good_utterances") or [])[:5]:
            await self.add_voice_sample(txt, 0.6)
            report["voice"] += 1
        await self._load_voice()
        # close old notes (they were digested into the journal)
        await k.db.execute("UPDATE self_notes SET status='digested' WHERE ts<? AND status='open'", (now - 3 * 86400,))
        await k.bus.emit(EV.SELF_UPDATE, report, source="selfm")
        return report

    def narrative_readiness(self, days_since: float) -> float:
        """P(open to revising my self-narrative) after ``days_since`` days without an edit (Weibull CDF)."""
        if days_since < NARRATIVE_FLOOR_DAYS:
            return 0.0
        return 1.0 - math.exp(-(max(0.0, days_since) / NARRATIVE_SCALE_DAYS) ** NARRATIVE_SHAPE)

    async def _narrative_change_allowed(self, now: float) -> bool:
        last = await self.k.db.fetchval("SELECT MAX(ts) FROM self_versions WHERE file='narrative.md'")
        if last is None:
            return True
        return chance.bernoulli(chance.rng_of(self.k), self.narrative_readiness((now - float(last)) / 86400.0))

    async def _consistent_with_core(self, change: str) -> bool:
        d = await self.k.llm.json("self.consistency", prompts.CONSISTENCY.format(
            core=self.identity.core_md[:1500], change=change), tier="fast",
            schema={"consistent": False, "reason": ""})
        return bool(d.get("consistent"))

    async def _apply_narrative_change(self, change: str, reason: str, now: float) -> None:
        cur = self.identity.narrative_md
        new = cur.rstrip() + f"\n\n{change.strip()}\n"
        parent = await self.k.db.fetchval(
            "SELECT id FROM self_versions WHERE file='narrative.md' ORDER BY ts DESC LIMIT 1")
        vid = new_id("sv")
        await self.k.db.execute("INSERT INTO self_versions VALUES (?,?,?,?,?,?,?)",
                                (vid, now, "narrative.md", new, parent, reason[:300], None))
        os.makedirs(self.override_dir, exist_ok=True)
        with open(os.path.join(self.override_dir, "narrative.md"), "w", encoding="utf-8") as f:
            f.write(new)
        self.identity.narrative_md = new
        self._digest = self.identity.digest()
        await self.k.bus.emit(EV.LEARN_CHANGE, {"layer": "self", "file": "narrative.md", "version": vid,
                                                "parent": parent, "change": change[:300]}, source="selfm")

    async def rollback_narrative(self, version_id: Optional[str] = None) -> bool:
        """Rollback = pointer switch to a previous version (or the persona original if None)."""
        if version_id is None:
            p = os.path.join(self.override_dir, "narrative.md")
            if os.path.exists(p):
                os.remove(p)
        else:
            content = await self.k.db.fetchval("SELECT content FROM self_versions WHERE id=?", (version_id,))
            if content is None:
                return False
            with open(os.path.join(self.override_dir, "narrative.md"), "w", encoding="utf-8") as f:
                f.write(content)
        self.identity = Identity.load(self.k.persona_dir, self.override_dir)
        self._digest = self.identity.digest()
        return True
