"""Learning agenda (docs/06 §7): score = learnable × interesting × novel × goal_fit.

Candidate sources: curiosity need, repeated failures (harmful evidence tags), requests in open loops,
life/project intentions, and "want to try" notes from tonight's reflections. The model proposes topics
with a mandatory success criterion; scoring combines model judgement with local statistics:
- learnable: the model's estimate, pulled towards the middle-difficulty optimum by the recent success
  rate on matching evidence tags (too easy or too hard scores low);
- novel: 1 − max cosine to what I already know/learn (skills, current learn intentions, playbook);
- goal_fit: max of the model's estimate and cosine to life/project goals.
Items without a success criterion are dropped. The top 1–3 become ``learn`` intentions.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from alive.growth.prompts import CURRICULUM_SCHEMA, CURRICULUM_SYSTEM, CURRICULUM_USER
from alive.growth.util import clamp, field, norm

log = logging.getLogger("alive.growth")

_REQUEST_WORDS = ("教", "学", "怎么", "推荐", "帮我", "解释", "讲讲", "teach", "learn", "explain")
_TRY_WORDS = ("想试", "想学", "想尝试", "下次试", "下周想", "想弄明白")


async def _gather_sources(g, sctx) -> tuple:
    k = g.k
    sources: list = []
    goals: list = []
    known: list = []
    mot = k.motivation
    if mot is not None:
        try:
            needs = mot.needs() or {}
            cur = needs.get("curiosity") or {}
            lvl, sp = float(cur.get("level", 0.5) or 0.5), float(cur.get("setpoint", 0.7) or 0.7)
            if lvl < sp:
                sources.append(f"好奇心偏低（{lvl:.2f} < {sp:.2f}），想学点新鲜的")
        except Exception:
            pass
        try:
            for it in await mot.active_intentions(limit=30):
                lv, kind, goal = field(it, "level", ""), field(it, "kind", ""), field(it, "goal", "")
                if not goal:
                    continue
                if kind == "learn":
                    known.append(goal)
                if lv in ("life", "project"):
                    goals.append(goal)
                    sources.append(f"长期目标：{goal}")
            for loop in await mot.open_loops(limit=10):
                if isinstance(loop, str) and any(w in loop for w in _REQUEST_WORDS):
                    sources.append(f"别人的请求/没做完的事：{loop}")
        except Exception as e:
            log.debug("curriculum: motivation unavailable: %s", e)
    try:
        harm = await g.evidence.since(sctx.now - 7 * 86400, polarity="harmful")
        counts: dict = {}
        for e in harm:
            counts[e.about] = counts.get(e.about, 0) + 1
        for tag, n in sorted(counts.items(), key=lambda x: -x[1]):
            if n >= 2:
                sources.append(f"反复出现的失误：{tag}（7 天内 {n} 次）")
    except Exception:
        pass
    for r in (sctx.state.get("reflections") or []):
        t = r.get("text", "") if isinstance(r, dict) else str(r)
        if any(w in t for w in _TRY_WORDS):
            sources.append(f"我想尝试：{t}")
    for s in g.skills_registry.list():
        known.append(f"{s['name']}：{s['description']}")
    for a in g.artifacts.active("playbook")[:20]:
        known.append(a.text)
    return sources, goals, known


def _success_rate_learnable(g_evidence_rows: list, topic: str) -> Any:
    rel = [e for e in g_evidence_rows if e.about and (norm(e.about) in norm(topic) or norm(topic) in norm(e.about))]
    if not rel:
        return None
    h = sum(1 for e in rel if e.polarity == "helpful")
    b = sum(1 for e in rel if e.polarity == "harmful")
    if h + b == 0:
        return None
    p = h / (h + b)
    return max(0.2, 1.0 - abs(p - 0.5) * 2)


async def build_agenda(g, sctx) -> list:
    k = g.k
    cfg = g.cfg["curriculum"]
    sources, goals, known = await _gather_sources(g, sctx)
    if not sources:
        return []
    try:
        identity = k.selfm.identity_digest() if k.selfm is not None else ""
        me = k.selfm.name() if k.selfm is not None else "我"
    except Exception:
        identity, me = "", "我"
    prompt = CURRICULUM_USER.format(me=me, identity=identity[:400], sources="\n".join(f"- {s}" for s in sources[:12]),
                                    known="\n".join(f"- {x}" for x in known[:15]) or "（暂无）")
    try:
        data = await k.llm.json("grow.curriculum", prompt, tier="deliberate", system=CURRICULUM_SYSTEM,
                                schema=CURRICULUM_SCHEMA)
    except Exception as e:
        log.warning("grow.curriculum failed: %s", e)
        return []
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    items = [i for i in items if str(i.get("topic") or "").strip() and str(i.get("success_criterion") or "").strip()]
    if not items:
        return []
    topics = [str(i["topic"]).strip() for i in items]
    tv = await k.embedder.embed(topics)
    kv = await k.embedder.embed(known) if known else np.zeros((0, k.embedder.dim), dtype=np.float32)
    gv = await k.embedder.embed(goals) if goals else np.zeros((0, k.embedder.dim), dtype=np.float32)
    try:
        ev_rows = await g.evidence.since(sctx.now - 30 * 86400)
    except Exception:
        ev_rows = []
    scored = []
    for it, v, topic in zip(items, tv, topics):
        learn_m = clamp(it.get("learnable", 0.5))
        stat = _success_rate_learnable(ev_rows, topic)
        learnable = learn_m if stat is None else (learn_m + stat) / 2
        interesting = clamp(it.get("interesting", 0.5))
        novel = 1.0 - (float(np.max(kv @ v)) if len(kv) else 0.0)
        novel = clamp(novel, 0.05, 1.0)
        goal_fit = clamp(it.get("goal_fit", 0.5))
        if len(gv):
            goal_fit = max(goal_fit, clamp(float(np.max(gv @ v))))
        goal_fit = max(goal_fit, 0.1)
        score = learnable * interesting * novel * goal_fit
        scored.append({"topic": topic, "why": str(it.get("why") or "")[:80],
                       "success_criterion": str(it["success_criterion"]).strip()[:120],
                       "learnable": round(learnable, 3), "interesting": round(interesting, 3), "novel": round(novel, 3),
                       "goal_fit": round(goal_fit, 3), "score": round(score, 4)})
    scored = [s for s in scored if s["score"] >= float(cfg["min_score"])]
    scored.sort(key=lambda s: -s["score"])
    return scored[: int(cfg["max_items"])]


async def apply_agenda(g, agenda: list) -> list:
    """Turn agenda items into ``learn`` intentions (each carries its success criterion in the plan)."""
    mot = g.k.motivation
    ids = []
    if mot is None:
        return ids
    for a in agenda:
        try:
            it = await mot.add_intention({"goal": f"学习：{a['topic']}", "level": "project", "kind": "learn",
                                          "priority": round(0.3 + 0.6 * min(1.0, a["score"] * 4), 3),
                                          "plan": [f"完成标准：{a['success_criterion']}"] + ([f"原因：{a['why']}"] if a.get("why") else []),
                                          "is_commitment": False})
            ids.append(getattr(it, "id", ""))
        except Exception as e:
            log.warning("add learn intention failed: %s", e)
    return ids
