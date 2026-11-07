"""Reply-latency survival: how much does a silence mean, for THIS person? (docs/05 §5.4, docs/07 §5, ADR-0007)

Priors: research_notes/拟人自主AI框架技术调研/air_reading_timing_priors.md §2 and §7(a).

How long a person takes to answer in IM is heavy-tailed and best described by a two-component lognormal mixture: a
fast component ("phone in hand, in the conversation") and a slow one ("away, busy") (Stouffer et al. 2006; Wu et al.
2010). Whether a silence means anything is judged against the person's OWN rhythm, not a fixed timeout (Kalman et al.
2006: at least 70 % of replies come within the replier's own mean latency, at most 4 % after ten times it).

``survival(t)`` is the probability that they would still not have answered after ``t`` if they were going to answer
the way they usually do. ``1 - survival(t)`` is the smooth evidence that my message is being passed over -- it grows
with time, faster for quick repliers, and never jumps at "24 h". Time while they are usually offline counts less
(``effective_elapsed``): availability gates the clock, the distribution does not need a long tail for the night.
"""
from __future__ import annotations

import math
import statistics
from typing import Callable, Optional, Sequence

# (closeness, pi_fast, fast median s, fast sigma, slow median s, slow sigma)   -- note §7(a), cold start
# core (1.5-5 layer): pi .5, 60 s (0.9), 20 min (1.3), median ~4-5 min
# good friends (15):  pi .4, 90 s (0.9), 40 min (1.4), median ~12 min
# ordinary (50):      pi .25, 2 min (0.9), 2 h (1.5), median ~1 h
# weak ties (150+):   pi .15, 3 min (0.9), 5 h (1.6), median ~3 h
_COLD = [(0.10, 0.15, 180.0, 0.9, 18000.0, 1.6),
         (0.30, 0.25, 120.0, 0.9, 7200.0, 1.5),
         (0.55, 0.40, 90.0, 0.9, 2400.0, 1.4),
         (0.90, 0.50, 60.0, 0.9, 1200.0, 1.3)]
# within a conversation (an answer to my line while we are talking). Core and good friends from note §7(a); the
# two outer layers are extrapolated toward their cold-start rows [inferred, low confidence].
_SESSION = [(0.10, 0.35, 120.0, 0.9, 7200.0, 1.5),
            (0.30, 0.50, 60.0, 0.9, 2400.0, 1.4),
            (0.55, 0.75, 45.0, 0.9, 720.0, 1.4),
            (0.90, 0.80, 30.0, 0.8, 600.0, 1.2)]
# evidence weight of the person's own observed latencies: n / (n + N0). N0 = 5 answers before their own rhythm
# counts as much as the population prior (a handful of replies already says a lot about someone's pace).
N0 = 5.0

# How much an unanswered initiative of each kind says about how my reaching out is received (growth evidence weight,
# docs/05 §5.4). Letting a shared link or a "good morning" slide is ordinary and says little; leaving a promise kept,
# a worried check-in or a confidence unanswered says more [inferred].
EVIDENCE_STAKES = {"promise": 1.0, "care": 0.9, "confide": 0.9, "continue": 0.7, "general": 0.6, "share": 0.4,
                   "greet": 0.3}


def _interp(table: list, c: float) -> tuple:
    c = min(max(c, table[0][0]), table[-1][0])
    for lo, hi in zip(table, table[1:]):
        if c <= hi[0]:
            a = (c - lo[0]) / (hi[0] - lo[0])
            pi = lo[1] + a * (hi[1] - lo[1])
            mf = math.exp(math.log(lo[2]) + a * (math.log(hi[2]) - math.log(lo[2])))
            sf = lo[3] + a * (hi[3] - lo[3])
            ms = math.exp(math.log(lo[4]) + a * (math.log(hi[4]) - math.log(lo[4])))
            ss = lo[5] + a * (hi[5] - lo[5])
            return pi, mf, sf, ms, ss
    t = table[-1]
    return t[1], t[2], t[3], t[4], t[5]


def _ln_survival(t: float, median: float, sigma: float) -> float:
    if t <= 0:
        return 1.0
    z = (math.log(t) - math.log(max(1e-9, median))) / (max(1e-6, sigma) * math.sqrt(2.0))
    return 0.5 * math.erfc(z)


def _mixture_median(pi: float, mf: float, sf: float, ms: float, ss: float) -> float:
    lo, hi = math.log(1.0), math.log(30 * 86400.0)
    for _ in range(60):                                    # bisection on log time
        mid = 0.5 * (lo + hi)
        t = math.exp(mid)
        s = pi * _ln_survival(t, mf, sf) + (1 - pi) * _ln_survival(t, ms, ss)
        lo, hi = (mid, hi) if s > 0.5 else (lo, mid)
    return math.exp(0.5 * (lo + hi))


def components(closeness: float, latencies: Sequence[float] = (), mode: str = "cold") -> list:
    """[(weight, median_s, sigma)] for this person. The population prior for their closeness is scaled by how quick
    they are in conversation compared with people at that closeness (geometric, shrunk by evidence): someone twice as
    quick as usual mid-conversation is probably also quicker to pick up a cold message."""
    c = min(1.0, max(0.0, float(closeness or 0.0)))
    pi, mf, sf, ms, ss = _interp(_COLD if mode == "cold" else _SESSION, c)
    obs = [float(x) for x in latencies if x and x > 0]
    if obs:
        ref = _mixture_median(*_interp(_SESSION, c))
        w = len(obs) / (len(obs) + N0)
        rho = (statistics.median(obs) / ref) ** w
        rho = min(5.0, max(0.2, rho))                       # numerical robustness against a few odd samples
        mf, ms = mf * rho, ms * rho
    return [(pi, mf, sf), (1 - pi, ms, ss)]


def survival(t: float, comps: Sequence[tuple]) -> float:
    """P(no answer yet after t seconds | they answer as they usually do)."""
    return sum(w * _ln_survival(t, m, s) for w, m, s in comps)


def hazard(t: float, comps: Sequence[tuple], dt: float = 60.0) -> float:
    """Per-second hazard of an answer at t (numerical, for diagnostics)."""
    s0, s1 = survival(t, comps), survival(t + dt, comps)
    return max(0.0, (s0 - s1) / max(1e-12, s0) / dt)


def effective_elapsed(start: float, end: float, active_at: Optional[Callable[[float], float]] = None,
                      step: float = 900.0) -> float:
    """Elapsed seconds weighted by how likely they are around at each moment (relative to their average):
    hours they usually sleep through count little. ``active_at(ts) -> 0..1``; None = uniform."""
    if end <= start:
        return 0.0
    if active_at is None:
        return end - start
    # their average availability over a day is the reference: a typical day counts as a day
    try:
        ref = sum(active_at(start + i * 3600.0) for i in range(24)) / 24.0
    except Exception:
        return end - start
    if ref <= 1e-6:
        return end - start
    total, t = 0.0, start
    span = min(end - start, 60 * 86400.0)                 # technical bound on the loop (two months)
    stop = start + span
    step = max(step, span / 400.0)                         # technical: at most ~400 evaluations
    while t < stop:
        d = min(step, stop - t)
        total += d * min(1.5, active_at(t + d / 2) / ref)
        t += d
    return total + (end - stop)


def reply_survival(elapsed_s: float, closeness: float, latencies: Sequence[float] = (), mode: str = "cold") -> float:
    return survival(max(0.0, elapsed_s), components(closeness, latencies, mode))
