"""Online write path from conversations (docs/03 §4.1–4.2).

observe_conversation() only appends to a per-thread buffer and returns. A per-thread watcher flushes
the buffer after ``debounce.idle_s`` virtual seconds without new lines, or immediately once
``debounce.max_lines`` lines are pending. A flush makes one fast-tier JSON call that yields episodic
segment summaries and candidate facts; each candidate fact is reconciled Mem0-style against its top-10
similar existing records (one batched fast-tier call per segment): ADD / UPDATE (new version with parent
pointer) / INVALIDATE (bi-temporal + dependency propagation) / NOOP.

Rules enforced here:
- sensitive categories (health, politics, religion, sexuality) are skipped unless the person asked me
  to remember;
- claims by others about me become ``self`` records with status ``pending`` (never rewrite self memory);
- my own statements become episodic notes, never facts (they are self-generated content);
- a lower-trust source never overwrites a higher-trust fact: both are kept and marked as conflicting.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
from collections import deque
from dataclasses import asdict
from typing import Any, Optional, Sequence

from alive.core.types import ChatLine, IncomingMessage, Trust
from alive.memory.prompts import (EXTRACT_SCHEMA, EXTRACT_SYSTEM, EXTRACT_USER, RECONCILE_SCHEMA, RECONCILE_SYSTEM,
                                  RECONCILE_USER)
from alive.memory.records import LIVE_STATUSES, provenance_tag
from alive.memory.textutil import contains_any, norm, truncate

log = logging.getLogger("alive.memory")

_REMEMBER_WORDS = ("记住", "记得", "别忘", "帮我记", "remember")
_FACT_KINDS = ["semantic", "relational", "opinion"]


def _int(v: Any, default: int, lo: int = 1, hi: int = 10) -> int:
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


class _Buf:
    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.lines: list = []
        self._seen: deque = deque(maxlen=600)
        self._seen_set: set = set()
        self.participants: set = set()
        self.last_obs = 0.0
        self.context: list = []
        self.summary = ""
        self.watcher = None
        self.flush_pending = False
        self.lock = asyncio.Lock()
        self.failures = 0

    def seen(self, key: str) -> bool:
        if key in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        return False


class ExtractionMixin:
    # attributes provided by Memory: k, cfg, _names, _spawn, note, get, index, ...

    def _init_extraction(self) -> None:
        self._buffers: dict = {}
        self._xsem: Optional[asyncio.Semaphore] = None

    def _sem(self) -> asyncio.Semaphore:
        if self._xsem is None:
            self._xsem = asyncio.Semaphore(int(self.cfg["extract_concurrency"]))
        return self._xsem

    # ------------------------------------------------------------------ observe / debounce
    async def observe_conversation(self, thread_id: str, lines: Sequence[ChatLine], participants: Sequence[str]) -> None:
        if not lines or not thread_id:
            return
        d = self.cfg["debounce"]
        buf = self._buffers.get(thread_id)
        if buf is None:
            buf = self._buffers[thread_id] = _Buf(thread_id)
        added = 0
        for ln in lines:
            key = ln.msg_id or f"{ln.ts}|{ln.person_key}|{hash(ln.text)}"
            if buf.seen(key):
                continue
            if buf.lines and ln.ts < buf.lines[-1].ts and not buf.lock.locked():
                # lines reach memory from more than one place (what they said, and what I said, recorded as I
                # send it): keep the buffer in the order things were said. Not while a batch is being extracted
                # (it removes the batch by position).
                bisect.insort(buf.lines, ln, key=lambda l: l.ts)
            else:
                buf.lines.append(ln)
            added += 1
            if ln.person_key and ln.sender and not ln.mine:
                self._names[ln.person_key] = ln.sender
                buf.participants.add(ln.person_key)
        for p in participants or ():
            if p:
                buf.participants.add(p)
        if not added:
            return
        buf.last_obs = self.k.clock.now()
        if len(buf.lines) >= int(d["max_lines"]):
            if not buf.flush_pending:
                buf.flush_pending = True
                self._spawn(lambda b=buf: self._flush(b), f"mem.flush:{thread_id}")
        else:
            self._ensure_watcher(buf)

    def _ensure_watcher(self, buf: _Buf) -> None:
        if buf.watcher is None or buf.watcher.done:
            buf.watcher = self.k.scheduler.spawn(lambda b=buf: self._watch(b), f"mem.debounce:{buf.thread_id}")

    async def _watch(self, buf: _Buf) -> None:
        idle = float(self.cfg["debounce"]["idle_s"])
        try:
            while buf.lines:
                wait = buf.last_obs + idle - self.k.clock.now()
                if wait > 0:
                    await self.k.clock.sleep(wait)
                    continue
                await self._flush(buf)
                if buf.lines and buf.failures:
                    buf.last_obs = self.k.clock.now()  # back off one idle period before retrying
        finally:
            buf.watcher = None

    async def _flush(self, buf: _Buf, force: bool = False) -> int:
        d = self.cfg["debounce"]
        done = 0
        async with buf.lock:
            buf.flush_pending = False
            while buf.lines:
                batch = list(buf.lines[: int(d["max_batch"])])
                self._active += 1
                try:
                    async with self._sem():
                        ok = await self._extract_segment(buf, batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("memory extraction failed for %s", buf.thread_id)
                    ok = False
                finally:
                    self._active -= 1
                if ok:
                    del buf.lines[: len(batch)]
                    buf.context = (buf.context + batch)[-int(d["context_lines"]):]
                    buf.failures = 0
                    done += len(batch)
                else:
                    buf.failures += 1
                    if buf.failures >= int(d["max_failures"]):
                        log.warning("dropping %d lines of %s after %d failed extractions", len(batch),
                                    buf.thread_id, buf.failures)
                        del buf.lines[: len(batch)]
                        buf.failures = 0
                    else:
                        break
                if not force and len(buf.lines) < int(d["max_lines"]):
                    break
        if buf.lines and self._started:
            self._ensure_watcher(buf)
        return done

    async def flush_all(self) -> dict:
        """Flush every pending conversation buffer now (sleep 'extract' stage, shutdown, tests)."""
        n_threads, n_lines = 0, 0
        for buf in list(self._buffers.values()):
            if buf.lines:
                n_threads += 1
                n_lines += await self._flush(buf, force=True)
        return {"threads": n_threads, "lines": n_lines}

    def _stop_watchers(self) -> None:
        for buf in self._buffers.values():
            if buf.watcher is not None and not buf.watcher.done:
                buf.watcher.cancel()
            buf.watcher = None

    async def _forget_in_buffers(self, pk: str) -> None:
        """Drop a person's buffered lines. Takes each affected buffer's lock, so an extraction batch already
        in flight with their lines finishes *before* the caller deletes records (no resurrection)."""
        for buf in list(self._buffers.values()):
            involved = pk in buf.participants or any(l.person_key == pk for l in buf.lines + buf.context)
            if not involved:
                continue
            async with buf.lock:
                buf.lines = [l for l in buf.lines if l.person_key != pk]
                buf.context = [l for l in buf.context if l.person_key != pk]
                buf.participants.discard(pk)
                buf.summary = ""

    async def _save_buffers(self) -> None:
        data = {}
        for tid, b in self._buffers.items():
            if b.lines or b.summary:
                data[tid] = {"lines": [asdict(l) for l in b.lines], "context": [asdict(l) for l in b.context],
                             "participants": sorted(b.participants), "summary": b.summary}
        await self.k.db.kv_set("mem", "buffers", data, ts=self.k.clock.now())

    async def _load_buffers(self) -> None:
        data = await self.k.db.kv_get("mem", "buffers", {}) or {}
        now = self.k.clock.now()
        for tid, d in data.items():
            b = _Buf(tid)
            try:
                b.lines = [ChatLine(**l) for l in d.get("lines", [])]
                b.context = [ChatLine(**l) for l in d.get("context", [])]
            except TypeError:
                continue
            for l in b.lines:
                b.seen(l.msg_id or f"{l.ts}|{l.person_key}|{hash(l.text)}")
            b.participants = set(d.get("participants") or [])
            b.summary = d.get("summary") or ""
            b.last_obs = now
            self._buffers[tid] = b
            if b.lines:
                self._ensure_watcher(b)

    # ------------------------------------------------------------------ extraction
    def _me_name(self) -> str:
        try:
            return self.k.selfm.name() if self.k.selfm is not None else "我"
        except Exception:
            return "我"

    def _affect_now(self) -> dict:
        aff = getattr(self.k, "affect", None)
        if aff is None:
            return {}
        try:
            s = aff.snapshot()
        except Exception:
            return {}
        emos = [e for e in (s.emotions or []) if isinstance(e, dict)]
        top = max(emos, key=lambda e: float(e.get("intensity", 0) or 0)) if emos else None
        inten = float(top.get("intensity", 0) or 0) if top else 0.0
        arousal = max(0.0, min(1.0, max(float(s.A), inten)))
        return {"valence": max(-1.0, min(1.0, float(s.P))), "arousal": arousal,
                "label": (top or {}).get("type"), "peak": inten >= 0.6 or arousal >= 0.75}

    def _trust_for(self, pk: str, buf: _Buf, line: Optional[ChatLine]) -> int:
        """How far I trust a statement = how far I trust the person who made it (docs/09 §5). Trust is a property
        of the person, not of the space the statement was made in."""
        gov = getattr(self.k, "gov", None)
        if gov is None:
            return int(Trust.PUBLIC)
        fn = getattr(gov, "trust_of_person", None)
        if fn is not None:
            try:
                return int(fn(pk))
            except Exception:
                return int(Trust.PUBLIC)
        channel, _, sender = pk.partition(":")
        msg = IncomingMessage(channel=channel, chat_id=sender or pk, chat_type="private", sender_id=sender or pk,
                              sender_name=self._names.get(pk, sender), text=line.text if line else "",
                              msg_id=line.msg_id if line else "", ts=line.ts if line else self.k.clock.now(),
                              sender_entity=pk, space_entity=pk)
        try:
            return int(gov.trust_of(msg))
        except Exception:
            return int(Trust.PUBLIC)

    def _is_group_space(self, buf: _Buf) -> bool:
        """docs/22: a one-to-one space's id is the counterpart's person entity id, a group is a group entity.
        Decided from the world registry, not from how many people spoke in this segment (a group segment with a
        single speaker is still a group, not a private chat with a stranger)."""
        world = getattr(self.k, "world", None)
        tid = buf.thread_id
        if world is not None and world.entity(tid) is not None:
            return world.kind(tid) == "group"
        return len(buf.participants) > 1      # space unknown to the registry (legacy route ids): best effort

    async def _rekey_buffers(self, old: str, new: str) -> None:
        """Entity merge: move buffered lines / participants from ``old`` to ``new``; the old one-to-one space's
        buffer (its id is the old person id) joins the surviving space's. Takes each affected buffer's lock, so
        an extraction already in flight finishes first (its rows are caught by the rekey's second SQL pass)."""
        for tid, buf in list(self._buffers.items()):
            if not (tid == old or old in buf.participants
                    or any(l.person_key == old for l in buf.lines + buf.context)):
                continue
            async with buf.lock:
                for l in buf.lines + buf.context:
                    if l.person_key == old:
                        l.person_key = new
                if old in buf.participants:
                    buf.participants.discard(old)
                    buf.participants.add(new)
        src = self._buffers.get(old)
        if src is None:
            return
        dst = self._buffers.get(new)
        if dst is None or dst is src:
            self._buffers.pop(old, None)
            src.thread_id = new
            self._buffers[new] = src
            return
        async with src.lock:
            async with dst.lock:
                keep = int(self.cfg["debounce"]["context_lines"])
                dst.lines = sorted(dst.lines + src.lines, key=lambda l: l.ts)
                dst.context = sorted(dst.context + src.context, key=lambda l: l.ts)[-keep:]
                dst.participants |= src.participants
                for key in list(src._seen):
                    dst.seen(key)
                dst.summary = dst.summary or src.summary
                dst.last_obs = max(dst.last_obs, src.last_obs)
                src.lines, src.context = [], []
        self._buffers.pop(old, None)
        if dst.lines and self._started:
            self._ensure_watcher(dst)

    async def _extract_segment(self, buf: _Buf, batch: list) -> bool:
        tz = self.k.clock.tz
        codes: dict = {}
        for ln in buf.context + batch:
            if not ln.mine and ln.person_key and ln.person_key not in codes:
                codes[ln.person_key] = f"p{len(codes) + 1}"
        rev = {c: pk for pk, c in codes.items()}
        me = self._me_name()

        def code(ln: ChatLine) -> str:
            return "me" if (ln.mine or not ln.person_key) else codes[ln.person_key]

        def name(ln: ChatLine) -> str:
            return me if (ln.mine or not ln.person_key) else (ln.sender or self._names.get(ln.person_key) or
                                                              ln.person_key.split(":")[-1])

        def hm(ts: float) -> str:
            from datetime import datetime
            return datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")

        legend = "\n".join(f"{c} = {self._names.get(pk) or pk.split(':')[-1]}" for pk, c in codes.items())
        legend = (legend + "\n" if legend else "") + f"me = 我（{me}）"
        ctx = "".join(f"（之前，仅供参考）[{hm(l.ts)}] {code(l)} {name(l)}: {truncate(l.text, 200)}\n"
                      for l in buf.context)
        body = "\n".join(f"{i + 1}. [{hm(l.ts)}] {code(l)} {name(l)}: {truncate(l.text, 300)}"
                         for i, l in enumerate(batch))
        prompt = EXTRACT_USER.format(me=me, legend=legend, prev=buf.summary or "（无）", lines=ctx + body)
        try:
            data = await self.k.llm.json("mem.extract", prompt, tier="fast", system=EXTRACT_SYSTEM,
                                         schema=EXTRACT_SCHEMA, thread_id=buf.thread_id)
        except Exception as e:
            log.warning("mem.extract call failed: %s", e)
            return False
        episodes = data.get("episodes") if isinstance(data.get("episodes"), list) else []
        facts = data.get("facts") if isinstance(data.get("facts"), list) else []
        aff = self._affect_now()
        ep_ids: list = []
        for ep in episodes[:3]:
            if not isinstance(ep, dict):
                continue
            summ = str(ep.get("summary") or "").strip()
            if not summ:
                continue
            seg = batch
            lr = ep.get("lines")
            if isinstance(lr, list) and len(lr) == 2:
                a, b = _int(lr[0], 1, 1, len(batch)), _int(lr[1], len(batch), 1, len(batch))
                if a <= b:
                    seg = batch[a - 1:b]
            pks = list(dict.fromkeys(l.person_key for l in seg if l.person_key and not l.mine))
            ents = [str(e) for e in (ep.get("entities") or []) if isinstance(e, (str, int))][:8]
            ea = dict(aff)
            if ep.get("peak") is True:
                ea["peak"] = True
            try:
                eid = await self.note(kind="episodic", content=truncate(summ, 400),
                                      importance=_int(ep.get("importance"), 3), affect=ea or None,
                                      source="experienced", trust=Trust.SYSTEM,
                                      evidence=[l.msg_id for l in seg if l.msg_id][:40], entities=ents,
                                      person_keys=pks, thread_id=buf.thread_id, observed_at=seg[-1].ts,
                                      meta={"segment": True, "lines": len(seg)})
                ep_ids.append(eid)
                buf.summary = truncate(summ, 200)
            except ValueError:
                continue
        report = await self._process_facts(buf, batch, facts, rev, ep_ids)
        self.counters["extractions"] += 1
        await self.k.bus.emit("mem.extract", {"thread_id": buf.thread_id, "lines": len(batch), "episodes": len(ep_ids),
                                              **report}, source="memory", thread_id=buf.thread_id)
        return True

    async def _process_facts(self, buf: _Buf, batch: list, facts: list, rev: dict, ep_ids: list) -> dict:
        report = {"add": 0, "update": 0, "invalidate": 0, "noop": 0, "conflict": 0, "claims": 0,
                  "sensitive_skipped": 0, "mine": 0}
        cands: list = []
        for f in facts[: int(self.cfg["max_facts_per_segment"])]:
            if not isinstance(f, dict):
                continue
            content = str(f.get("content") or "").strip()
            if len(content) < 4:
                continue
            li = _int(f.get("line"), 0, 0, len(batch))
            line = batch[li - 1] if 1 <= li <= len(batch) else None
            tb = str(f.get("told_by") or "").strip().lower()
            if tb == "me":
                teller = "me"
            elif tb in rev:
                teller = rev[tb]
            elif line is not None:
                teller = "me" if (line.mine or not line.person_key) else line.person_key
            else:
                continue
            ab = str(f.get("about") or "").strip().lower()
            about = "me" if ab == "me" else rev.get(ab)
            kind = str(f.get("kind") or "semantic").strip().lower()
            sensitive = bool(f.get("sensitive") is True) or bool(contains_any(content, self.cfg["sensitive_words"]))
            explicit = f.get("explicit_remember") is True or bool(
                line is not None and any(w in line.text for w in _REMEMBER_WORDS))
            if sensitive and not explicit:
                report["sensitive_skipped"] += 1
                self.counters["facts_skipped_sensitive"] += 1
                continue
            ts = line.ts if line is not None else batch[-1].ts
            when = str(f.get("when") or "").strip()
            ents = [str(e) for e in (f.get("entities") or []) if isinstance(e, (str, int))][:8]
            imp = _int(f.get("importance"), 4)
            evidence = ([line.msg_id] if line is not None and line.msg_id else []) + ep_ids
            common = dict(importance=imp, entities=ents, refers_to={"raw": when} if when else None,
                          thread_id=buf.thread_id, observed_at=ts, evidence=evidence)
            if teller == "me":
                # self-generated content: remembered as something I said, never as a fact
                await self.note(kind="episodic", content=f"我说过：{content}", source="experienced",
                                trust=Trust.SYSTEM, meta={"my_statement": True},
                                **{**common, "importance": min(imp, 5)})
                report["mine"] += 1
                continue
            trust = self._trust_for(teller, buf, line)
            if kind == "self_claim" or about == "me":
                await self.note(kind="self", content=content, source="told_by", told_by=teller, trust=trust,
                                person_keys=[teller], meta={"claim_about_me": True}, **common)
                report["claims"] += 1
                continue
            pks = [about] if about else []
            cands.append({"content": content, "kind": "relational" if kind == "relational" else "semantic",
                          "told_by": teller, "trust": trust, "person_keys": pks, "ts": ts, **common})
        if cands:
            await self._reconcile(cands, report)
        return report

    # ------------------------------------------------------------------ reconciliation
    def _lexical_sim(self, a: str, b: str, names: set) -> float:
        """Overlap of CJK/latin bigrams after removing person names (names would make every fact about the
        same person look similar). |A∩B| / min(|A|,|B|)."""
        def grams(t: str) -> set:
            t = norm(t)
            for n in names:
                if n:
                    t = t.replace(n, " ")
            t = "".join(ch for ch in t if ch.isalnum() or ch == " ")
            out = set()
            for w in t.split():
                out |= {w[i:i + 2] for i in range(len(w) - 1)} if len(w) > 1 else set()
            return out
        A, B = grams(a), grams(b)
        if not A or not B:
            return 0.0
        return len(A & B) / min(len(A), len(B))

    async def _similar(self, c: dict, vec) -> list:
        """Top-k existing fact records to reconcile a candidate against: vector + trigram text + same-person
        routes, ranked by max(cosine, name-stripped lexical overlap) with a same-person bonus."""
        k = int(self.cfg["reconcile_top_k"])
        min_sim = float(self.cfg["reconcile_min_sim"])
        sims = await self.index.search_async(vec, k * 4)
        flt = self._filters(LIVE_STATUSES, _FACT_KINDS, None, None)
        text_rows = await self._route_text(c["content"], flt, k)
        ids = [i for i, s in sims if s >= min_sim * 0.6] + [r["id"] for r in text_rows]
        pks = [p for p in c.get("person_keys") or [] if p]
        if pks:
            for p in pks:
                ids += [r["record_id"] for r in await self.k.db.fetchall(
                    "SELECT record_id FROM mem_ent WHERE key=? AND role='person' ORDER BY record_id DESC LIMIT 40", (p,))]
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        where, params = flt
        rows = await self.k.db.fetchall(
            f"SELECT r.* FROM mem_records r WHERE r.id IN ({','.join('?' * len(ids))}) AND {where}", ids + params)
        cos = self.index.cosine_many(vec, [r["id"] for r in rows])
        names = {n for n in self._names.values() if n and len(n) >= 2}
        scored = []
        for r in rows:
            s = max(cos.get(r["id"], 0.0), 0.9 * self._lexical_sim(c["content"], r["content"], names))
            if pks and any(f'"{p}"' in (r["person_keys"] or "") for p in pks):
                s += 0.1
            if s >= min_sim:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:k]]

    async def _reconcile(self, cands: list, report: dict) -> None:
        from alive.memory.records import row_to_hit
        vecs = await self.k.embedder.embed([c["content"] for c in cands])
        now = self.k.clock.now()
        tz = self.k.clock.tz
        code_of: dict = {}
        blocks = []
        for n, (c, v) in enumerate(zip(cands, vecs), start=1):
            c["similar"] = await self._similar(c, v)
            c["n"] = n
            if not c["similar"]:
                continue
            teller = self._names.get(c["told_by"]) or c["told_by"].split(":")[-1]
            lines = [f"新事实 {n}：「{c['content']}」（{teller} 说的，可信度 {c['trust']}）", "  候选旧记忆："]
            for r in c["similar"]:
                if r["id"] not in code_of:
                    code_of[r["id"]] = f"r{len(code_of) + 1}"
                h = row_to_hit(r)
                lines.append(f"  {code_of[r['id']]}. {h.content}{provenance_tag(h, now, tz, self._names)}"
                             f"（可信度 {h.trust}）")
            blocks.append("\n".join(lines))
        decisions: dict = {}
        if blocks:
            from datetime import datetime
            prompt = RECONCILE_USER.format(now=datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M"),
                                           items="\n\n".join(blocks))
            try:
                data = await self.k.llm.json("mem.reconcile", prompt, tier="fast", system=RECONCILE_SYSTEM,
                                             schema=RECONCILE_SCHEMA)
            except Exception as e:
                log.warning("mem.reconcile failed, defaulting to ADD: %s", e)
                data = {"decisions": []}
            for d in data.get("decisions") or []:
                if isinstance(d, dict):
                    fi = _int(d.get("fact"), 0, 0, 10_000)
                    if fi and fi not in decisions:
                        decisions[fi] = d
        by_code = {v: k for k, v in code_of.items()}
        for c in cands:
            d = decisions.get(c["n"]) or {}
            action = str(d.get("action") or "ADD").strip().upper()
            tcode = str(d.get("target") or "").strip().lower()
            target = by_code.get(tcode)
            if target is not None and target not in {r["id"] for r in c["similar"]}:
                target = None
            if action not in ("ADD", "UPDATE", "INVALIDATE", "NOOP") or (action != "ADD" and target is None):
                action = "ADD"
            try:
                await self._apply(c, action, target, str(d.get("content") or "").strip(),
                                  str(d.get("reason") or "")[:120], report)
            except Exception:
                log.exception("applying reconciliation decision failed")

    async def _note_fact(self, c: dict, **over) -> str:
        args = dict(kind=c["kind"], content=c["content"], importance=c["importance"], source="told_by",
                    told_by=c["told_by"], trust=c["trust"], evidence=c["evidence"], entities=c["entities"],
                    person_keys=c["person_keys"], refers_to=c["refers_to"], thread_id=c["thread_id"],
                    valid_from=c["ts"], observed_at=c["ts"])
        args.update(over)
        return await self.note(**args)

    async def _apply(self, c: dict, action: str, target: Optional[str], content: str, reason: str,
                     report: dict) -> Optional[str]:
        if action == "NOOP":
            self._touch([target])
            report["noop"] += 1
            return target
        if action == "ADD":
            report["add"] += 1
            return await self._note_fact(c)
        tgt = await self.get(target)
        if tgt is None or tgt.status not in LIVE_STATUSES:
            report["add"] += 1
            return await self._note_fact(c)
        protected = tgt.status == "pinned" or tgt.kind == "self"
        lower = int(c["trust"]) < int(tgt.trust)
        if action == "UPDATE":
            if protected or lower:
                report["add"] += 1
                return await self._note_fact(c)
            nid = await self._note_fact(
                c, content=content or c["content"], kind=tgt.kind, importance=max(int(c["importance"]), tgt.importance),
                person_keys=list(dict.fromkeys(list(tgt.person_keys) + list(c["person_keys"]))),
                entities=list(dict.fromkeys(list(tgt.entities) + list(c["entities"]))),
                derived_from=tgt.derived_from, version=tgt.version + 1, parent_version=tgt.id)
            await self.supersede(tgt.id, nid)
            report["update"] += 1
            return nid
        # INVALIDATE
        nid = await self._note_fact(c)
        if protected or lower:
            await self.mark_conflict(nid, tgt.id, reason or "lower-trust contradiction")
            report["conflict"] += 1
        else:
            await self.invalidate(tgt.id, reason=reason or "contradicted", replaced_by=nid, valid_to=c["ts"])
            report["invalidate"] += 1
        return nid
