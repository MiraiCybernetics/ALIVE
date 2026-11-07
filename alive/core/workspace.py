"""Global workspace and salience arbitration (docs/02 §2–3).

A bounded set of slots (default 7, 4–9 depending on energy) holds what the individual currently
"has in mind". Candidates compete by salience; replacing an item requires beating the weakest slot
by a hysteresis margin. One item at a time is the *spotlight* (being acted upon), which protects a
single coherent self. Workspace contents are rendered into every deliberate prompt (main item in
full, background items as one-liners) so that replying to A can naturally mention B.

Chance (ADR-0007): the spotlight is a softmax draw over the items plus a "nothing in particular" option (the
old threshold is now that option's score), with a temperature from arousal and focus; the current spotlight keeps
an inertia bonus. Items have no fixed TTL: each one's time on my mind is drawn when it enters (Weibull) around a
median of its kind's typical ``ttl`` scaled by salience, today's focus and how open (unanswered, unsaid) it is.
That decides whether I ever come back to something I let pass.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from alive.runtime import chance

# Persistence priors. Each item's lifetime is drawn ONCE when it (re-)enters (a time-to-event draw, equivalent to a
# hazard; under the test harness's median RNG it is exactly the median):
#   median = ttl x sqrt(salience / SAL_REF) x focus x (1 + OPEN_GAIN x open)
# SAL_REF 0.35 (the old focus threshold) and the square root keep the medians of typical items within ~+-25% of the
#   old fixed TTLs (observed 2400 s, thread 900 s, urge 1800 s, intention 7200 s, cue 900 s); more salient -> longer
#   (hazard ~ 1/sqrt(salience)).
# WEIBULL_K 1.5: the lifetime distribution (rising hazard: a fresh item rarely vanishes at once; P(gone within 10%
#   of its median) ~ 4%, P(> 2x median) ~ 14%).
# OPEN_GAIN 1.0: something I left unanswered or unsaid stays on my mind up to 2x longer (Zeigarnik effect).
# DEFAULT_TEMPERATURE 0.05: an item 0.15 above the "nothing in particular" option wins ~95% against it, one 0.15
#   below ~5% - weak items now come up now and then instead of never.
SAL_REF = 0.35
WEIBULL_K = 1.5
OPEN_GAIN = 1.0
DEFAULT_TEMPERATURE = 0.05
_NONE = "\x00none"


@dataclass
class WorkspaceItem:
    key: str                 # unique: "urge:social", "intent:<id>", "cue:<mem>", "thought:<id>"
    kind: str                # urge | intention | memory_cue | thought | thread | space
    text: str
    salience: float
    entered_at: float = 0.0
    ttl: float = 3600.0      # typical persistence (median, virtual seconds) at reference salience - not a deadline
    ref: Any = None
    sticky: bool = False     # never drops out while referenced (e.g. active long task)
    open: float = 0.0        # 0..1: left unanswered / unsaid -> stays on my mind longer
    checked_at: float = 0.0  # last time it was looked at by ``expire`` (waiting items fade per minute)
    life_q: float = 1.0      # this item's drawn lifetime as a multiple of the median (Weibull quantile)

    def median_life(self, focus: float = 1.0) -> float:
        """ttl x sqrt(salience / SAL_REF) x focus x (1 + OPEN_GAIN x open), in seconds."""
        return (max(1.0, self.ttl) * math.sqrt(max(0.02, self.salience) / SAL_REF) * max(0.2, focus)
                * (1.0 + OPEN_GAIN * max(0.0, min(1.0, self.open))))

    def gone(self, now: float, focus: float = 1.0) -> bool:
        """Has it dropped out of mind? Its lifetime was drawn when it (re-)entered; the median follows its current
        salience, today's focus and how open it is."""
        return not self.sticky and now - self.entered_at > self.median_life(focus) * self.life_q

    def drop_probability(self, now: float, focus: float = 1.0) -> float:
        """P(dropped out by ``now``) for an item that entered at ``entered_at`` (Weibull CDF; analytic helper)."""
        if self.sticky:
            return 0.0
        lam = self.median_life(focus) / math.log(2.0) ** (1.0 / WEIBULL_K)
        return 1.0 - math.exp(-(max(0.0, now - self.entered_at) / lam) ** WEIBULL_K)


def life_quantile(u: float) -> float:
    """Weibull(k) quantile relative to its median: u = 0.5 -> 1.0."""
    u = min(1.0 - 1e-9, max(1e-9, float(u)))
    return (-math.log(1.0 - u) / math.log(2.0)) ** (1.0 / WEIBULL_K)


@dataclass
class Workspace:
    capacity: int = 7
    hysteresis: float = 0.05
    inertia: float = 0.3
    items: dict = field(default_factory=dict)
    spotlight: Optional[str] = None
    waiting: dict = field(default_factory=dict)
    focus: float = 1.0                                    # today's focus (texture multiplier), set by the core
    rng_fn: Optional[Callable[[], random.Random]] = None  # the life's RNG (set by the core)

    def _rng(self) -> random.Random:
        return self.rng_fn() if self.rng_fn is not None else chance.rng_of(None)

    def set_capacity_from_energy(self, energy: float) -> None:
        self.capacity = max(4, min(9, int(round(4 + 5 * max(0.0, min(1.0, energy))))))

    def load(self) -> float:
        return len(self.items) / max(1, self.capacity)

    def overflow(self) -> int:
        return len(self.waiting)

    def offer(self, item: WorkspaceItem, now: float) -> bool:
        """Admit or refresh a candidate. Returns True if it is now in the workspace."""
        item.entered_at = item.entered_at or now
        item.checked_at = now
        item.life_q = life_quantile(self._rng().random())       # how long this one will stay with me
        if item.key in self.items:
            cur = self.items[item.key]
            cur.salience = max(cur.salience * 0.7 + item.salience * 0.3, item.salience)
            cur.text, cur.ref = item.text, item.ref if item.ref is not None else cur.ref
            cur.open = max(cur.open, item.open)
            cur.entered_at = cur.checked_at = now
            cur.life_q = item.life_q                            # refreshed: a new life from now
            return True
        if len(self.items) < self.capacity:
            self.items[item.key] = item
            self.waiting.pop(item.key, None)
            return True
        weakest_key = min((k for k in self.items if k != self.spotlight), key=lambda k: self.items[k].salience,
                          default=None)
        if weakest_key is not None and item.salience > self.items[weakest_key].salience + self.hysteresis:
            evicted = self.items.pop(weakest_key)
            self.waiting[evicted.key] = evicted
            self.items[item.key] = item
            self.waiting.pop(item.key, None)
            return True
        self.waiting[item.key] = item
        if len(self.waiting) > 50:
            for k in sorted(self.waiting, key=lambda k: self.waiting[k].salience)[:10]:
                self.waiting.pop(k, None)
        return False

    def expire(self, now: float, decay_per_min: float = 0.01) -> list:
        """Things drop out of mind (drawn lifetimes, see module doc); waiting items also fade by ``decay_per_min``."""
        gone = []
        for k, it in list(self.items.items()):
            it.checked_at = now
            if it.gone(now, self.focus):
                gone.append(self.items.pop(k))
                if self.spotlight == k:
                    self.spotlight = None
        for k, it in list(self.waiting.items()):
            minutes = max(0.0, now - (it.checked_at or now)) / 60.0
            it.salience -= decay_per_min * minutes
            it.checked_at = now
            if it.salience <= 0 or it.gone(now, self.focus):
                self.waiting.pop(k, None)
        # refill from waiting: the most salient comes back to mind first
        while len(self.items) < self.capacity and self.waiting:
            k = max(self.waiting, key=lambda k: self.waiting[k].salience)
            self.items[k] = self.waiting.pop(k)
        return gone

    def remove(self, key: str) -> None:
        self.items.pop(key, None)
        self.waiting.pop(key, None)
        if self.spotlight == key:
            self.spotlight = None

    def focus_weights(self, threshold: float = 0.3, temperature: float = DEFAULT_TEMPERATURE) -> dict:
        """Probability of each key (and None = nothing in particular) winning the spotlight draw."""
        keys, scores = self._focus_scores(threshold)
        ws = chance.softmax_weights(scores, temperature)
        return {(None if k == _NONE else k): w for k, w in zip(keys, ws)}

    def _focus_scores(self, threshold: float) -> tuple:
        cands = []
        for key, it in self.items.items():
            s = float(it.salience)
            if key == self.spotlight:
                s *= 1.0 + self.inertia          # what I am already on keeps an edge
            cands.append((s, key))
        cands.append((float(threshold), _NONE))   # "nothing in particular": the former threshold
        cands.sort(key=lambda x: (-x[0], x[1]))   # most likely first (median draws pick the mode)
        return [k for _, k in cands], [s for s, _ in cands]

    def select_focus(self, threshold: float = 0.3, temperature: Optional[float] = None,
                     rng: Optional[random.Random] = None) -> Optional[WorkspaceItem]:
        """Draw the spotlight: softmax over items (salience; the current one with an inertia bonus) and the
        "nothing in particular" option scored ``threshold``. ``temperature`` comes from arousal and focus."""
        if not self.items:
            self.spotlight = None
            return None
        keys, scores = self._focus_scores(threshold)
        t = DEFAULT_TEMPERATURE if temperature is None else max(1e-3, float(temperature))
        pick = chance.softmax_choice(rng if rng is not None else self._rng(), keys, scores, t)
        if pick is None or pick == _NONE:
            self.spotlight = None                 # my attention went to nothing in particular
            return None
        self.spotlight = pick
        return self.items[pick]

    def render(self, max_items: int = 5, exclude: Optional[str] = None) -> str:
        its = sorted((i for i in self.items.values() if i.key != exclude), key=lambda i: -i.salience)[:max_items]
        if not its:
            return "（没什么特别挂心的）"
        out = []
        for n, it in enumerate(its):
            t = it.text if n == 0 else it.text[:40]
            out.append(f"- {t}")
        return "\n".join(out)

    def texts(self) -> list:
        return [i.text for i in self.items.values()]

    def snapshot(self) -> dict:
        return {"capacity": self.capacity, "spotlight": self.spotlight, "waiting": len(self.waiting),
                "items": [{"key": i.key, "kind": i.kind, "text": i.text[:60], "salience": round(i.salience, 3)}
                          for i in sorted(self.items.values(), key=lambda i: -i.salience)]}
