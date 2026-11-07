"""Expression mapping (docs/04 §6): numbers -> first-person Chinese labels, style factors, behavior modulators.

Pure functions over an ``ExprState``. "Numbers are truth, language is for use": prompts never see raw PAD.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from alive.affect.dynamics import DEFAULT_BANDS, NEUTRAL_NORM, clamp, mood_octant, norm
from alive.runtime.chance import soft_step
from alive.affect.tables import (EMOTION_WORD_ZH, HIGH_AROUSAL_NEGATIVE, MOOD_PHRASES_ZH, NEUTRAL_MOOD_ZH,
                                 is_negative)


@dataclass
class ExprState:
    mood: Tuple[float, float, float]
    default: Tuple[float, float, float]
    emotions: List[dict]
    energy: float
    sleep_pressure: float
    circadian: float
    social_battery: float
    stress: float
    boredom: float
    asleep: bool
    hour: float
    now: float
    woke_at: Optional[float] = None
    big5: Dict[str, float] = field(default_factory=dict)
    regulation_style: str = "reappraise"
    mood_phrases: Dict[str, Sequence[str]] = field(default_factory=dict)
    bands: Sequence[float] = DEFAULT_BANDS
    texture: Dict[str, float] = field(default_factory=dict)   # today's life-texture multipliers (around 1.0)


# --------------------------------------------------------------------------- labels
def time_of_day_zh(hour: float) -> str:
    h = hour % 24
    if h < 5:
        return "凌晨"
    if h < 8:
        return "早上"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    return "晚上"


def mood_label(st: ExprState) -> str:
    octant, band, _ = mood_octant(st.mood, st.bands, NEUTRAL_NORM)
    if octant == "neutral":
        return NEUTRAL_MOOD_ZH
    phrases = st.mood_phrases.get(octant) or MOOD_PHRASES_ZH[octant]
    idx = {"slightly": 0, "moderately": 1, "fully": 2}[band]
    return str(phrases[min(idx, len(phrases) - 1)])


def emotion_phrase(e: dict) -> str:
    et = e.get("type", "")
    w = EMOTION_WORD_ZH.get(et, et)
    name = e.get("target_name") or ""
    if et == "happy_for" and name:
        w = f"替{name}高兴"
    elif et == "pity" and name:
        w = f"心疼{name}"
    cause = (e.get("cause_text") or "").strip().rstrip("。.!！")
    x = float(e.get("intensity", 0.0))
    residual = x < 0.7 * float(e.get("initial", x) or x)
    if x < 0.35:
        lead = "还有点" if residual else "有点"
        return f"{lead}因为{cause}而{w}" if cause else f"{lead}{w}"
    if x < 0.65:
        return f"因为{cause}挺{w}的" if cause else f"挺{w}的"
    return f"因为{cause}特别{w}" if cause else f"特别{w}"


def top_emotions(emotions: Sequence[dict], n: int = 2, min_intensity: float = 0.12) -> List[dict]:
    """Strongest emotions of distinct types (docs/04 OQ-26: expression uses only the strongest 1-2)."""
    seen = set()
    out: List[dict] = []
    for e in sorted(emotions, key=lambda x: -float(x.get("intensity", 0.0))):
        if float(e.get("intensity", 0.0)) < min_intensity or e.get("type") in seen:
            continue
        seen.add(e.get("type"))
        out.append(e)
        if len(out) >= n:
            break
    return out


def emotion_line(st: ExprState) -> str:
    return "；".join(emotion_phrase(e) for e in top_emotions(st.emotions))


def energy_label(st: ExprState) -> str:
    tod = time_of_day_zh(st.hour)
    if st.asleep:
        return "在睡觉，迷迷糊糊的"
    if st.woke_at is not None and 0 <= st.now - st.woke_at < 30 * 60:
        base = f"{tod}刚醒，还有点迷糊"
    else:
        e = st.energy
        sleepy = st.sleep_pressure > 0.45 or st.circadian < -0.3
        if e >= 0.75:
            phrase = "精神很好"
        elif e >= 0.55:
            phrase = "精神还行"
        elif e >= 0.35:
            phrase = "有点犯困" if sleepy else "有点累"
        elif e >= 0.2:
            phrase = "挺困的" if sleepy else "挺累的"
        else:
            phrase = "困得不行" if sleepy else "累得不行"
        base = f"{tod}{phrase}"
    extras = []
    if st.social_battery < 0.25:
        extras.append("不太想社交")
    if st.stress > 0.6:
        extras.append("压力有点大")
    if st.boredom > 0.6 and st.energy >= 0.35:
        extras.append("有点闲得发慌")
    return base + ("，" + "，".join(extras) if extras else "")


def labels(st: ExprState) -> dict:
    return {"mood": mood_label(st), "emotion": emotion_line(st), "energy": energy_label(st)}


# --------------------------------------------------------------------------- style (docs/04 §6.2)
# corners (p, a) = (+,+), (+,-), (-,+), (-,-) chosen so the neutral point maps to 1.0
_STYLE_CORNERS = {
    "length": (1.25, 1.05, 0.85, 0.85),
    "emoji": (1.5, 1.1, 0.6, 0.8),
    "delay": (0.8, 1.05, 0.9, 1.25),
}


def _bilinear(corners: Tuple[float, float, float, float], p: float, a: float) -> float:
    hh, hl, lh, ll = corners
    c = (hh + hl + lh + ll) / 4
    ap = (hh + hl - lh - ll) / 4
    aa = (hh - hl + lh - ll) / 4
    b = (hh - hl - lh + ll) / 4
    return c + ap * p + aa * a + b * p * a


def _tone(st: ExprState) -> str:
    P, A, D = st.mood
    if st.energy < 0.25 and not st.asleep:
        return "简略"
    if norm((P, A)) < 0.15:
        return "平和"
    if P >= 0 and A >= 0:
        return "活泼"
    if P >= 0:
        return "温和、慢条斯理"
    if A >= 0:
        return "生硬" if D >= 0 else "急促"
    return "平淡"


def style(st: ExprState) -> dict:
    """Factors around 1.0 relative to the persona's *default* mood (her normal voice = 1.0); tone from
    the absolute mood quadrant. Extra keys: delay_jitter (unsteady delays when tense), expressiveness."""
    p = clamp((st.mood[0] - st.default[0]) / 0.6)
    a = clamp((st.mood[1] - st.default[1]) / 0.6)
    length = _bilinear(_STYLE_CORNERS["length"], p, a)
    emoji = _bilinear(_STYLE_CORNERS["emoji"], p, a)
    delay = _bilinear(_STYLE_CORNERS["delay"], p, a)
    jitter = 0.15 + 0.35 * max(0.0, -p) * max(0.0, a)
    e_low = clamp((0.4 - st.energy) / 0.4, 0.0, 1.0)
    length *= 1 - 0.3 * e_low
    delay *= 1 + 0.5 * e_low
    emoji *= 1 - 0.2 * e_low
    # social battery running low: shorter and slower, smoothly (logistic around 0.3, width 0.06: ~0.08 at 0.45,
    # 0.5 at 0.3, ~0.92 at 0.15) instead of a step at 0.3
    b_low = soft_step(0.3 - st.social_battery, 0.0, 0.06)
    length *= 1 - 0.1 * b_low
    delay *= 1 + 0.1 * b_low
    expressiveness = {"suppress": 0.6, "express": 1.2, "mixed": 0.9}.get(st.regulation_style, 1.0)
    return {"length": round(length, 3), "emoji": round(max(0.0, emoji), 3), "delay": round(delay, 3),
            "tone": _tone(st), "delay_jitter": round(jitter, 3), "expressiveness": expressiveness}


# --------------------------------------------------------------------------- modulators (docs/04 §6.3)
def negative_arousal(st: ExprState) -> float:
    """0..1 how much the state is a high-arousal negative one (anger/fear/anxiety-like)."""
    P, A, _ = st.mood
    mood_part = clamp(-P / 0.6, 0.0, 1.0) * clamp(A / 0.6, 0.0, 1.0)
    emo = max([float(e.get("intensity", 0.0)) for e in st.emotions if e.get("type") in HIGH_AROUSAL_NEGATIVE]
              or [0.0])
    return clamp(max(mood_part, emo), 0.0, 1.0)


def modulators(st: ExprState) -> dict:
    """{"willingness", "salience_affect", "patience"}. Temperament enters through today's texture: neuroticism is
    shifted by the sensitivity texture (multiplier 0.5..1.5 -> N +-0.25), and the sociability texture nudges the
    willingness to engage (+-0.075). (The former "initiative_threshold" is gone: initiative is a hazard, docs/21.)"""
    P, A, _ = st.mood
    pd, ad = P - st.default[0], A - st.default[1]
    N = float(st.big5.get("N", 0.5))
    Ag = float(st.big5.get("A", 0.5))
    tex = st.texture or {}
    N_eff = clamp(N + 0.5 * (float(tex.get("sensitivity", 1.0)) - 1.0), 0.0, 1.0)
    e_low = clamp((0.3 - st.energy) / 0.3, 0.0, 1.0)
    b_low = clamp((0.4 - st.social_battery) / 0.4, 0.0, 1.0)
    na = negative_arousal(st)
    willingness = 0.15 * pd + 0.05 * ad - 0.25 * b_low - 0.15 * e_low + 0.08 * st.boredom
    willingness += 0.15 * (float(tex.get("sociability", 1.0)) - 1.0)
    if st.asleep:
        willingness -= 0.3
    strongest = max([float(e.get("intensity", 0.0)) for e in st.emotions] or [0.0])
    salience = 1.0 + 0.6 * strongest + 0.2 * st.stress + 0.2 * (N_eff - 0.5)
    patience = 1.0 - 0.3 * na - 0.25 * e_low - 0.2 * st.stress + 0.2 * (Ag - 0.5)
    return {"willingness": round(clamp(willingness, -0.5, 0.5), 4),
            "salience_affect": round(clamp(salience, 0.6, 2.0), 4),
            "patience": round(clamp(patience, 0.3, 1.3), 4)}


def is_negative_emotion(e: dict) -> bool:
    return is_negative(e.get("type", ""))
