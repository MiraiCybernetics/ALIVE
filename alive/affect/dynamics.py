"""Pure affect math (docs/04 §2-§3): ALMA default mood, PAD octants, second-order mood dynamics,
two-process sleep model, the habitual sleep window and the stochastic night (bedtime, wake time, sleep depth,
wake-up probability). No I/O, no kernel access: unit-testable.

Units: time inside the dynamics is in HOURS; callers convert from virtual seconds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Dict, Optional, Sequence, Tuple

from alive.affect.tables import OCTANTS

Vec = Tuple[float, float, float]
SQRT3 = math.sqrt(3.0)

# ALMA (Gebhard 2005) personality -> default mood coefficients, rows P, A, D over (O, C, E, A, N)
ALMA_COEFFS: Dict[str, Dict[str, float]] = {
    "P": {"E": 0.21, "A": 0.59, "N": 0.19},
    "A": {"O": 0.15, "A": 0.30, "N": -0.57},
    "D": {"O": 0.25, "C": 0.17, "E": 0.60, "A": -0.32},
}


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(c * c for c in v))


def alma_default_mood(big5: Dict[str, float], *, scale: str = "centered",
                      coeffs: Optional[Dict[str, Dict[str, float]]] = None) -> Vec:
    """Default (baseline) mood from Big Five via the ALMA linear mapping.

    scale="centered" (docs/04 §2.1, the framework default): inputs in 0..1 are mapped to -1..1 (2x-1) first.
    scale="raw": inputs are used as given (the ALMA paper's worked example uses this convention:
    O=.4 C=.8 E=.6 A=.3 N=.4 -> P=.38 A=-.08 D=.50, "slightly relaxed").
    N enters with the sign printed in ALMA (docs/04 §2.1 says N is used directly).
    """
    co = coeffs or ALMA_COEFFS
    vals = {}
    for k in ("O", "C", "E", "A", "N"):
        x = float(big5.get(k, 0.5))
        vals[k] = (2.0 * x - 1.0) if scale == "centered" else x
    out = []
    for axis in ("P", "A", "D"):
        out.append(clamp(sum(w * vals.get(k, 0.0) for k, w in co.get(axis, {}).items())))
    return (out[0], out[1], out[2])


# --------------------------------------------------------------------------- octants & intensity bands
BAND_NAMES = ("slightly", "moderately", "fully")
DEFAULT_BANDS = (2.0 / 3.0, 4.0 / 3.0)   # norm thresholds (thirds of each axis' range 2.0), see ADR note
NEUTRAL_NORM = 0.12


def octant_of(v: Sequence[float]) -> str:
    key = tuple(1 if c >= 0 else -1 for c in v[:3])
    return OCTANTS[key]  # type: ignore[index]


def mood_octant(v: Sequence[float], bands: Sequence[float] = DEFAULT_BANDS,
                neutral: float = NEUTRAL_NORM) -> Tuple[str, str, float]:
    """-> (octant name | "neutral", band name | "", norm). Band = intensity of the mood vector (ALMA)."""
    n = norm(v)
    if n < neutral:
        return "neutral", "", n
    band = BAND_NAMES[0] if n < bands[0] else BAND_NAMES[1] if n < bands[1] else BAND_NAMES[2]
    return octant_of(v), band, n


# --------------------------------------------------------------------------- mood dynamics
@dataclass
class MoodParams:
    half_life_h: float = 24.0        # regression half-life to default mood (12-48 h via neuroticism)
    k_emotion: float = 0.8           # pull stiffness per unit emotion strength (1/h^2)
    k_push: float = 0.05             # push stiffness (deepening a mood supported by emotions)
    k_bored: float = 0.015           # boredom pull toward a low-arousal, slightly negative state
    damping_ratio: float = 0.9       # < 1: slight overshoot -> hysteresis; adaptive to current stiffness
    v_max: float = 2.0               # |velocity| cap (PAD units per hour)
    lockin_boost: float = 4.0        # regression stiffness multiplier during anti-lock-in

    @property
    def k0(self) -> float:
        # critically damped spring: (1 + w t) e^{-w t} = 0.5 at w t ~= 1.678
        w = 1.678 / max(0.5, self.half_life_h)
        return w * w


BORED_TARGET = (-0.2, -0.5)  # (P, A) target of the boredom force (WASABI: boredom lowers arousal)


def regression_half_life(neuroticism: float, lo: float = 12.0, hi: float = 48.0) -> float:
    """Higher neuroticism -> slower regression to baseline (docs/04 §3.2)."""
    return lo + (hi - lo) * clamp(neuroticism, 0.0, 1.0)


def _same_octant(a: Sequence[float], b: Sequence[float]) -> bool:
    return all((x >= 0) == (y >= 0) for x, y in zip(a, b))


def mood_step(x: Vec, v: Vec, default: Vec, center: Optional[Vec], strength: float, dt_h: float,
              p: MoodParams, *, boredom: float = 0.0, anti_lockin: bool = False) -> Tuple[Vec, Vec, str]:
    """One semi-implicit Euler step of the damped second-order mood system (ALMA pull/push + WASABI inertia).

    force = k_e*s*(center - x)          [pull phase: mood between origin and center or elsewhere]
          | k_push*s*unit(x)*headroom   [push phase: mood beyond the center in the same octant]
          + k0*(default - x)            [regression]
          + k_b*boredom*(bored - x)     [P, A only]
    v <- v*exp(-c dt) + force*dt ; x <- clamp(x + v dt), with c = 2*zeta*sqrt(k_total) (adaptive damping).
    Returns (x, v, phase) where phase is "pull" | "push" | "none".
    """
    k0 = p.k0 * (p.lockin_boost if anti_lockin else 1.0)
    force = [k0 * (d - xi) for d, xi in zip(default, x)]
    k_tot = k0
    phase = "none"
    s = clamp(strength, 0.0, 1.0)
    if center is not None and s > 0:
        nx, nc = norm(x), norm(center)
        if nx > 1e-6 and _same_octant(x, center) and nx >= nc:
            phase = "push"
            if not (anti_lockin and center[0] < 0):
                head = max(0.0, 1.0 - nx / SQRT3)
                for i in range(3):
                    force[i] += p.k_push * s * (x[i] / nx) * head
        else:
            phase = "pull"
            ke = p.k_emotion * s
            k_tot += ke
            for i in range(3):
                force[i] += ke * (center[i] - x[i])
    b = clamp(boredom, 0.0, 1.0)
    if b > 0 and p.k_bored > 0:
        kb = p.k_bored * b
        k_tot += kb
        force[0] += kb * (BORED_TARGET[0] - x[0])
        force[1] += kb * (BORED_TARGET[1] - x[1])
    c = 2.0 * p.damping_ratio * math.sqrt(max(k_tot, 1e-9))
    decay = math.exp(-c * dt_h)
    nv = [vi * decay + fi * dt_h for vi, fi in zip(v, force)]
    vn = norm(nv)
    if vn > p.v_max:
        nv = [c_ * p.v_max / vn for c_ in nv]
    nx_ = []
    for i in range(3):
        xi = x[i] + nv[i] * dt_h
        if xi > 1.0 or xi < -1.0:
            xi = clamp(xi)
            nv[i] = 0.0
        nx_.append(xi)
    return (nx_[0], nx_[1], nx_[2]), (nv[0], nv[1], nv[2]), phase


def max_stable_dt_h(p: MoodParams, strength: float = 1.0) -> float:
    """Upper bound for a substep that keeps semi-implicit Euler well inside its stability region."""
    w = math.sqrt(p.k0 * p.lockin_boost + p.k_emotion * max(strength, 0.0) + p.k_bored)
    return min(5.0 / 60.0, 0.25 / max(w, 1e-6))


# --------------------------------------------------------------------------- physiology
@dataclass
class SleepParams:
    r_wake: float = 0.052      # per hour: sp -> 1 while awake  (~0.6 after 16 h from 0.08)
    r_sleep: float = 0.25      # per hour: sp -> 0 while asleep (~0.6 -> 0.08 over 8 h)
    sp_wake: float = 0.08      # typical sleep pressure right after a full night (initialization)
    circ_weight: float = 0.3
    load_weight: float = 0.2
    stress_weight: float = 0.2


def sleep_pressure_step(sp: float, asleep: bool, dt_h: float, p: SleepParams) -> float:
    """Exact solution of the two-process model's homeostatic process over dt (stable for any dt)."""
    if asleep:
        return clamp(sp * math.exp(-p.r_sleep * dt_h), 0.0, 1.0)
    return clamp(1.0 - (1.0 - sp) * math.exp(-p.r_wake * dt_h), 0.0, 1.0)


def circadian_value(hour_of_day: float, peak_hour: float) -> float:
    """Process C. docs/04 writes sin(2*pi*(t - t_peak)/24h); a sine peaks 6 h after t_peak, so the
    cosine is used to put the alertness maximum exactly at the configured peak hour."""
    return math.cos(2.0 * math.pi * (hour_of_day - peak_hour) / 24.0)


def energy_value(sp: float, circ: float, load: float, stress: float, p: SleepParams) -> float:
    return clamp(1.0 - sp + p.circ_weight * circ - p.load_weight * load - p.stress_weight * stress, 0.0, 1.0)


# --------------------------------------------------------------------------- rhythm / sleep window
def parse_hhmm(s: object, default: float) -> float:
    try:
        hh, mm = str(s).strip().split(":")[:2]
        return (int(hh) % 24) + int(mm) / 60.0
    except Exception:
        return default


class SleepRhythm:
    """The *habitual* sleep window from rhythm.yaml (wake, sleep, weekend_shift_hours, circadian_peak_hour).

    These are the prior means of the stochastic nights (``NightParams`` / ``bedtime_ts`` / ``wake_ts`` below; the
    affect engine draws one night at a time): the window computed here is what a night looks like with every draw
    at its mean and no state influence. Night of date D starts at D+sleep (if the sleep time is before noon it is
    taken as after midnight, i.e. D+1) and ends at the next wake time. Friday/Saturday nights start later and
    Saturday/Sunday mornings end later by the mean weekend shift.
    """

    def __init__(self, tz: tzinfo, *, wake: str = "07:40", sleep: str = "23:40",
                 weekend_shift_h: float = 0.0, peak_h: float = 15.5):
        self.tz = tz
        self.wake_h = parse_hhmm(wake, 7.5)
        self.sleep_h = parse_hhmm(sleep, 23.5)
        self.weekend_shift_h = float(weekend_shift_h or 0.0)
        self.peak_h = float(peak_h)

    @classmethod
    def from_cfg(cls, tz: tzinfo, rhythm: dict) -> "SleepRhythm":
        return cls(tz, wake=rhythm.get("wake", "07:40"), sleep=rhythm.get("sleep", "23:40"),
                   weekend_shift_h=float(rhythm.get("weekend_shift_hours", 0.0) or 0.0),
                   peak_h=float(rhythm.get("circadian_peak_hour", 15.5)))

    def _at(self, d: date, hours: float) -> datetime:
        base = datetime(d.year, d.month, d.day, tzinfo=self.tz)
        return base + timedelta(hours=hours)

    # habitual anchors of one night (no weekend shift: that is a per-night draw)
    @staticmethod
    def is_weekend_night(d: date) -> bool:
        """Friday and Saturday nights (the next morning is a weekend morning)."""
        return d.weekday() in (4, 5)

    def habitual_start(self, d: date) -> float:
        start_day = d + timedelta(days=1) if self.sleep_h < 12.0 else d
        return self._at(start_day, self.sleep_h).timestamp()

    def habitual_wake(self, d: date) -> float:
        end = self._at(d + timedelta(days=1), self.wake_h).timestamp()
        start = self.habitual_start(d)
        return end if end > start else end + 86400.0

    def habitual_length_h(self) -> float:
        return ((self.wake_h - self.sleep_h) % 24.0) or 8.0

    def night_of(self, ts: float) -> date:
        """The night a moment belongs to: from one habitual wake time to the next it is the coming night's day."""
        return datetime.fromtimestamp(ts - self.wake_h * 3600.0, self.tz).date()

    def noon(self, d: date) -> float:
        return self._at(d, 12.0).timestamp()

    def window(self, d: date) -> Tuple[float, float]:
        """(start_ts, end_ts) of the night that begins on local date ``d``."""
        start_day = d + timedelta(days=1) if self.sleep_h < 12.0 else d
        start = self._at(start_day, self.sleep_h)
        if d.weekday() in (4, 5):  # Fri, Sat nights
            start += timedelta(hours=self.weekend_shift_h)
        end_day = d + timedelta(days=1)
        end = self._at(end_day, self.wake_h)
        if end_day.weekday() in (5, 6):  # Sat, Sun mornings
            end += timedelta(hours=self.weekend_shift_h)
        if end <= start:
            end += timedelta(days=1)
        return start.timestamp(), end.timestamp()

    def window_at(self, ts: float) -> Optional[Tuple[float, float]]:
        d = datetime.fromtimestamp(ts, self.tz).date()
        for dd in (d - timedelta(days=1), d):
            s, e = self.window(dd)
            if s <= ts < e:
                return s, e
        return None

    def is_sleep_time(self, ts: float) -> bool:
        return self.window_at(ts) is not None

    def next_window(self, ts: float) -> Tuple[float, float]:
        """The current window if inside one, else the next one."""
        w = self.window_at(ts)
        if w is not None:
            return w
        d = datetime.fromtimestamp(ts, self.tz).date()
        for k in range(0, 3):
            s, e = self.window(d + timedelta(days=k))
            if s > ts:
                return s, e
        return self.window(d + timedelta(days=1))  # pragma: no cover

    def last_wake(self, ts: float) -> float:
        """Most recent wake time (end of a window) <= ts."""
        d = datetime.fromtimestamp(ts, self.tz).date()
        best = None
        for k in range(-2, 1):
            _, e = self.window(d + timedelta(days=k))
            if e <= ts and (best is None or e > best):
                best = e
        return best if best is not None else ts - 8 * 3600

    def hour_of_day(self, ts: float) -> float:
        dt = datetime.fromtimestamp(ts, self.tz)
        return dt.hour + dt.minute / 60.0 + dt.second / 3600.0

    def circadian(self, ts: float) -> float:
        return circadian_value(self.hour_of_day(ts), self.peak_h)


# --------------------------------------------------------------------------- stochastic nights (ADR-0007)
@dataclass
class NightParams:
    """Priors of one night (docs/04 §3.3). The habitual times in rhythm.yaml are the means; everything a person
    varies from night to night is a draw (alive/runtime/chance.daily: one draw per night, persisted).

    bed_sd_h 35 min: night-to-night SD of sleep onset in young adults is ~30-45 min (actigraphy studies).
    a_sp 0.5: half of a homeostatic deviation shows up in the bedtime (slept in by 2 h -> to bed ~1 h later);
        habit and the circadian clock carry the rest.
    b_arousal_h 1.5 h per unit of arousal above my baseline: excited or upset (+0.4) -> ~35 min later; calmer
        than usual -> a little earlier.
    shift_lo_h / shift_hi_h -2.5 / +4 h: physical range of the state-driven shift (nobody "falls asleep" at 18:00
        because of a calm evening; an all-nighter is outside this model).
    weekend_sd_h 0.3 h around rhythm.yaml's weekend_shift_hours (mean, 0.75 h): Fri/Sat nights later, mornings later.
    theta_sigma 0.25: lognormal noise of the sleep pressure at which I wake. Alone it moves the homeostatic wake
        by ~1 h SD; the circadian gate halves that -> wake time SD ~30 min.
    circ_gate_per_h 0.25 (= r_sleep): the morning circadian drive (habit, light, alarm) raises the wake threshold
        as fast as homeostasis discharges, so both weigh equally in when I wake.
    snooze median 8 min, sigma 0.8: lying in after waking (right-skewed). The anchor is shifted by the median so
        the median get-up time stays the habitual one.
    min_sleep_h 1.5: physical floor of a night.
    onset_min 15: the first minutes after falling asleep are light sleep (N1).
    wake_base / wake_span / wake_width: the arousal threshold a notification must cross, 0.30 in the lightest
        sleep (just before waking) to 0.90 in the deepest (early night); logistic width 0.10.
    """
    bed_sd_h: float = 35.0 / 60.0
    a_sp: float = 0.5
    b_arousal_h: float = 1.5
    shift_lo_h: float = -2.5
    shift_hi_h: float = 4.0
    weekend_sd_h: float = 0.3
    theta_sigma: float = 0.25
    circ_gate_per_h: float = 0.25
    snooze_median_h: float = 8.0 / 60.0
    snooze_sigma: float = 0.8
    min_sleep_h: float = 1.5
    onset_min: float = 15.0
    wake_base: float = 0.30
    wake_span: float = 0.60
    wake_width: float = 0.10

    @classmethod
    def from_cfg(cls, cfg: Optional[dict]) -> "NightParams":
        p = cls()
        for k, v in (cfg or {}).items():
            if k in cls.__dataclass_fields__ and v is not None:
                setattr(p, k, float(v))
        return p


def typical_bed_pressure(p: SleepParams, night_len_h: float) -> float:
    """Sleep pressure at the habitual bedtime after a habitual day (awake 24 h - night length from sp_wake)."""
    return 1.0 - (1.0 - p.sp_wake) * math.exp(-p.r_wake * max(1.0, 24.0 - night_len_h))


def typical_wake_threshold(p: SleepParams, night_len_h: float) -> float:
    """The pressure a habitual night discharges down to (~sp_wake): the median wake threshold."""
    return typical_bed_pressure(p, night_len_h) * math.exp(-p.r_sleep * night_len_h)


def homeostatic_offset_h(sp_now: float, now_ts: float, habitual_start_ts: float, sp_target: float,
                         p: SleepParams) -> float:
    """How much later (+) or earlier (-) than the habitual bedtime my sleep pressure, staying awake from now, reaches
    its usual bedtime level. Slept in or napped -> positive; short night -> negative."""
    sp_now = clamp(sp_now, 0.0, 0.999)
    sp_target = clamp(sp_target, 0.01, 0.999)
    t_cross = now_ts + 3600.0 * math.log((1.0 - sp_now) / (1.0 - sp_target)) / max(1e-6, p.r_wake)
    return (t_cross - habitual_start_ts) / 3600.0


def bedtime_ts(habitual_start_ts: float, weekend_h: float, z_bed: float, offset_sp_h: float, arousal_dev: float,
               np_: NightParams) -> float:
    """Tonight's sleep onset ~ N(habitual + weekend + a*(homeostatic offset) + b*arousal, bed_sd)."""
    shift = np_.a_sp * offset_sp_h + np_.b_arousal_h * arousal_dev
    shift = clamp(shift, np_.shift_lo_h, np_.shift_hi_h)
    return habitual_start_ts + 3600.0 * (weekend_h + shift + np_.bed_sd_h * z_bed)


def wake_ts(start_ts: float, sp_bed: float, anchor_ts: float, theta: float, snooze_h: float, p: SleepParams,
            np_: NightParams) -> float:
    """When I get up: sleep pressure falls as sp_bed*exp(-r t); the wake threshold theta is raised by the morning
    circadian drive as exp(c (t - anchor)). They cross at the r/(r+c)-weighted mean of the homeostatic wake time
    and the habitual (anchor) time; then a lognormal snooze. Closed form, stable for any night."""
    r, c = max(1e-6, p.r_sleep), max(0.0, np_.circ_gate_per_h)
    theta = max(1e-4, theta)
    t_h = start_ts + 3600.0 * max(0.0, math.log(max(sp_bed, theta) / theta)) / r
    t = (r * t_h + c * anchor_ts) / (r + c) - 3600.0 * np_.snooze_median_h
    t = max(t, start_ts + 3600.0 * np_.min_sleep_h)
    return t + 3600.0 * max(0.0, snooze_h)


def sleep_depth(ts: float, start_ts: float, sp_now: float, sp_bed: float, theta: float, np_: NightParams) -> float:
    """0 (about to wake / snoozing) .. 1 (deepest). The fraction of tonight's homeostatic discharge still ahead
    (log scale: linear in time, so deep early in the night and lighter toward morning), times a short light
    sleep-onset phase."""
    theta = max(1e-4, theta)
    if sp_bed <= theta * 1.0001 or sp_now <= theta:
        frac = 0.0
    else:
        frac = clamp(math.log(sp_now / theta) / math.log(sp_bed / theta), 0.0, 1.0)
    onset = 1.0 - math.exp(-max(0.0, ts - start_ts) / (60.0 * max(1e-3, np_.onset_min)))
    return frac * onset


def wake_probability(salience: float, depth: float, np_: NightParams, threshold_shift: float = 0.0) -> float:
    """Chance that a notification of salience 0..1 wakes me at sleep depth 0..1: logistic in salience around an
    arousal threshold that rises with depth. Monotone increasing in salience, decreasing in depth."""
    thr = np_.wake_base + np_.wake_span * clamp(depth, 0.0, 1.0) + threshold_shift
    z = (clamp(salience, 0.0, 1.0) - thr) / max(1e-3, np_.wake_width)
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))
