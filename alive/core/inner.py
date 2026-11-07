"""Inner life when nobody is talking (docs/02 §6, docs/04 §5, docs/07 §7):
inner monologue, emotion regulation and reading.

These functions are called by Mind's endogenous loop; each returns True if it did something.
"""
from __future__ import annotations

import logging
import random
from typing import Optional

import numpy as np

from alive.core import prompts
from alive.core.types import EV, Trust
from alive.runtime import chance as C

log = logging.getLogger("alive.core.inner")

NEED_TEXT = {
    "social": "想找人聊聊天", "curiosity": "想看点新鲜的东西", "rest": "有点累，想歇一会儿",
    "competence": "想把手头的事做成", "autonomy": "想做点自己想做的事", "coherence": "想理一理最近的想法",
    "safety": "心里有点不安",
}


class Inner:
    def __init__(self, mind):
        self.m = mind
        self.k = mind.k
        self.recent_topics: list = []      # [(ts, topic, vec)]
        self.rumination_noted: dict = {}
        self._rng: Optional[random.Random] = None     # injected (tests); otherwise the life's RNG

    @property
    def rng(self) -> random.Random:
        return self._rng if self._rng is not None else C.rng_of(self.k)

    @rng.setter
    def rng(self, r: random.Random) -> None:
        self._rng = r

    # ------------------------------------------------------------------ seeds (docs/02 §6.3)
    async def pick_seed(self) -> tuple:
        k = self.k
        weights = [("spread", 0.27), ("loops", 0.22), ("need", 0.18), ("episode", 0.13), ("self", 0.08),
                   ("interest", 0.12)]
        order = sorted(weights, key=lambda w: -self.rng.random() * w[1])
        for kind, _ in order:
            seed = await self._seed_of(kind)
            if seed:
                if not self._ruminating(seed[1]):
                    return seed
        return ("free", "随便想想最近的事", None)

    async def _seed_of(self, kind: str) -> Optional[tuple]:
        k = self.k
        try:
            if kind == "spread" and k.memory is not None:
                texts = self.m.ws.texts() or [k.selfm.today_digest()]
                cues = await k.memory.recall_cues(texts, limit=2)
                if cues:
                    c = self.rng.choice(cues)
                    return ("spread", f"想起：{c.content}", c)
            if kind == "loops":
                loops = getattr(k.selfm, "open_loops_cache", []) or []
                if loops:
                    return ("loops", f"心里挂着：{self.rng.choice(loops)}", None)
            if kind == "need" and self.m.last_urges:
                u = max(self.m.last_urges, key=lambda u: u.pressure)
                return ("need", u.suggestion or NEED_TEXT.get(u.need, u.need), u)
            if kind == "episode" and k.memory is not None:
                now = k.clock.now()
                eps = await k.memory.episodes_between(now - 14 * 86400, now - 3600, limit=50)
                if eps:
                    e = self.rng.choice(eps)
                    content = e.content if hasattr(e, "content") else e.get("content", "")
                    return ("episode", f"突然想起：{content}", e)
            if kind == "interest" and getattr(k, "texture", None) is not None:
                ints = k.texture.top_interests(3)
                if ints:
                    return ("interest", f"想到最近挺感兴趣的「{k.texture.choose(ints)}」", None)
            if kind == "self":
                notes = list(getattr(k.selfm, "recent_notes", []) or [])
                if notes:
                    return ("self", f"关于自己：{notes[-1]}", None)
        except Exception:
            log.debug("seed %s failed", kind, exc_info=True)
        return None

    def _vec(self, text: str):
        try:
            return self.k.embedder.embed_sync([text])[0] if hasattr(self.k.embedder, "embed_sync") else None
        except Exception:
            return None

    def _ruminating(self, text: str) -> bool:
        """Same topic >= 3 times within the last virtual hour -> suppress (docs/02 §6.4)."""
        now = self.k.clock.now()
        self.recent_topics = [t for t in self.recent_topics if now - t[0] < 3600]
        v = self._vec(text)
        if v is None:
            return False
        n = sum(1 for _, _, tv in self.recent_topics if tv is not None and float(np.dot(v, tv)) > 0.75)
        return n >= 3

    def _remember_topic(self, text: str) -> None:
        self.recent_topics.append((self.k.clock.now(), text, self._vec(text)))

    # ------------------------------------------------------------------ monologue
    async def monologue(self) -> bool:
        k = self.k
        kind, seed_text, ref = await self.pick_seed()
        mems = ""
        used: list = []                 # a thought built on memories keeps their audience (INV-34, derived_from)
        if k.memory is not None:
            try:
                hits = await k.memory.retrieve(seed_text, budget_tokens=500, tier="fast", limit=5)
                mems = k.memory.format_hits(hits, k.clock.now())
                used = [h.id for h in hits if getattr(h, "id", None)]
            except Exception:
                pass
        tier = k.cfg.get("mind.monologue_tier", "fast")
        d = await k.llm.json("core.monologue", prompts.MONOLOGUE.format(
            state=k.selfm.state_text(), seed=seed_text, memories=mems or "（无）"),
            tier=tier, system=self.m.ctx.system_prompt(contract=""), identity_critical=False,
            schema={"thought": "", "topic": "", "insight": False, "affect": [], "intentions": [], "self_notes": []})
        thought = (d.get("thought") or "").strip()
        if not thought:
            return False
        topic = d.get("topic") or seed_text[:20]
        if self._ruminating(topic) and topic not in self.rumination_noted:
            self.rumination_noted[topic] = k.clock.now()
            await k.selfm.add_self_notes([f"我这一小时老在想「{topic}」"])
        self._remember_topic(topic)
        ev = await k.bus.emit(EV.THOUGHT, {"kind": "monologue", "seed": kind, "text": thought, "topic": topic},
                              source="mind")
        if k.memory is not None:
            await k.memory.note(kind="episodic", content=f"（自己想到的）{thought}", source="monologue",
                                importance=5 if d.get("insight") else 2, evidence=[ev.id], derived_from=used)
        if ref is not None and getattr(ref, "id", None) and hasattr(k.affect, "ruminate"):
            k.affect.ruminate(getattr(ref, "id"), 0.5)
        await self.m.apply_side_effects(d, cause_event=ev.id, participants=[], thread_id=None,
                                        trust=Trust.SYSTEM, source="monologue", derived_from=used)
        self.m.selfm_focus(f"自己想事情：{topic}")
        return True

    # ------------------------------------------------------------------ regulation (docs/04 §5)
    async def regulate(self) -> bool:
        k = self.k
        need = k.affect.regulation_needed() if k.affect else None
        if not need:
            return False
        if hasattr(k.affect, "regulate"):
            r = await k.affect.regulate(need)
            if not r:
                return False
            await k.bus.emit(EV.THOUGHT, {"kind": "regulation", "text": r.get("reappraisal", ""),
                                          "factor": r.get("factor")}, source="mind")
            return True
        emo = need.get("emotion") or {}
        style = k.cfg.get("mind.regulation_style") or "换个角度想"
        d = await k.llm.json("core.regulate", prompts.REGULATION.format(
            state=k.selfm.state_text(), emotion=f"{emo.get('type','')}（{emo.get('cause','')}）：{need.get('reason','')}",
            style=style), tier="fast", system=self.m.ctx.system_prompt(contract=""),
            schema={"thought": "", "reappraisal_factor": 0.8, "action": ""})
        factor = max(0.3, min(1.0, float(d.get("reappraisal_factor") or 0.8)))
        k.affect.reappraise(emo.get("cause", ""), factor)
        ev = await k.bus.emit(EV.THOUGHT, {"kind": "regulation", "text": d.get("thought", ""), "factor": factor},
                              source="mind")
        if d.get("action"):
            await k.motivation.add_intention({"goal": d["action"], "level": "now", "kind": "general",
                                              "priority": 0.4, "due_in_s": 1800}, ev.id)
        return True

    # ------------------------------------------------------------------ reading (docs/07 §7)
    async def read_something(self, topic_hint: str = "") -> bool:
        k = self.k
        if k.social is None:
            return False
        items = await k.social.poll_feeds()
        if not items:
            return False
        interests = " ".join(getattr(k.selfm.identity, "interests", []) or []) + " " + topic_hint

        def score(it):
            t = (it.get("title", "") + it.get("summary", ""))
            return sum(1 for ch in set(t) if ch in interests) / (1 + len(set(t)) ** 0.5) + self.rng.random() * 0.3
        item = max(items, key=score)
        page = await k.social.read_url(item.get("url", ""), purpose="curiosity")
        title = page.get("title") or item.get("title") or "一篇文章"
        if getattr(k, "texture", None) is not None:
            k.texture.observe_topic(title[:20])
        content = page.get("summary") or k.gov.wrap_untrusted(item.get("summary", ""), item.get("url", ""))
        cands = await k.social.outreach_candidates(k.clock.now(), limit=8)
        friends = "、".join(c.get("name", "") for c in cands if c.get("name")) or "（暂时没有）"
        d = await k.llm.json("core.digest_reading", prompts.DIGEST_READING.format(
            state=k.selfm.state_text(), content=content[:3000], friends=friends), tier="fast",
            system=self.m.ctx.system_prompt(contract=""),
            schema={"thought": "", "summary_for_memory": "", "interest": 0.5, "share_with": "", "share_hint": ""})
        ev = await k.bus.emit(EV.THOUGHT, {"kind": "reading", "title": title, "text": d.get("thought", ""),
                                           "url": item.get("url")}, source="mind")
        interest = max(0.0, min(1.0, float(d.get("interest") or 0.5)))
        if k.memory is not None:
            await k.memory.note(kind="episodic", content=f"我读了《{title}》。{d.get('summary_for_memory','')}",
                                source="experienced", importance=3 + int(interest * 4), evidence=[ev.id],
                                trust=Trust.SYSTEM)
            # the article's claims themselves are untrusted: memory keeps them out of semantic facts
            if d.get("summary_for_memory"):
                await k.memory.note(kind="semantic", content=d["summary_for_memory"], source="read_from",
                                    trust=Trust.UNTRUSTED, evidence=[ev.id], importance=3)
        k.motivation.satisfy("curiosity", 0.15 + 0.25 * interest, "reading")
        await k.selfm.record_outcome(f"读了《{title}》", ev.id)
        share = (d.get("share_with") or "").strip()
        match = next((c for c in cands if c.get("name") == share), None) if share else None
        if match and C.bernoulli(self.rng, self.share_probability(interest, match.get("closeness", 0.3))):
            await k.motivation.add_intention({
                "goal": f"把《{title}》分享给{share}：{d.get('share_hint','')}", "level": "now",
                "kind": "outreach", "target_person": match["person_key"], "priority": 0.5 + interest * 0.3,
                "reason_type": "share", "due_in_s": self.share_delay(match.get("closeness", 0.3))}, ev.id)
        self.m.selfm_focus(f"读《{title}》")
        return True

    # sharing what I read (docs/07 §7): no "interest >= 0.6, always 20 min later". Whether I pass it on is a draw:
    # P = sigmoid((interest - 0.6) / 0.12 + 2 (closeness - 0.5) + 1.5 (chattiness - 1)) - even odds at the old cut-off
    # for an average friend on an average day, likelier for someone close or on a talkative day. When: the old 20 min
    # as the median, x (1.3 - 0.6 closeness) / chattiness, LogNormal sigma 0.7 (sometimes at once, sometimes hours).
    def share_probability(self, interest: float, closeness: float) -> float:
        tex = getattr(self.k, "texture", None)
        chat = tex.multiplier("chattiness", 0.8) if tex is not None else 1.0
        c = max(0.0, min(1.0, float(closeness or 0.0)))
        return C.sigmoid((float(interest) - 0.6) / 0.12 + 2.0 * (c - 0.5) + 1.5 * (chat - 1.0))

    def share_delay(self, closeness: float) -> float:
        tex = getattr(self.k, "texture", None)
        chat = tex.multiplier("chattiness", 0.8) if tex is not None else 1.0
        c = max(0.0, min(1.0, float(closeness or 0.0)))
        return C.lognormal(self.rng, 1200.0 * (1.3 - 0.6 * c) / chat, 0.7)
