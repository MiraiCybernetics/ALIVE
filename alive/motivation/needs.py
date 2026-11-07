"""Homeostatic needs (docs/05 §2): levels, decay, coupling, pressure. Pure model, no kernel access."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

NEED_NAMES = ("social", "curiosity", "rest", "competence", "safety", "autonomy", "coherence")

DEFAULT_NEEDS: Dict[str, dict] = {
    "social": {"setpoint": 0.65, "weight": 1.0, "decay": -0.04},
    "curiosity": {"setpoint": 0.75, "weight": 1.0, "decay": -0.05},
    "rest": {"setpoint": 0.6, "weight": 0.8, "decay": 0.0},
    "competence": {"setpoint": 0.55, "weight": 0.7, "decay": -0.02},
    "safety": {"setpoint": 0.8, "weight": 0.9, "decay": 0.02},
    "autonomy": {"setpoint": 0.6, "weight": 0.7, "decay": -0.02},
    "coherence": {"setpoint": 0.7, "weight": 0.6, "decay": -0.01},
}

# CENTRES of the urge onset on (coupled) pressure (weight 1.0 & deficit ~0.19 -> 0.08). Onset is logistic, not a
# step: each urge episode draws its own latent threshold around the centre (``logistic_threshold``), so
# P(urge | pressure) = sigmoid((pressure - centre) / (0.2 x centre)): about one episode in four starts below 0.064 or
# above 0.096. Today's setpoint and decay are drawn per day by the engine (MotivationEngine._daily_needs).
DEFAULT_THRESHOLDS: Dict[str, float] = {"social": 0.08, "curiosity": 0.08, "rest": 0.08, "competence": 0.08,
                                        "safety": 0.06, "autonomy": 0.08, "coherence": 0.08}

DEFAULT_COUPLING = {
    "rest_amplify": 0.5,               # low rest amplifies every other pressure ("累了什么都烦")
    "safety_suppress_curiosity": 0.8,  # low safety suppresses curiosity ("受威胁时不探索")
    "boredom_curiosity": 0.5,          # boredom (affect) pushes curiosity pressure up (docs/04 §3.3)
}

GAMMA = 1.5


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


ONSET_WIDTH = 0.2      # logistic scale of the latent onset, relative to its centre


def logistic_threshold(rng, centre: float, width: float = ONSET_WIDTH) -> float:
    """A latent onset threshold ~ Logistic(centre, width x centre), floored at 0.2 x centre (an urge needs some
    pressure). Drawn once per episode, so the onset probability at pressure p is the logistic CDF - smooth in p and
    independent of how often it is checked."""
    u = min(1 - 1e-9, max(1e-9, rng.random()))
    return max(0.2 * centre, centre + width * centre * math.log(u / (1 - u)))


@dataclass
class Need:
    name: str
    setpoint: float
    weight: float
    decay: float                 # per hour (negative = level falls; positive = level recovers)
    threshold: float = 0.08
    level: float = 0.0
    last_satisfied: Optional[float] = None


@dataclass
class NeedModel:
    needs: Dict[str, Need] = field(default_factory=dict)
    coupling: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_COUPLING))
    sleep_decay_factor: float = 0.25   # needs barely drift while asleep
    rest_tau_h: float = 0.5            # rest level follows energy with this time constant

    @classmethod
    def from_temperament(cls, temperament: dict, overrides: Optional[dict] = None) -> "NeedModel":
        spec = {k: dict(v) for k, v in DEFAULT_NEEDS.items()}
        for name, v in (temperament.get("needs") or {}).items():
            spec.setdefault(name, {}).update(v or {})
        for name, v in ((overrides or {}).get("needs") or {}).items():
            spec.setdefault(name, {}).update(v or {})
        th = dict(DEFAULT_THRESHOLDS)
        th.update((overrides or {}).get("urge_thresholds") or {})
        needs = {}
        for name, v in spec.items():
            sp = float(v.get("setpoint", 0.6))
            needs[name] = Need(name=name, setpoint=sp, weight=float(v.get("weight", 1.0)),
                               decay=float(v.get("decay", 0.0)), threshold=float(v.get("threshold", th.get(name, 0.08))),
                               level=sp)
        coupling = dict(DEFAULT_COUPLING)
        coupling.update(temperament.get("coupling") or {})
        coupling.update((overrides or {}).get("coupling") or {})
        return cls(needs=needs, coupling=coupling,
                   sleep_decay_factor=float((overrides or {}).get("sleep_decay_factor", 0.25)))

    # ------------------------------------------------------------------ dynamics
    def step(self, dt_h: float, *, energy: Optional[float], asleep: bool) -> None:
        """Advance levels by dt hours. Exact for linear decay; rest follows energy exponentially."""
        if dt_h <= 0:
            return
        f = self.sleep_decay_factor if asleep else 1.0
        for n in self.needs.values():
            if n.name == "rest":
                if energy is not None:
                    a = 1.0 - math.exp(-dt_h / max(0.01, self.rest_tau_h))
                    n.level = clamp(n.level + (clamp(energy) - n.level) * a)
                continue
            if n.decay > 0:   # upward regression (e.g. safety) recovers to the setpoint, not beyond
                if n.level < n.setpoint:
                    n.level = min(n.setpoint, n.level + n.decay * dt_h * f)
            else:
                n.level = clamp(n.level + n.decay * dt_h * f)

    def setpoint_offsets(self, mood_dev: tuple, stress: float = 0.0) -> Dict[str, float]:
        """Mood shifts setpoints temporarily (docs/05 §2.3): good mood -> want more contact, etc."""
        dP, dA = (mood_dev + (0.0, 0.0))[:2]
        return {"social": clamp(0.1 * dP, -0.08, 0.08), "curiosity": clamp(0.05 * dA, -0.05, 0.05),
                "rest": clamp(0.1 * stress, 0.0, 0.08)}

    def raw_pressure(self, n: Need, offset: float = 0.0) -> float:
        deficit = max(0.0, clamp(n.setpoint + offset) - n.level)
        return n.weight * deficit ** GAMMA

    def pressures(self, *, mood_dev: tuple = (0.0, 0.0), stress: float = 0.0, boredom: float = 0.0,
                  weights: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """pressure_i = w_i * max(0, setpoint_i' - level_i)^1.5, then coupling."""
        offs = self.setpoint_offsets(mood_dev, stress)
        w = weights or {}
        raw = {name: self.raw_pressure(n, offs.get(name, 0.0)) * float(w.get(name, 1.0))
               for name, n in self.needs.items()}
        out = dict(raw)
        rest = self.needs.get("rest")
        if rest is not None and rest.setpoint > 0:
            tired = clamp((rest.setpoint - rest.level) / rest.setpoint)
            amp = 1.0 + self.coupling.get("rest_amplify", 0.0) * tired
            for name in out:
                if name != "rest":
                    out[name] *= amp
        safety = self.needs.get("safety")
        if safety is not None and "curiosity" in out and safety.setpoint > 0:
            threat = clamp((safety.setpoint - safety.level) / safety.setpoint)
            out["curiosity"] *= max(0.0, 1.0 - self.coupling.get("safety_suppress_curiosity", 0.0) * threat)
        if "curiosity" in out:
            out["curiosity"] *= 1.0 + self.coupling.get("boredom_curiosity", 0.0) * clamp(boredom)
        return out

    def to_state(self) -> dict:
        return {name: {"level": n.level, "last_satisfied": n.last_satisfied} for name, n in self.needs.items()}

    def load_state(self, st: dict) -> None:
        for name, v in (st or {}).items():
            n = self.needs.get(name)
            if n is not None and isinstance(v, dict):
                n.level = clamp(float(v.get("level", n.level)))
                n.last_satisfied = v.get("last_satisfied")
