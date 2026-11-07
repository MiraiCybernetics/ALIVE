"""Appraisal (docs/04 §4): System-1 quick rule table and deterministic OCC rules over appraisal variables.

Both functions are pure: they return *base* (type, intensity, note) proposals. Intensity modulation
(sensitivity, relationship weight, mood congruence, low-energy irritability) is applied by the engine.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from alive.affect.tables import normalize_type

Proposal = Tuple[str, float, str]   # (type, base intensity 0..1, cause/rule note)

QUICK_CAP = 0.4

# signal -> (type, base intensity, cause text). Some signals branch on relationship weight (rw).
QUICK_RULES = {
    "praised": ("pride", 0.30, "被夸了"),
    "thanked": ("gratification", 0.25, "被感谢了"),
    "insulted": ("anger", 0.35, "被冒犯了"),
    "ignored": ("disappointment", 0.20, "被冷落了"),
    "good_news": ("joy", 0.30, "听到好消息"),
    "bad_news": ("distress", 0.30, "听到坏消息"),
    "question": ("interest", 0.12, "有人问我问题"),
    "affectionate": ("liking", 0.25, "对方很亲近"),
    "threatened": ("fear", 0.30, "感觉被威胁"),
    "good_news_for_other": ("happy_for", 0.30, "对方有好事"),
    "bad_news_for_other": ("pity", 0.30, "对方遇到了不好的事"),
}


def _strength(v: object) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return max(0.0, min(1.0, float(v)))
    return 1.0 if v else 0.0


def quick_rules(signals: dict) -> List[Proposal]:
    """Rule table for System-1 signals -> coarse emotion proposals (base intensities, before modulation)."""
    out: List[Proposal] = []
    rw = _strength(signals.get("relationship_weight", 0.5)) if "relationship_weight" in signals else 0.5
    polar = False
    for sig, (etype, base, cause) in QUICK_RULES.items():
        st = _strength(signals.get(sig))
        if st <= 0:
            continue
        if sig == "ignored" and rw >= 0.6:
            etype, cause = "sadness", "被在乎的人冷落了"
        if sig == "insulted" and rw < 0.3:
            etype = "irritation"
        out.append((etype, base * st, cause))
        if sig == "insulted" and rw >= 0.6:
            out.append(("distress", 0.25 * st, "被在乎的人说了重话"))
        if sig == "affectionate":
            out.append(("joy", 0.10 * st, cause))
        if sig in ("praised", "thanked", "insulted", "ignored", "good_news", "bad_news", "affectionate",
                   "threatened"):
            polar = True
    s = signals.get("sentiment")
    if not polar and isinstance(s, (int, float)) and not isinstance(s, bool):
        s = max(-1.0, min(1.0, float(s)))
        if s >= 0.3:
            out.append(("joy", 0.2 * s, "聊得挺愉快"))
        elif s <= -0.3:
            out.append(("distress", 0.2 * -s, "对方情绪不太好"))
    return out


# --------------------------------------------------------------------------- OCC rules
APPRAISAL_KEYS = ("relevance", "desirability", "desirability_for_other", "agency", "praiseworthiness",
                  "likelihood", "coping_potential", "appealingness", "prospect")

_AGENCY = {
    "self": "self", "me": "self", "i": "self", "myself": "self", "我": "self", "自己": "self", "我自己": "self",
    "other": "other", "them": "other", "you": "other", "interlocutor": "other", "对方": "other", "他": "other",
    "她": "other", "你": "other", "person": "other",
    "third": "third", "third_party": "third", "others": "third", "第三方": "third", "别人": "third",
    "environment": "environment", "env": "environment", "world": "environment", "none": "environment",
    "环境": "environment", "circumstance": "environment", "nobody": "environment",
}


def has_appraisal(d: dict) -> bool:
    return any(k in d for k in ("desirability", "praiseworthiness", "desirability_for_other", "appealingness"))


def _f(d: dict, k: str, default: float, lo: float = -1.0, hi: float = 1.0) -> float:
    v = d.get(k, default)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def parse_likelihood(v: object) -> float:
    if v is None:
        return 1.0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return max(0.0, min(1.0, float(v)))
    s = str(v).strip().lower()
    if s in ("happened", "done", "actual", "occurred", "certain", "已发生", "发生了", "true", "yes"):
        return 1.0
    if s in ("possible", "maybe", "likely", "expected", "future", "可能", "也许", "将来", "prospect"):
        return 0.5
    if s in ("unlikely", "不太可能"):
        return 0.2
    try:
        return max(0.0, min(1.0, float(s)))
    except ValueError:
        return 1.0


def parse_agency(v: object) -> str:
    if v is None:
        return "environment"
    return _AGENCY.get(str(v).strip().lower(), _AGENCY.get(str(v).strip(), "environment"))


def occ_rules(a: dict, prior_prospect: Optional[Callable[[], Optional[str]]] = None) -> List[Proposal]:
    """Deterministic OCC mapping of appraisal variables to emotion proposals.

    Variables: relevance 0..1 (default 1), desirability -1..1 (for me), desirability_for_other -1..1,
    agency self|other|third|environment, praiseworthiness -1..1, likelihood 0..1 or "happened"/"possible"
    (default happened), coping_potential 0..1, appealingness -1..1 (objects/people),
    prospect "hoped"|"feared" (what I expected before an outcome; else ``prior_prospect()`` is asked).

    Branch order (most specific first):
      1. action of an agent (|praiseworthiness| >= .15, agency self/other/third):
         self: pride/shame, compound with desirability -> gratification/remorse
         other|third: admiration/reproach, compound -> gratitude/anger
      2. fortunes of others (|desirability_for_other| >= .2): happy_for / pity / resentment / gloating
      3. prospect (likelihood < 1): hope / anxiety (prospect-based fear, docs/04 §3.1)
      4. confirmation of a prior prospect: satisfaction / disappointment / relief / fears_confirmed
      5. well-being: joy / distress
      + aspect of objects (|appealingness| >= .2): liking / disliking (love / hate when >= .7)
    """
    out: List[Proposal] = []
    r = _f(a, "relevance", 1.0, 0.0, 1.0)
    d = _f(a, "desirability", 0.0)
    do = _f(a, "desirability_for_other", 0.0)
    pw = _f(a, "praiseworthiness", 0.0)
    cp = _f(a, "coping_potential", 0.5, 0.0, 1.0)
    lk = parse_likelihood(a.get("likelihood"))
    ag = parse_agency(a.get("agency"))

    if abs(pw) >= 0.15 and ag in ("self", "other", "third"):
        compound = abs(d) >= 0.15 and ((pw > 0) == (d > 0))
        if ag == "self":
            if compound:
                out.append(("gratification" if pw > 0 else "remorse", r * (abs(pw) + abs(d)) / 2, "occ:self_action+outcome"))
            else:
                out.append(("pride" if pw > 0 else "shame", r * abs(pw), "occ:self_action"))
        else:
            if compound:
                out.append(("gratitude" if pw > 0 else "anger", r * (abs(pw) + abs(d)) / 2, "occ:other_action+outcome"))
            else:
                out.append(("admiration" if pw > 0 else "reproach", r * abs(pw), "occ:other_action"))
    elif abs(do) >= 0.2:
        pleased = d >= 0
        if do > 0:
            et = "happy_for" if pleased else "resentment"
        else:
            et = "gloating" if (pleased and d > 0.15) else "pity"
        out.append((et, r * max(abs(do), abs(d)) if et != "pity" else r * abs(do), "occ:fortunes_of_others"))
    elif abs(d) >= 0.1:
        if lk < 0.95:
            if d > 0:
                out.append(("hope", r * abs(d) * (0.5 + 0.5 * lk), "occ:prospect"))
            else:
                out.append(("anxiety", r * abs(d) * (0.5 + 0.5 * lk) * (1.2 - 0.4 * cp), "occ:prospect"))
        else:
            prior = str(a.get("prospect") or "").strip().lower()
            if prior in ("hope", "hoped", "期待", "盼着"):
                prior = "hoped"
            elif prior in ("fear", "feared", "worried", "anxiety", "担心", "害怕"):
                prior = "feared"
            elif not prior and prior_prospect is not None:
                prior = prior_prospect() or ""
            if prior == "hoped":
                out.append(("satisfaction" if d > 0 else "disappointment", r * abs(d), "occ:prospect_confirmed"))
            elif prior == "feared":
                out.append(("relief" if d > 0 else "fears_confirmed", r * abs(d), "occ:prospect_confirmed"))
            else:
                if d > 0:
                    out.append(("joy", r * abs(d), "occ:wellbeing"))
                else:
                    out.append(("distress", r * abs(d) * (1.2 - 0.4 * cp), "occ:wellbeing"))
    ap = _f(a, "appealingness", 0.0)
    if abs(ap) >= 0.2:
        if ap > 0:
            out.append(("love" if ap >= 0.7 else "liking", r * abs(ap), "occ:object"))
        else:
            out.append(("hate" if ap <= -0.7 else "disliking", r * abs(ap), "occ:object"))
    return [(t, max(0.0, min(1.0, i)), n) for t, i, n in out if i > 0]


def explicit_type(delta: dict) -> str:
    return normalize_type(delta.get("type") or delta.get("emotion") or "")
