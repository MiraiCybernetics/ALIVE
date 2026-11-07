"""Embodiment helpers (docs/07 §4): segment splitting and the human-ish delay model.

Splitting: one long draft -> at most 3 messages on sentence / newline boundaries; tiny pieces are merged
into neighbours; code blocks, inline code and URLs are never split.

Delays (virtual seconds; the pace once I'm looking -- WHEN I look is alive/social/air.py):
    first  = reaction + read(incoming) + typing(segment) − time already spent thinking   (>= 30% typing)
    next   = gap + typing(segment)
    reaction ~ LogNormal(median 4 s × mode × tiredness × (1.3 − 0.6·closeness), σ 0.9)
             (mid-conversation replies take ~40 s with close ties / ~1 min with good friends in all, reading,
             thinking and typing included -- research note air_reading_timing_priors.md §2)
    read     = CJK chars / 15 + latin words / 4
    typing   = CJK chars / today's speed (Gamma, mean 4/s, cv 0.15, drawn once a day) × (0.75 + 0.25·energy)
               + latin words / 1.0 + other symbols / 8, × LogNormal(1, 0.2) per message
    gap      ~ LogNormal(0.7 s, 0.6) between my own consecutive messages
    every delay × affect.style()["delay"]; only a technical ceiling, no behavioural caps
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from typing import Optional

from alive.social import text as T

_PROTECT = re.compile(r"```.*?```|`[^`\n]+`|https?://\S+|www\.[^\s]+", re.S)
# a boundary is after a run of sentence-final punctuation (+ closing quotes/brackets), or at newline runs
_BOUNDARY = re.compile(r"(?:[。！？!?…~～]+[」』”’\"')）\]]*[ \t]*)|(?:\n+)|(?:(?<=[a-z0-9\)\"'])[.](?=\s+[A-Z]))")


# typed symbols: anything that is not whitespace, latin alnum or CJK/kana/hangul
_SYMBOL = re.compile(r"[^\sA-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def _protected_spans(text: str) -> list:
    return [(m.start(), m.end()) for m in _PROTECT.finditer(text)]


def _inside(pos: int, spans: list) -> bool:
    return any(a < pos < b for a, b in spans)


def split_segments(text: str, max_segments: int = 3, min_chars: int = 24, tiny_chars: int = 4) -> list:
    """Split a draft into <= max_segments chat messages. Returns [] for empty text."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= min_chars or max_segments <= 1:
        return [text]
    spans = _protected_spans(text)
    cuts = []
    for m in _BOUNDARY.finditer(text):
        end = m.end()
        if 0 < end < len(text) and not _inside(m.start(), spans) and not _inside(end, spans):
            cuts.append(end)
    # also cut right after a code block that ends a paragraph? (keep simple: code stays with neighbours)
    bounds = [0] + sorted(set(cuts)) + [len(text)]
    pieces = [text[a:b] for a, b in zip(bounds, bounds[1:]) if text[a:b].strip()]
    if len(pieces) <= 1:
        return [text]

    def size(p: str) -> int:
        return len(p.strip())

    # merge tiny pieces ("嗯。", "!") into the previous piece (or the next one if first)
    merged: list = []
    for p in pieces:
        if merged and size(p) < tiny_chars:
            merged[-1] += p
        else:
            merged.append(p)
    if len(merged) > 1 and size(merged[0]) < tiny_chars:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    pieces = merged
    # greedily merge the adjacent pair with the smallest combined length until within the limit
    while len(pieces) > max_segments:
        best_i, best_len = 0, None
        for i in range(len(pieces) - 1):
            ln = size(pieces[i]) + size(pieces[i + 1])
            if best_len is None or ln < best_len:
                best_i, best_len = i, ln
        pieces[best_i:best_i + 2] = [pieces[best_i] + pieces[best_i + 1]]
    out = [p.strip() for p in pieces if p.strip()]
    return out or [text]


def normalize_segments(segments, max_segments: int = 3, min_chars: int = 24) -> list:
    """List of drafts -> list of messages: a single long draft is split; more than ``max_segments`` drafts
    are merged from the tail (keeps the first messages as the model wrote them)."""
    segs = [s.strip() for s in (segments or []) if isinstance(s, str) and s.strip()]
    if len(segs) == 1:
        return split_segments(segs[0], max_segments=max_segments, min_chars=min_chars)
    if len(segs) > max_segments > 0:
        head, tail = segs[:max_segments - 1], segs[max_segments - 1:]
        segs = head + ["\n".join(tail)]
    return segs


@dataclass
class DelayParams:
    """Medians and spreads of the pace model (ADR-0007: draws, not fixed delays; no behavioural caps)."""
    reaction_median_s: float = 4.0       # already looking at the chat: until I start typing (mid-conversation)
    reaction_sigma: float = 0.9          # heavy right tail: people glance away, switch apps
    drowsy_factor: float = 1.8
    focus_factor: float = 1.5
    read_cjk_per_s: float = 15.0
    read_words_per_s: float = 4.0
    type_cjk_mean: float = 4.0           # CJK chars/s (pinyin IME, phone); today's speed ~ Gamma(mean, cv 0.15)
    type_cjk_cv: float = 0.15
    type_words_per_s: float = 1.0
    type_other_per_s: float = 8.0
    jitter_sigma: float = 0.2            # per-message multiplicative noise
    gap_median_s: float = 0.7            # pause between my own consecutive messages
    gap_sigma: float = 0.6
    min_typing_frac: float = 0.3
    physical_cap_s: float = 600.0        # technical ceiling (a pathological text), not behaviour

    @classmethod
    def from_cfg(cls, cfg) -> "DelayParams":
        p = cls()
        for k in p.__dataclass_fields__:
            v = cfg.get(k) if cfg is not None else None
            if v is not None:
                setattr(p, k, float(v))
        return p


class DelayModel:
    """Human pace (docs/07 §4). Reaction ~ LogNormal(median x mode x energy x tie, sigma); typing at today's speed
    (drawn once a day, slower when tired) with per-message noise; gaps between my messages ~ LogNormal. Closer people
    get a slightly quicker reaction (priority). No behavioural caps: a long message takes long to type."""

    def __init__(self, params: Optional[DelayParams] = None, rng: Optional[random.Random] = None, rng_fn=None,
                 daily_fn=None):
        self.p = params or DelayParams()
        self._rng = rng
        self._rng_fn = rng_fn
        self._daily_fn = daily_fn          # (key, sampler) -> value stable within the local day
        self._speed_fallback: Optional[float] = None

    @property
    def rng(self) -> random.Random:
        if self._rng is not None:
            return self._rng
        if self._rng_fn is not None:
            return self._rng_fn()
        self._rng = random.Random()
        return self._rng

    def jitter(self) -> float:
        return math.exp(self.rng.gauss(0.0, self.p.jitter_sigma))

    def typing_speed(self, energy: float = 0.8) -> float:
        """CJK chars/s now: today's speed (a daily draw) x how awake I am."""
        def draw(r):
            shape = 1.0 / (self.p.type_cjk_cv ** 2)
            return r.gammavariate(shape, self.p.type_cjk_mean / shape)
        if self._daily_fn is not None:
            base = float(self._daily_fn("typing_cjk_per_s", draw))
        else:
            if self._speed_fallback is None:
                self._speed_fallback = draw(self.rng)
            base = self._speed_fallback
        return max(0.5, base * (0.75 + 0.25 * max(0.0, min(1.0, energy))))

    def reaction_time(self, mode: str = "active", energy: float = 0.8, closeness: float = 0.4) -> float:
        med = self.p.reaction_median_s
        mode = (mode or "").lower()
        if mode == "drowsy":
            med *= self.p.drowsy_factor
        elif mode in ("focus", "overwhelmed"):
            med *= self.p.focus_factor
        med *= 1.0 + 1.5 * max(0.0, 0.6 - max(0.0, energy))      # tired -> slower (linear below 0.6, no step)
        med *= 1.3 - 0.6 * max(0.0, min(1.0, closeness))          # priority follows the tie
        return math.exp(self.rng.gauss(math.log(max(0.05, med)), self.p.reaction_sigma))

    def read_time(self, text: str) -> float:
        return T.cjk_count(text) / self.p.read_cjk_per_s + T.latin_words(text) / self.p.read_words_per_s

    def typing_time(self, text: str, energy: float = 0.8) -> float:
        cjk = T.cjk_count(text)
        words = T.latin_words(text)
        other = len(_SYMBOL.findall(text or ""))
        return (cjk / self.typing_speed(energy) + words / self.p.type_words_per_s
                + other / self.p.type_other_per_s) * self.jitter()

    def first_delay(self, segment: str, incoming: str = "", *, mode: str = "active", energy: float = 0.8,
                    style_delay: float = 1.0, already_elapsed: float = 0.0, closeness: float = 0.4) -> float:
        typing = self.typing_time(segment, energy)
        base = self.reaction_time(mode, energy, closeness) + self.read_time(incoming) + typing
        base = max(base - max(0.0, already_elapsed), typing * self.p.min_typing_frac)
        return min(self.p.physical_cap_s, base * max(0.1, style_delay))

    def next_delay(self, segment: str, *, style_delay: float = 1.0, energy: float = 0.8) -> float:
        gap = math.exp(self.rng.gauss(math.log(self.p.gap_median_s), self.p.gap_sigma))
        typing = self.typing_time(segment, energy)
        return min(self.p.physical_cap_s, (gap + typing) * max(0.1, style_delay))
