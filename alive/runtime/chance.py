"""Human-like chance (docs/21 §4, ADR-0007): the stochastic primitives behaviour is built from.

Rule (P12 / INV-31, extended by ADR-0007): wherever a person would vary, behaviour is a draw from a
state-dependent distribution -- never a fixed number, a hard threshold, an argmax or a quota. Tendency comes from
temperament, today's texture and the relationship; variation from mood, energy, time of day and chance. The only
fixed numbers are safety / legal / technical limits (kill switch, budget, disclosure law, malfunction fuse,
timeouts, caches) and *physical* ceilings no person would reach anyway.

Conventions
-----------
* Draws come from the life texture's persisted RNG (``rng_of(kernel)``), so one life is reproducible from its seed
  and its history; callers (tests) may pass their own ``random.Random``.
* A former threshold ``x >= c`` becomes a smooth membership ``soft_step(x, c, w)`` used as a weight or as the
  probability of a Bernoulli draw -- never as a switch.
* A former fixed duration ``d`` becomes ``lognormal(rng, median=d * f(state), sigma)``: human timing is
  multiplicative and right-skewed.
* A former fixed count ``n`` becomes Poisson / geometric / negative-binomial around a state-dependent mean.
* A former argmax / top-k becomes ``softmax_choice`` / ``gumbel_topk`` with a temperature from state.
* Things that are stable within a day but differ between days (typing speed, bedtime, how much patience I have
  today) are ``daily(kernel, key, sampler)``: drawn once per local day and persisted in the texture.
"""
from __future__ import annotations

import math
import random
from typing import Callable, Hashable, Iterable, Optional, Sequence

_FALLBACK = random.Random(20261107)


def rng_of(kernel) -> random.Random:
    """The life's RNG (texture.rng), or a module fallback when there is no texture (minimal test kernels).
    ``kernel.chance_rng`` overrides both (dependency injection: tests, replays)."""
    r = getattr(kernel, "chance_rng", None) if kernel is not None else None
    if isinstance(r, random.Random):
        return r
    tex = getattr(kernel, "texture", None) if kernel is not None else None
    r = getattr(tex, "rng", None) if tex is not None else None
    return r if isinstance(r, random.Random) else _FALLBACK


# ---------------------------------------------------------------------------------------------- smooth functions
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float) -> float:
    p = clamp(p, 1e-6, 1 - 1e-6)
    return math.log(p / (1 - p))


def soft_step(x: float, center: float, width: float) -> float:
    """Smooth 0..1 membership of ``x`` above ``center`` (replaces ``x >= center``); ``width`` ~ the 27%..73% span/2."""
    return sigmoid((x - center) / max(1e-9, width))


def saturate(x: float, scale: float) -> float:
    """1 - exp(-x/scale): 0 at 0, ~0.63 at ``scale``, -> 1."""
    return 1.0 - math.exp(-max(0.0, x) / max(1e-9, scale))


def hazard_p(rate_per_h: float, dt_h: float) -> float:
    """Probability that an event with intensity ``rate_per_h`` happens within ``dt_h`` hours."""
    return 1.0 - math.exp(-max(0.0, rate_per_h) * max(0.0, dt_h))


# ---------------------------------------------------------------------------------------------- draws
def bernoulli(rng: random.Random, p: float) -> bool:
    return rng.random() < clamp(p)


def lognormal(rng: random.Random, median: float, sigma: float, lo: Optional[float] = None,
              hi: Optional[float] = None) -> float:
    """LogNormal with the given median (not mean). ``lo``/``hi`` are physical bounds, not behavioural caps."""
    v = math.exp(rng.gauss(math.log(max(1e-9, median)), max(0.0, sigma)))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def lognormal_mixture(rng: random.Random, components: Sequence[tuple]) -> tuple:
    """``components`` = [(weight, median, sigma, label?)...]. Returns (value, label_or_index)."""
    total = sum(max(0.0, c[0]) for c in components) or 1.0
    r = rng.random() * total
    for i, c in enumerate(components):
        r -= max(0.0, c[0])
        if r <= 0 or i == len(components) - 1:
            return lognormal(rng, c[1], c[2]), (c[3] if len(c) > 3 else i)
    return lognormal(rng, components[-1][1], components[-1][2]), len(components) - 1  # pragma: no cover


def jitter(rng: random.Random, value: float, cv: float) -> float:
    """``value`` times a median-1 lognormal factor with roughly coefficient of variation ``cv``."""
    if cv <= 0:
        return value
    return value * lognormal(rng, 1.0, math.sqrt(math.log(1 + cv * cv)))


def gamma_mean(rng: random.Random, mean: float, cv: float) -> float:
    """Gamma with the given mean and coefficient of variation (shape 1/cv^2)."""
    if mean <= 0:
        return 0.0
    if cv <= 0:
        return mean
    shape = 1.0 / (cv * cv)
    return rng.gammavariate(shape, mean / shape)


def beta_mean(rng: random.Random, mean: float, concentration: float) -> float:
    m = clamp(mean, 1e-4, 1 - 1e-4)
    c = max(1e-3, concentration)
    return rng.betavariate(m * c, (1 - m) * c)


def poisson(rng: random.Random, lam: float) -> int:
    lam = max(0.0, lam)
    if lam > 30:                                   # normal approximation for large means
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    # inverse transform with ONE uniform: exact, and a 'typical' RNG (MedianRandom, u = 0.5) gives the median
    u = rng.random()
    n, pk = 0, math.exp(-lam)
    cdf = pk
    while u > cdf and n < 1000:
        n += 1
        pk *= lam / n
        cdf += pk
    return n


def neg_binomial(rng: random.Random, mean: float, dispersion: float) -> int:
    """Over-dispersed count (Gamma-Poisson): variance = mean + mean^2 / dispersion. Human counts are bursty."""
    if mean <= 0:
        return 0
    lam = rng.gammavariate(max(1e-3, dispersion), mean / max(1e-3, dispersion))
    return poisson(rng, lam)


def geometric_extra(rng: random.Random, p_continue: float, cap: int = 50) -> int:
    """Number of additional items when each further one happens with probability ``p_continue``."""
    n = 0
    p = clamp(p_continue, 0.0, 0.98)
    while n < cap and rng.random() < p:
        n += 1
    return n


class HazardClock:
    """An event of a time-varying intensity, by time rescaling: draw a unit-exponential threshold E once, add
    rate x dt at every evaluation, and fire when the accumulated hazard reaches E (then draw a new E).
    Distributionally identical to an independent Bernoulli(1 - exp(-rate dt)) at every evaluation, but a 'typical'
    RNG (MedianRandom: E = ln 2) makes the event happen at its median time instead of never. Prefer this, or a
    once-sampled duration, over per-tick Bernoulli draws."""

    __slots__ = ("acc", "need")

    def __init__(self, acc: float = 0.0, need: Optional[float] = None):
        self.acc = float(acc)
        self.need = need

    def step(self, rng: random.Random, rate: float, dt: float) -> bool:
        if self.need is None:
            self.need = -math.log(max(1e-12, 1.0 - rng.random()))
        self.acc += max(0.0, rate) * max(0.0, dt)
        if self.acc >= self.need:
            self.acc, self.need = 0.0, None
            return True
        return False

    def to_dict(self) -> dict:
        return {"acc": round(self.acc, 6), "need": self.need}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "HazardClock":
        d = d or {}
        return cls(float(d.get("acc", 0.0) or 0.0), d.get("need"))


# ---------------------------------------------------------------------------------------------- choice
def softmax_weights(scores: Sequence[float], temperature: float) -> list:
    t = max(1e-6, temperature)
    if not scores:
        return []
    m = max(scores)
    ws = [math.exp((s - m) / t) for s in scores]
    z = sum(ws) or 1.0
    return [w / z for w in ws]


def softmax_choice(rng: random.Random, items: Sequence, scores: Sequence[float], temperature: float):
    """One item, chosen with probability ~ exp(score / T). T -> 0 approaches argmax; large T approaches uniform."""
    if not items:
        return None
    ws = softmax_weights(list(scores), temperature)
    r = rng.random()
    for it, w in zip(items, ws):
        r -= w
        if r <= 0:
            return it
    return items[-1]


def gumbel_topk(rng: random.Random, scores: dict, k: int, temperature: float) -> list:
    """Plackett-Luce sample of ``k`` keys without replacement (Gumbel-top-k), ordered. Replaces sorted(...)[:k]."""
    t = max(1e-6, temperature)
    keyed = []
    for key, s in scores.items():
        u = min(1 - 1e-12, max(1e-12, rng.random()))
        keyed.append((s / t - math.log(-math.log(u)), key))
    keyed.sort(key=lambda x: -x[0])
    return [key for _, key in keyed[:max(0, k)]]


def weighted_sample(rng: random.Random, items: Sequence, weights: Sequence[float]):
    total = sum(max(0.0, w) for w in weights)
    if not items or total <= 0:
        return None
    r = rng.random() * total
    for it, w in zip(items, weights):
        r -= max(0.0, w)
        if r <= 0:
            return it
    return items[-1]


# ---------------------------------------------------------------------------------------------- per-day draws
def daily(kernel, key: Hashable, sampler: Callable[[random.Random], float], ts: Optional[float] = None):
    """A value that is stable within a local day and re-drawn on the next (persisted with the texture when there
    is one). ``sampler(rng)`` produces the value."""
    tex = getattr(kernel, "texture", None) if kernel is not None else None
    try:
        day = kernel.clock.today(ts)
    except Exception:
        day = "?"
    store = getattr(tex, "daily", None) if tex is not None else None
    if not isinstance(store, dict):
        store = getattr(kernel, "_chance_daily", None)
        if store is None:
            store = {}
            try:
                setattr(kernel, "_chance_daily", store)
            except Exception:
                pass
    k = f"{day}|{key}"
    if k not in store:
        store[k] = sampler(rng_of(kernel))
        if len(store) > 400:                       # keep a few days
            for old in sorted(store)[: len(store) - 300]:
                store.pop(old, None)
    return store[k]


class MedianRandom(random.Random):
    """A 'typical person' RNG for tests of everything EXCEPT the randomness itself: uniform draws are 0.5, normal
    draws their mean, Gamma/Beta draws their mean. Every Bernoulli then goes the way of its more likely outcome and
    every duration is its median, so tests of other behaviour stay deterministic. Stochastic semantics are tested
    with real seeded RNGs."""

    def random(self) -> float:  # noqa: D401
        return 0.5

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        return mu

    def normalvariate(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        return mu

    def gammavariate(self, alpha: float, beta: float) -> float:
        return alpha * beta

    def betavariate(self, alpha: float, beta: float) -> float:
        return alpha / (alpha + beta)

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2.0


def mean_or(values: Iterable[float], default: float) -> float:
    vs = [float(v) for v in values if v is not None]
    return sum(vs) / len(vs) if vs else default
