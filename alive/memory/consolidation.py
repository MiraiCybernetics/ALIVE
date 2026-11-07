"""Sleep stages owned by memory (docs/03 §6–7, docs/06 §5.1): extract, reconcile, forget, precompute.

- extract:    flush every pending conversation buffer (online extraction may still be debouncing).
- reconcile:  re-evaluate ``stale`` records (dependency propagation) and conflicting pairs, batched,
              deliberate tier.
- forget:     compress weak (retrievability below threshold) low-importance (<= 4) episodic records into
              per-thread-per-day summaries; originals become ``compressed`` (never deleted; the event log
              keeps the raw conversation). Rewriting an existing summary obeys the 25% max-loss rule and
              keeps a pre-image.
- precompute: sleep-time compute — for predictable topics of the coming day (records referring to it,
              open loops, due intentions) pre-retrieve memory bundles and store them in kv.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Optional

from alive.core.types import Trust
from alive.memory.prompts import (COMPRESS_SCHEMA, COMPRESS_SYSTEM, COMPRESS_USER, CONFLICT_SCHEMA, CONFLICT_SYSTEM,
                                  CONFLICT_USER, RECHECK_SCHEMA, RECHECK_SYSTEM, RECHECK_USER)
from alive.memory.records import LIVE_STATUSES, provenance_tag, row_to_hit
from alive.memory.textutil import item_loss, truncate

log = logging.getLogger("alive.memory")


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), max(1, n)):
        yield xs[i:i + n]


class ConsolidationMixin:
    async def consolidate(self, stage: str, ctx: dict) -> dict:
        ctx = dict(ctx or {})
        ctx.setdefault("now", self.k.clock.now())
        fn = {"extract": self._stage_extract, "reconcile": self._stage_reconcile, "forget": self._stage_forget,
              "precompute": self._stage_precompute}.get(stage)
        if fn is None:
            return {"stage": stage, "error": "unknown stage"}
        self._active += 1
        try:
            rep = await fn(ctx)
        finally:
            self._active -= 1
        await self.flush_access()
        rep["stage"] = stage
        return rep

    # ------------------------------------------------------------------ extract
    async def _stage_extract(self, ctx: dict) -> dict:
        rep = await self.flush_all()
        await self.drain()
        return {"flushed_threads": rep["threads"], "lines": rep["lines"]}

    # ------------------------------------------------------------------ reconcile
    async def _stage_reconcile(self, ctx: dict) -> dict:
        rep = {"kept": 0, "revised": 0, "invalidated": 0, "conflicts_resolved": 0, "conflicts_kept": 0,
               "dropped": 0}
        rows = await self.k.db.fetchall(
            "SELECT record_id, reason, depth, attempts FROM mem_recheck ORDER BY queued_at LIMIT ?",
            (int(self.cfg["recheck_max_per_night"]),))
        stale = [r for r in rows if str(r["reason"]).startswith("dependency")]
        conflicts = [r for r in rows if str(r["reason"]).startswith("conflict")]
        for batch in _chunks(stale, int(self.cfg["recheck_batch"])):
            await self._recheck_batch(batch, rep)
        pairs, seen = [], set()
        for r in conflicts:
            other = str(r["reason"]).split(":", 1)[1] if ":" in r["reason"] else ""
            key = tuple(sorted((r["record_id"], other)))
            if other and key not in seen:
                seen.add(key)
                pairs.append((r["record_id"], other, int(r["attempts"])))
        for batch in _chunks(pairs, int(self.cfg["recheck_batch"])):
            await self._conflict_batch(batch, rep)
        return rep

    async def _dequeue(self, ids: list, *, bump: bool = False) -> None:
        if not ids:
            return
        q = ",".join("?" * len(ids))
        if bump:
            await self.k.db.transaction(lambda c: (
                c.execute(f"UPDATE mem_recheck SET attempts = attempts + 1 WHERE record_id IN ({q})", ids),
                c.execute("DELETE FROM mem_recheck WHERE attempts >= 3")))
        else:
            await self.k.db.execute(f"DELETE FROM mem_recheck WHERE record_id IN ({q})", ids)

    async def _recheck_batch(self, batch: list, rep: dict) -> None:
        now = self.k.clock.now()
        tz = self.k.clock.tz
        items, recs, gone = [], {}, []
        for q in batch:
            rec = await self.get(q["record_id"])
            if rec is None or rec.status not in ("stale", "pinned", "active", "pending"):
                gone.append(q["record_id"])
                continue
            deps = await self.get_many(rec.derived_from)
            dl = []
            for d in deps:
                if d.status == "invalid":
                    rb = ((d.meta or {}).get("invalidated") or {}).get("replaced_by")
                    newer = await self.get(rb) if rb else None
                    dl.append(f"   - 「{d.content}」已不成立" + (f"，新情况是「{newer.content}」" if newer else ""))
                elif d.status == "superseded":
                    dl.append(f"   - 「{d.content}」已被更新")
                else:
                    dl.append(f"   - 「{d.content}」仍成立")
            n = len(recs) + 1
            recs[n] = rec
            items.append(f"{n}. 记忆：「{rec.content}」{provenance_tag(rec, now, tz, self._names)}\n   它的依据：\n"
                         + ("\n".join(dl) if dl else "   - （依据已删除）"))
        await self._dequeue(gone)
        rep["dropped"] += len(gone)
        if not items:
            return
        try:
            data = await self.k.llm.json("mem.recheck", RECHECK_USER.format(items="\n\n".join(items)),
                                         tier="deliberate", system=RECHECK_SYSTEM, schema=RECHECK_SCHEMA)
        except Exception as e:
            log.warning("mem.recheck failed: %s", e)
            await self._dequeue([r.id for r in recs.values()], bump=True)
            return
        verdicts: dict = {}
        for res in data.get("results") or []:
            if not isinstance(res, dict):
                continue
            try:
                n = int(res.get("item"))
            except (TypeError, ValueError):
                continue
            v = str(res.get("verdict") or "").strip().lower()
            if n in recs and n not in verdicts and (v in ("keep", "invalid") or
                                                    (v == "revise" and str(res.get("content") or "").strip())):
                verdicts[n] = (v, res)
        # invalid / revise first: an invalidation inside this batch may make a sibling stale again,
        # and such a record must neither be re-activated by a "keep" nor be dequeued.
        restaled: set = set()
        order = sorted(verdicts, key=lambda n: {"invalid": 0, "revise": 1, "keep": 2}[verdicts[n][0]])
        for n in order:
            v, res = verdicts[n]
            rec = recs[n]
            if v == "invalid":
                r = await self.invalidate(rec.id, reason="sleep recheck: " + str(res.get("reason") or "")[:100])
                restaled.update(r["stale"])
                rep["invalidated"] += 1
            elif v == "revise":
                deps = []
                for d in await self.get_many(rec.derived_from):
                    rb = ((d.meta or {}).get("invalidated") or {}).get("replaced_by") if d.status == "invalid" else None
                    deps.append(rb or d.id)
                nid = await self.note(kind=rec.kind, content=str(res["content"]).strip(), importance=rec.importance,
                                      affect=rec.affect, source=rec.source, told_by=rec.told_by, trust=rec.trust,
                                      entities=rec.entities, person_keys=rec.person_keys, derived_from=deps,
                                      thread_id=rec.thread_id, version=rec.version + 1, parent_version=rec.id,
                                      meta={"revised_from": rec.id, "reason": str(res.get("reason") or "")[:120]})
                await self.supersede(rec.id, nid)
                rep["revised"] += 1
            elif rec.id not in restaled:
                await self.k.db.execute("UPDATE mem_records SET status='active' WHERE id=? AND status='stale'",
                                        (rec.id,))
                rep["kept"] += 1
        done = [recs[n].id for n in verdicts if recs[n].id not in restaled]
        await self._dequeue(done)
        await self._dequeue([r.id for n, r in recs.items() if n not in verdicts and r.id not in restaled], bump=True)

    async def _conflict_batch(self, pairs: list, rep: dict) -> None:
        now = self.k.clock.now()
        tz = self.k.clock.tz
        items, live = [], {}
        for a, b, _ in pairs:
            ra, rb = await self.get(a), await self.get(b)
            if ra is None or rb is None or ra.status not in LIVE_STATUSES or rb.status not in LIVE_STATUSES:
                await self._clear_conflict(a, b)
                continue
            n = len(live) + 1
            live[n] = (ra, rb)
            items.append(f"{n}. a：「{ra.content}」{provenance_tag(ra, now, tz, self._names)}（可信度 {ra.trust}）\n"
                         f"   b：「{rb.content}」{provenance_tag(rb, now, tz, self._names)}（可信度 {rb.trust}）")
        if not items:
            return
        try:
            data = await self.k.llm.json("mem.conflict", CONFLICT_USER.format(items="\n".join(items)),
                                         tier="deliberate", system=CONFLICT_SYSTEM, schema=CONFLICT_SCHEMA)
        except Exception as e:
            log.warning("mem.conflict failed: %s", e)
            data = {"results": []}
        done = set()
        for res in data.get("results") or []:
            if not isinstance(res, dict):
                continue
            try:
                n = int(res.get("pair"))
            except (TypeError, ValueError):
                continue
            if n not in live or n in done:
                continue
            ra, rb = live[n]
            w = str(res.get("winner") or "").strip().lower()
            reason = str(res.get("reason") or "")[:100]
            if w in ("a", "b"):
                win, lose = (ra, rb) if w == "a" else (rb, ra)
                await self._clear_conflict(ra.id, rb.id)
                await self.invalidate(lose.id, reason="conflict resolved: " + reason, replaced_by=win.id)
                rep["conflicts_resolved"] += 1
                done.add(n)
            elif w == "both":
                await self._clear_conflict(ra.id, rb.id)
                rep["conflicts_resolved"] += 1
                done.add(n)
        for n, (ra, rb) in live.items():
            if n not in done:
                rep["conflicts_kept"] += 1
                await self._dequeue([ra.id, rb.id], bump=True)

    async def _clear_conflict(self, a: str, b: str) -> None:
        def _tx(c):
            for x, y in ((a, b), (b, a)):
                r = c.execute("SELECT conflict_with FROM mem_records WHERE id=?", (x,)).fetchone()
                if r is not None:
                    cw = [i for i in json.loads(r["conflict_with"] or "[]") if i != y]
                    c.execute("UPDATE mem_records SET conflict_with=? WHERE id=?", (json.dumps(cw), x))
                c.execute("DELETE FROM mem_recheck WHERE record_id=? AND reason=?", (x, f"conflict:{y}"))
        await self.k.db.transaction(_tx)

    # ------------------------------------------------------------------ forget / compress
    async def _stage_forget(self, ctx: dict) -> dict:
        f = self.cfg["forget"]
        now = float(ctx.get("now") or self.k.clock.now())
        thr = max(1e-6, min(0.99, float(f["r_threshold"])))
        ln = math.log(1.0 / thr)   # R < thr  <=>  (now - last_access) > strength * ln(1/thr)
        rows = await self.k.db.fetchall(
            "SELECT * FROM mem_records WHERE kind='episodic' AND status='active' AND importance <= ? "
            "AND (? - last_access) > strength * ? AND meta NOT LIKE '%\"summary_of\"%' "
            "ORDER BY observed_at ASC LIMIT 3000", (int(f["max_importance"]), now, ln))
        tz = self.k.clock.tz
        groups: dict = {}
        for r in rows:
            day = datetime.fromtimestamp(float(r["observed_at"]), tz).strftime("%Y-%m-%d")
            groups.setdefault(f"{r['thread_id'] or '-'}|{day}", []).append(r)
        rep = {"candidates": len(rows), "groups": 0, "compressed": 0, "summaries": 0, "rewrites": 0,
               "rewrites_rejected": 0}
        todo = [(k, g) for k, g in groups.items() if len(g) >= int(f["min_group"])][: int(f["max_groups"])]
        for key, g in todo:
            existing = await self.k.db.fetchone(
                "SELECT * FROM mem_records WHERE kind='episodic' AND status IN ('active','pinned','stale') "
                "AND meta LIKE ? LIMIT 1", (f'%"summary_key": {json.dumps(key, ensure_ascii=False)}%',))
            listing = "\n".join(f"{i + 1}. {truncate(r['content'], 200)}" for i, r in enumerate(g))
            pre = f"已有的摘要：{existing['content']}\n\n" if existing is not None else ""
            try:
                data = await self.k.llm.json("mem.compress", COMPRESS_USER.format(existing=pre, items=listing),
                                             tier="fast", system=COMPRESS_SYSTEM, schema=COMPRESS_SCHEMA)
            except Exception as e:
                log.warning("mem.compress failed: %s", e)
                continue
            summary = str(data.get("summary") or "").strip()
            if not summary:
                continue
            ids = [r["id"] for r in g]
            rep["groups"] += 1
            sid: Optional[str] = None
            if existing is not None:
                ok = await self.rewrite_summary(existing["id"], summary, reason="forget-merge",
                                                max_loss=float(f["max_loss"]), add_sources=ids)
                if ok:
                    sid = existing["id"]
                    rep["rewrites"] += 1
                else:
                    rep["rewrites_rejected"] += 1
            if sid is None:
                pks, ents = [], []
                for r in g:
                    pks += json.loads(r["person_keys"] or "[]")
                    ents += json.loads(r["entities"] or "[]")
                peak = any(r["aff_peak"] for r in g)
                val = sum(float(r["aff_valence"]) for r in g) / len(g)
                aro = max(float(r["aff_arousal"]) for r in g)
                sid = await self.note(kind="episodic", content=truncate(summary, 400),
                                      importance=max(int(r["importance"]) for r in g),
                                      affect={"valence": val, "arousal": aro, "peak": peak}, source="experienced",
                                      trust=Trust.SYSTEM, entities=list(dict.fromkeys(ents))[:12],
                                      person_keys=list(dict.fromkeys(pks)), derived_from=ids,
                                      thread_id=g[0]["thread_id"], observed_at=float(g[0]["observed_at"]),
                                      meta={"summary_of": ids, "summary_key": key})
                rep["summaries"] += 1

            def _tx(c, ids=ids, sid=sid):
                for i in ids:
                    r = c.execute("SELECT meta FROM mem_records WHERE id=?", (i,)).fetchone()
                    m = json.loads((r["meta"] if r else None) or "{}")
                    m["compressed_into"] = sid
                    c.execute("UPDATE mem_records SET status='compressed', meta=? WHERE id=? AND status='active'",
                              (json.dumps(m, ensure_ascii=False), i))
                if sid:
                    c.executemany("INSERT OR IGNORE INTO mem_deps (depends_on, record_id) VALUES (?,?)",
                                  [(i, sid) for i in ids])
                return len(ids)

            rep["compressed"] += await self.k.db.transaction(_tx)
        return rep

    async def rewrite_summary(self, record_id: str, new_content: str, *, reason: str = "rewrite",
                              max_loss: float = 0.25, add_sources: tuple = ()) -> bool:
        """Rewrite a summary record in place, keeping a pre-image. Refused if the record is pinned or
        if more than ``max_loss`` of the old items would be lost (OpenClaw 25% rule)."""
        rec = await self.get(record_id)
        if rec is None or rec.status == "pinned" or not (new_content or "").strip():
            return False
        loss = item_loss(rec.content, new_content)
        if loss > max_loss:
            log.info("summary rewrite of %s rejected: loss %.0f%% > %.0f%%", record_id, loss * 100, max_loss * 100)
            return False
        now = self.k.clock.now()
        meta = dict(rec.meta or {})
        meta["rewrites"] = int(meta.get("rewrites", 0)) + 1
        if add_sources:
            meta["summary_of"] = list(dict.fromkeys(list(meta.get("summary_of") or []) + list(add_sources)))
        content = truncate(new_content.strip(), 600)
        vec = (await self.k.embedder.embed([content]))[0]
        from alive.runtime.embed import to_blob
        tag, dim = self._embedder_tag, self.k.embedder.dim

        def _tx(c):
            c.execute("INSERT INTO mem_preimages (record_id, content, meta, saved_at, reason) VALUES (?,?,?,?,?)",
                      (record_id, rec.content, json.dumps(rec.meta or {}, ensure_ascii=False), now, reason))
            c.execute("UPDATE mem_records SET content=?, meta=?, version=version+1 WHERE id=?",
                      (content, json.dumps(meta, ensure_ascii=False), record_id))
            c.execute("INSERT OR REPLACE INTO mem_vec (id, embedder, dim, vec) VALUES (?,?,?,?)",
                      (record_id, tag, dim, to_blob(vec)))

        await self.k.db.transaction(_tx)
        self.index.add(record_id, vec)
        return True

    async def preimages(self, record_id: str) -> list:
        rows = await self.k.db.fetchall("SELECT id, content, saved_at, reason FROM mem_preimages WHERE record_id=? "
                                        "ORDER BY id DESC", (record_id,))
        return [dict(r) for r in rows]

    async def restore_preimage(self, record_id: str, preimage_id: Optional[int] = None) -> bool:
        """Roll a rewritten summary back to a saved pre-image (the current content is itself saved first)."""
        if preimage_id is None:
            row = await self.k.db.fetchone("SELECT * FROM mem_preimages WHERE record_id=? ORDER BY id DESC LIMIT 1",
                                           (record_id,))
        else:
            row = await self.k.db.fetchone("SELECT * FROM mem_preimages WHERE id=? AND record_id=?",
                                           (int(preimage_id), record_id))
        if row is None:
            return False
        return await self.rewrite_summary(record_id, row["content"], reason=f"restore:{row['id']}", max_loss=1.0)

    # ------------------------------------------------------------------ precompute
    def _target_day(self, ctx: dict) -> tuple:
        tz = self.k.clock.tz
        if ctx.get("date"):
            d = datetime.strptime(str(ctx["date"]), "%Y-%m-%d").date()
        else:
            d = datetime.fromtimestamp(float(ctx["now"]) + 12 * 3600, tz).date()
        s = datetime(d.year, d.month, d.day, tzinfo=tz)
        return d.strftime("%Y-%m-%d"), s.timestamp(), (s + timedelta(days=1)).timestamp()

    async def _stage_precompute(self, ctx: dict) -> dict:
        p = self.cfg["precompute"]
        date, start, end = self._target_day(ctx)
        topics: list = []
        for h in await self.upcoming(start, end, limit=int(p["max_topics"])):
            topics.append({"topic": h.content, "person_key": (h.person_keys or [h.told_by] or [None])[0],
                           "kind": "upcoming", "source_id": h.id})
        mot = getattr(self.k, "motivation", None)
        if mot is not None:
            try:
                for it in await mot.active_intentions(limit=20):
                    due = getattr(it, "due_at", None)
                    if due is not None and start <= float(due) < end:
                        topics.append({"topic": it.goal, "person_key": getattr(it, "target_person", None),
                                       "kind": "intention", "source_id": it.id})
                for loop in await mot.open_loops(limit=7):
                    if isinstance(loop, str) and loop.strip():
                        topics.append({"topic": loop, "person_key": None, "kind": "open_loop", "source_id": ""})
            except Exception as e:
                log.warning("precompute: motivation unavailable: %s", e)
        seen, bundles = set(), []
        for t in topics:
            key = t["topic"].strip()
            if not key or key in seen:
                continue
            seen.add(key)
            hits = await self.retrieve(key, person_key=t["person_key"], budget_tokens=int(p["budget_tokens"]),
                                       limit=int(p["limit"]), tier="standard", touch=False)
            bundles.append({**t, "hit_ids": [h.id for h in hits],
                            "text": self.format_hits(hits, start + 9 * 3600)})
            if len(bundles) >= int(p["max_topics"]):
                break
        self._precomputed[date] = bundles
        for d in [d for d in self._precomputed if d < date and d != self.k.clock.today()]:
            self._precomputed.pop(d, None)
        await self.k.db.kv_set("mem.precompute", date, bundles, ts=self.k.clock.now())
        return {"date": date, "topics": len(bundles)}

    def precomputed(self, person_key: Optional[str] = None, date: Optional[str] = None) -> list:
        """Sleep-time bundles for ``date`` (default today): [{topic, person_key, kind, hit_ids, text}]."""
        bundles = self._precomputed.get(date or self.k.clock.today()) or []
        if person_key is None:
            return list(bundles)
        return [b for b in bundles if b.get("person_key") == person_key]
