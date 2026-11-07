"""Expression and jargon learners (docs/06 §4.1b, MaiBot learn_style / learn_jargon).

``observe`` only buffers USER lines per thread (my own lines are dropped at the door, so the model never
sees them) and returns. A per-thread watcher runs the learners when enough lines accumulate
(group/private thresholds) or after an idle period. Each run makes two fast-tier calls (style rules,
jargon) concurrently; outputs are validated deterministically before becoming *candidates*:

- every rule must carry an example that is (nearly) an original USER sentence; rules whose example
  matches one of my own sentences are dropped;
- no person names, no '@', no URLs, length limits, learning blacklist (persons/threads/words);
- jargon terms must actually occur in user lines and must not be a participant name.

Candidates enter the shared L2 lifecycle (static check + AI review at night before activation).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import Optional, Sequence

from alive.core.types import ChatLine
from alive.growth.prompts import (JARGON_SCHEMA, JARGON_SYSTEM, JARGON_USER, LEARN_STYLE_SCHEMA, LEARN_STYLE_SYSTEM,
                                  LEARN_STYLE_USER)
from alive.growth.util import containment, norm, truncate

log = logging.getLogger("alive.growth")

_URL = re.compile(r"https?://|www\.", re.I)


class _LBuf:
    def __init__(self, thread_id: str, chat_type: str):
        self.thread_id = thread_id
        self.chat_type = chat_type
        self.lines: list = []
        self.mine: deque = deque(maxlen=50)     # my recent sentences (only to reject leaked rules)
        self.names: dict = {}
        self.last_obs = 0.0
        self.watcher = None
        self.running = False
        self.lock = asyncio.Lock()
        self.seen: deque = deque(maxlen=400)


class ExpressionLearner:
    def __init__(self, growth):
        self.g = growth
        self.k = growth.k
        self.cfg = growth.cfg["learning"]
        self._bufs: dict = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self.runs = 0

    # ------------------------------------------------------------------ config helpers
    def _blacklisted_person(self, pk: str) -> bool:
        bl = self.cfg.get("blacklist") or {}
        return pk in set(bl.get("persons") or [])

    def _blacklisted_thread(self, tid: str) -> bool:
        bl = self.cfg.get("blacklist") or {}
        return tid in set(bl.get("threads") or [])

    def _bad_words(self) -> list:
        return list((self.cfg.get("blacklist") or {}).get("words") or [])

    def _threshold(self, chat_type: str) -> int:
        return int(self.cfg["group_lines"] if chat_type == "group" else self.cfg["private_lines"])

    # ------------------------------------------------------------------ observe
    async def observe(self, thread_id: str, chat_type: str, lines: Sequence[ChatLine]) -> None:
        if not lines or self._blacklisted_thread(thread_id):
            return
        buf = self._bufs.get(thread_id)
        if buf is None:
            buf = self._bufs[thread_id] = _LBuf(thread_id, chat_type or "group")
        buf.chat_type = chat_type or buf.chat_type
        added = 0
        for ln in lines:
            text = (ln.text or "").strip()
            if not text:
                continue
            if ln.mine or not ln.person_key:
                buf.mine.append(text)             # never learned from; used to reject leaked rules
                continue
            key = ln.msg_id or f"{ln.ts}|{ln.person_key}|{hash(text)}"
            if key in buf.seen or self._blacklisted_person(ln.person_key):
                continue
            buf.seen.append(key)
            if ln.sender:
                buf.names[ln.person_key] = ln.sender
                self.g._names[ln.person_key] = ln.sender
            buf.lines.append(ln)
            added += 1
        if not added:
            return
        buf.last_obs = self.k.clock.now()
        if len(buf.lines) >= self._threshold(buf.chat_type):
            if not buf.running:
                buf.running = True
                self.g._spawn(lambda b=buf: self._run(b), f"grow.learn:{thread_id}")
        elif buf.watcher is None or buf.watcher.done:
            buf.watcher = self.k.scheduler.spawn(lambda b=buf: self._watch(b), f"grow.learn_idle:{thread_id}")

    async def _watch(self, buf: _LBuf) -> None:
        idle = float(self.cfg["idle_s"])
        try:
            while buf.lines:
                wait = buf.last_obs + idle - self.k.clock.now()
                if wait > 0:
                    await self.k.clock.sleep(wait)
                    continue
                if len(buf.lines) < int(self.cfg["min_lines"]):
                    return  # too little material; keep it for the next batch
                await self._run(buf)
                return
        finally:
            buf.watcher = None

    async def flush_all(self, force: bool = True) -> int:
        n = 0
        for buf in list(self._bufs.values()):
            if buf.lines and (force or len(buf.lines) >= int(self.cfg["min_lines"])):
                n += await self._run(buf)
        return n

    async def forget_person(self, pk: str) -> None:
        """Drop a person's buffered lines; waits for an in-flight learning run on that buffer first."""
        for b in list(self._bufs.values()):
            if pk not in b.names and not any(l.person_key == pk for l in b.lines):
                continue
            async with b.lock:
                b.lines = [l for l in b.lines if l.person_key != pk]
                b.names.pop(pk, None)

    async def rekey_entity(self, old: str, new: str) -> None:
        """Entity merge: buffered lines and names move from ``old`` to ``new``; the old one-to-one space's
        buffer joins the surviving space's (waits for an in-flight learning run on each buffer)."""
        for tid, b in list(self._bufs.items()):
            if tid != old and old not in b.names and not any(l.person_key == old for l in b.lines):
                continue
            async with b.lock:
                for l in b.lines:
                    if l.person_key == old:
                        l.person_key = new
                if old in b.names:
                    b.names.setdefault(new, b.names.pop(old))
        src = self._bufs.get(old)
        if src is None:
            return
        dst = self._bufs.get(new)
        if dst is None or dst is src:
            self._bufs.pop(old, None)
            src.thread_id = new
            self._bufs[new] = src
            return
        async with src.lock:
            async with dst.lock:
                dst.lines = sorted(dst.lines + src.lines, key=lambda l: l.ts)
                for k, v in src.names.items():
                    dst.names.setdefault(k, v)
                for key in src.seen:
                    if key not in dst.seen:
                        dst.seen.append(key)
                dst.mine.extend(src.mine)
                dst.last_obs = max(dst.last_obs, src.last_obs)
                src.lines = []
        self._bufs.pop(old, None)

    # ------------------------------------------------------------------ learning run
    async def _run(self, buf: _LBuf) -> int:
        if self._sem is None:
            self._sem = asyncio.Semaphore(int(self.cfg["concurrency"]))
        async with buf.lock:
            buf.running = False
            batch = list(buf.lines[: int(self.cfg["max_batch"])])
            if not batch:
                return 0
            if self.g.learning_paused():
                del buf.lines[: len(batch)]
                return 0
            self.g._active += 1
            try:
                async with self._sem:
                    made = await self._learn(buf, batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("expression learning failed for %s", buf.thread_id)
                made = 0
            finally:
                self.g._active -= 1
            del buf.lines[: len(batch)]
            self.runs += 1
            return made

    def _scope(self, buf: _LBuf, batch: list) -> str:
        if buf.chat_type == "group":
            return f"channel:{buf.thread_id}"
        pks = [l.person_key for l in batch if l.person_key]
        return f"person:{pks[-1]}" if pks else f"channel:{buf.thread_id}"

    async def _learn(self, buf: _LBuf, batch: list) -> int:
        codes: dict = {}
        for l in batch:
            codes.setdefault(l.person_key, f"U{len(codes) + 1}")
        text = "\n".join(f"{codes[l.person_key]}: {truncate(l.text, 200)}" for l in batch)
        style_task = self.k.llm.json("grow.learn_style", LEARN_STYLE_USER.format(lines=text), tier="fast",
                                     system=LEARN_STYLE_SYSTEM, schema=LEARN_STYLE_SCHEMA, thread_id=buf.thread_id)
        jargon_task = self.k.llm.json("grow.jargon", JARGON_USER.format(lines=text), tier="fast",
                                      system=JARGON_SYSTEM, schema=JARGON_SCHEMA, thread_id=buf.thread_id)
        res = await asyncio.gather(style_task, jargon_task, return_exceptions=True)
        style = res[0] if isinstance(res[0], dict) else {}
        jargon = res[1] if isinstance(res[1], dict) else {}
        names = set(n for n in buf.names.values() if n) | set(self.g._names.values())
        try:
            names.add(self.k.selfm.name())
        except Exception:
            pass
        scope = self._scope(buf, batch)
        made = 0
        user_texts = [l.text for l in batch]
        for r in (style.get("rules") or [])[:10]:
            if not isinstance(r, dict):
                continue
            sit = str(r.get("situation") or "").strip()
            sty = str(r.get("style") or "").strip()
            ex = str(r.get("example") or "").strip()
            src = self._source_line(ex, batch)
            if src is None or self._leaks_mine(ex, buf) or not self._valid_rule(sit, sty, names):
                continue
            a, created = await self.g.artifacts.create(
                "expression", sty, kind="style", situation=sit, scope=scope, examples=[truncate(src.text, 120)],
                speakers=[src.person_key], source="learner", author="observed", status="candidate")
            made += int(created)
        for t in (jargon.get("terms") or [])[:30]:
            if not isinstance(t, dict):
                continue
            term = str(t.get("term") or "").strip()
            meaning = str(t.get("meaning") or "").strip()
            ex = str(t.get("example") or "").strip()
            if not self._valid_term(term, meaning, names, user_texts):
                continue
            src = self._source_line(ex, batch) or next((l for l in batch if norm(term) in norm(l.text)), None)
            if src is None or self._leaks_mine(term, buf, exact_only=True):
                continue
            try:
                conf = float(t.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            a, created = await self.g.artifacts.create(
                "expression", meaning, kind="jargon", situation=f"有人说「{term}」", scope=scope,
                examples=[truncate(src.text, 120)], speakers=[src.person_key],
                jargon={"term": term, "meaning": meaning, "first_seen": self.k.clock.now(), "confidence": conf},
                source="learner", author="observed", status="candidate")
            made += int(created)
        return made

    # ------------------------------------------------------------------ validation
    @staticmethod
    def _source_line(example: str, batch: list) -> Optional[ChatLine]:
        if not example:
            return None
        ne = norm(example)
        best, best_c = None, 0.0
        for l in batch:
            nl = norm(l.text)
            if ne and ne in nl:
                return l
            c = containment(example, l.text)
            if c > best_c:
                best, best_c = l, c
        return best if best_c >= 0.6 else None

    @staticmethod
    def _leaks_mine(text: str, buf: _LBuf, exact_only: bool = False) -> bool:
        nt = norm(text)
        for m in buf.mine:
            nm = norm(m)
            if nt and nt in nm and (exact_only or len(nt) >= 4):
                # appears in my own words; only a leak if it does not also come from users
                return not any(nt in norm(l.text) for l in buf.lines)
        return False

    def _valid_rule(self, sit: str, sty: str, names: set) -> bool:
        if not sit or not sty or len(sit) > 24 or len(sty) > 24:
            return False
        joined = sit + " " + sty
        if "@" in joined or _URL.search(joined):
            return False
        if any(n and len(n) >= 2 and n in joined for n in names):
            return False
        nj = norm(joined)
        return not any(norm(w) in nj for w in self._bad_words() if w)

    def _valid_term(self, term: str, meaning: str, names: set, user_texts: list) -> bool:
        if not term or not meaning or len(term) > 16 or len(meaning) > 40:
            return False
        if "@" in term or _URL.search(term) or term in names:
            return False
        if any(n and len(n) >= 2 and (n in term or term in n) for n in names):
            return False
        nt = norm(term)
        if not any(nt in norm(t) for t in user_texts):
            return False
        return not any(norm(w) in nt or norm(w) in norm(meaning) for w in self._bad_words() if w)
