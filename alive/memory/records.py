"""Record-level rules: provenance (INV-11 / INV-14), strength & retrievability (docs/03 §6),
row -> hit conversion and provenance tags for prompts (docs/03 §5.2 step 5)."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Optional

from alive.core.types import Trust
from alive.interfaces import MemoryHit

DAY = 86400.0
KINDS = ("episodic", "semantic", "opinion", "relational", "self")
SOURCES = ("experienced", "told_by", "read_from", "inferred", "reflected", "seeded", "dreamed", "monologue")
SELF_GENERATED = ("dreamed", "monologue", "seeded")      # INV-11: never semantic
OPINION_ONLY = ("inferred", "reflected")                  # INV-11: opinion memory only
PINNED_STRENGTH = 1e12

# statuses used by retrieval tiers
LIVE_STATUSES = ("active", "pinned", "stale", "pending")
DEEP_STATUSES = LIVE_STATUSES + ("compressed", "invalid", "superseded")

DEFAULT_BASE_DAYS = {"episodic": 3.0, "semantic": 30.0, "opinion": 14.0, "relational": 60.0, "self": 90.0}

STATUS_PENALTY = {"stale": 0.4, "pending": 0.3, "compressed": 0.3, "invalid": 0.9, "superseded": 0.9}
CONFLICT_PENALTY = 0.3


@dataclass
class Provenance:
    kind: str
    source: str
    told_by: Optional[str]
    trust: int
    status: str = "active"
    downgraded_from: Optional[str] = None
    url: Optional[str] = None
    notes: list = field(default_factory=list)


def normalize_source(source: str, told_by: Optional[str]) -> tuple:
    """'told_by:<pk>' -> ('told_by', pk); 'read_from:<url>' -> ('read_from', None, url)."""
    s = (source or "experienced").strip()
    url = None
    if s.startswith("told_by:"):
        told_by = told_by or s.split(":", 1)[1] or None
        s = "told_by"
    elif s.startswith("read_from:"):
        url = s.split(":", 1)[1] or None
        s = "read_from"
    if s not in SOURCES:
        s = "experienced" if not told_by else "told_by"
    if s == "told_by" and not told_by:
        s = "experienced"
    return s, told_by, url


def apply_provenance_rules(kind: str, source: str, told_by: Optional[str], trust: Any,
                           semantic_min_trust: int = int(Trust.PUBLIC)) -> Provenance:
    """Enforce INV-11 and INV-14 at write time. Never raises; downgrades instead.

    - dreamed / monologue / seeded never become semantic (or relational/self facts): -> episodic
      (monologue -> opinion: a passing thought is at best a belief).
    - inferred / reflected only enter opinion memory.
    - UNTRUSTED content never becomes a semantic/relational/self fact: -> episodic ("I read/heard X").
    - Claims by others about me (kind self, not experienced) are kept but marked pending verification;
      they never overwrite autobiographical memory (docs/03 §4.2.3).
    """
    k = kind if kind in KINDS else "episodic"
    src, tb, url = normalize_source(source, told_by)
    try:
        t = int(Trust.parse(trust))
    except Exception:
        t = int(Trust.PUBLIC)
    p = Provenance(kind=k, source=src, told_by=tb, trust=t, url=url)
    fact_kinds = ("semantic", "relational", "self")
    if src in SELF_GENERATED and k in fact_kinds:
        p.downgraded_from, p.kind = k, ("opinion" if src == "monologue" else "episodic")
        p.notes.append(f"INV-11: {src} cannot be {k}")
    elif src in OPINION_ONLY and k != "opinion":
        p.downgraded_from, p.kind = k, "opinion"
        p.notes.append(f"INV-11: {src} -> opinion")
    elif t <= int(Trust.UNTRUSTED) and k in fact_kinds:
        p.downgraded_from, p.kind = k, "episodic"
        p.notes.append("INV-14: untrusted content cannot be a fact")
    elif k in ("semantic", "relational") and src in ("told_by", "read_from") and t < semantic_min_trust:
        p.downgraded_from, p.kind = k, "episodic"
        p.notes.append("trust below semantic_min_trust")
    if p.kind == "self" and p.source != "experienced":
        p.status = "pending"
    return p


def initial_strength(kind: str, importance: int, valence: float, arousal: float, peak: bool,
                     base_days: Optional[dict] = None, pinned: bool = False) -> float:
    """S0 = base[kind] × (1 + importance/5) × (1 + 0.5·|valence|·arousal) × (peak ? 2 : 1), in seconds."""
    if pinned:
        return PINNED_STRENGTH
    b = (base_days or DEFAULT_BASE_DAYS).get(kind, 7.0) * DAY
    imp = max(1, min(10, int(importance)))
    s = b * (1 + imp / 5.0) * (1 + 0.5 * abs(valence) * max(0.0, min(1.0, arousal))) * (2.0 if peak else 1.0)
    return float(s)


def retrievability(now: float, last_access: float, strength: float) -> float:
    if strength >= PINNED_STRENGTH:
        return 1.0
    dt = max(0.0, now - last_access)
    return math.exp(-dt / max(1.0, strength))


def recall_strength(strength: float, last_access: float, now: float, gain: float = 0.5,
                    max_strength: float = 3650 * DAY) -> float:
    """S <- S × (1 + g · spacing_bonus); spacing_bonus = 1 - R(now): the more forgotten, the bigger the boost."""
    if strength >= PINNED_STRENGTH:
        return strength
    bonus = 1.0 - retrievability(now, last_access, strength)
    return float(min(max_strength, strength * (1.0 + gain * bonus)))


# ---------------------------------------------------------------------------
# Hits
# ---------------------------------------------------------------------------
@dataclass
class Hit(MemoryHit):
    """MemoryHit plus the extra fields memory keeps; also readable like a dict (``h["content"]``)."""
    entities: list = field(default_factory=list)
    refers_raw: Optional[str] = None
    thread_id: Optional[str] = None
    strength: float = 0.0
    last_access: float = 0.0
    access_count: int = 0
    conflict: bool = False
    derived_from: list = field(default_factory=list)
    version: int = 1
    meta: dict = field(default_factory=dict)

    def __getitem__(self, k: str) -> Any:
        if k == "ts":
            return self.observed_at
        return getattr(self, k)

    def get(self, k: str, default: Any = None) -> Any:
        try:
            return self[k]
        except AttributeError:
            return default

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.observed_at
        return d


def _jl(v: Any, default: Any) -> Any:
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


def row_to_hit(r, score: float = 0.0) -> Hit:
    refers = None
    if r["ref_start"] is not None:
        refers = {"start": r["ref_start"], "end": r["ref_end"], "granularity": r["ref_granularity"],
                  "raw": r["ref_raw"]}
    aff = None
    if r["aff_valence"] or r["aff_arousal"] or r["aff_label"] or r["aff_peak"]:
        aff = {"valence": r["aff_valence"], "arousal": r["aff_arousal"], "label": r["aff_label"],
               "peak": bool(r["aff_peak"])}
    return Hit(id=r["id"], kind=r["kind"], content=r["content"], score=float(score), source=r["source"],
               told_by=r["told_by"], observed_at=float(r["observed_at"]), refers_to=refers,
               importance=int(r["importance"]), status=r["status"], trust=int(r["trust"]),
               person_keys=_jl(r["person_keys"], []), affect=aff, entities=_jl(r["entities"], []),
               refers_raw=r["ref_raw"], thread_id=r["thread_id"], strength=float(r["strength"]),
               last_access=float(r["last_access"]), access_count=int(r["access_count"]),
               conflict=bool(_jl(r["conflict_with"], [])), derived_from=_jl(r["derived_from"], []),
               version=int(r["version"]), meta=_jl(r["meta"], {}))


# ---------------------------------------------------------------------------
# Provenance tags (Chinese, relative time)
# ---------------------------------------------------------------------------
def relative_time_cn(ts: float, now: float, tz: tzinfo) -> str:
    dt = now - ts
    if dt < 0:
        dt = 0
    if dt < 600:
        return "刚才"
    if dt < 3600:
        return f"{int(dt // 60)} 分钟前"
    d_now = datetime.fromtimestamp(now, tz).date()
    d_ts = datetime.fromtimestamp(ts, tz).date()
    days = (d_now - d_ts).days
    if days == 0:
        return f"{int(dt // 3600)} 小时前" if dt < 6 * 3600 else "今天早些时候"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days < 7:
        return f"{days} 天前"
    if days < 30:
        return f"{days // 7} 周前"
    if days < 365:
        return f"{max(1, days // 30)} 个月前"
    return f"{days // 365} 年前"


def provenance_tag(h: MemoryHit, now: float, tz: tzinfo, names: dict) -> str:
    when = relative_time_cn(h.observed_at, now, tz)
    src = h.source
    if src == "told_by" and h.told_by:
        nm = names.get(h.told_by) or h.told_by.split(":")[-1]
        base = f"{nm} 说的，{when}"
    elif src == "read_from":
        base = "读到的，不一定可靠" if h.trust <= int(Trust.UNTRUSTED) else f"读到的，{when}"
    elif src == "inferred":
        base = "我猜的"
    elif src == "reflected":
        base = "我自己琢磨的"
    elif src == "dreamed":
        base = "梦到的，不是真的"
    elif src == "monologue":
        base = "我脑子里想过的"
    elif src == "seeded":
        base = "预设的背景记忆，不是真实经历"
    else:
        base = f"我亲历的，{when}" if h.kind == "episodic" else f"我知道的，{when}"
    if h.source != "read_from" and h.trust <= int(Trust.UNTRUSTED):
        base += "，不一定可靠"
    extra = {"stale": "，可能已经过时", "pending": "，还没核实", "compressed": "，印象模糊了",
             "invalid": "，后来知道不对了", "superseded": "，后来有更新"}.get(h.status, "")
    if getattr(h, "conflict", False):
        extra += "，有矛盾的说法"
    return f"（{base}{extra}）"


def date_note(h: MemoryHit, tz: tzinfo) -> str:
    """'〔指 11月18日 周三〕' for records with a resolved refers_to."""
    ref = h.refers_to or {}
    if not ref or ref.get("start") is None:
        return ""
    st = datetime.fromtimestamp(float(ref["start"]), tz)
    wd = "一二三四五六日"[st.weekday()]
    g = ref.get("granularity") or "day"
    if g == "hour":
        s = f"{st.month}月{st.day}日 周{wd} {st.hour:02d}:{st.minute:02d}"
    elif g in ("week", "range"):
        en = datetime.fromtimestamp(float(ref.get("end") or ref["start"]) - 1, tz)
        s = f"{st.month}月{st.day}日–{en.month}月{en.day}日"
    elif g == "month":
        s = f"{st.year}年{st.month}月"
    elif g == "year":
        s = f"{st.year}年"
    else:
        s = f"{st.month}月{st.day}日 周{wd}"
    return f"〔指 {s}〕"
