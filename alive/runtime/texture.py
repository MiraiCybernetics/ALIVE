"""Life texture: a stochastic system with tendency (inertia), phases and mutations (docs/21 §4).

Layers
- Tendency: each trait follows a mean-reverting Ornstein-Uhlenbeck process; traits are correlated through a
  few shared latent factors (e.g. "vitality" lifts chattiness, playfulness and initiative together).
- Phases: a Markov chain over life phases (normal / lively / low / absorbed / busy), switched at day
  boundaries; each phase shifts trait means; transition odds are nudged by recent real mood.
- Mutations: Poisson-timed jumps with heavy-tailed size that decay with a half-life, including interest
  mutations ("suddenly into X") seeded from topics the individual actually encountered.

Honesty (INV-26): the texture only changes tendencies and choices; it never invents experiences. Its
current phase and recent mutations are rendered as natural language for the self state so the individual
can talk truthfully about why it has been different lately.

Deterministic under a fixed seed; state (including the RNG) is persisted in kv.
"""
from __future__ import annotations

import logging
import math
import random
from typing import Optional, Sequence

log = logging.getLogger("alive.texture")

TRAITS = ["chattiness", "assertiveness", "playfulness", "initiative", "curiosity", "sociability", "focus",
          "sensitivity"]
TRAIT_ZH = {"chattiness": "话多", "assertiveness": "有主见", "playfulness": "爱玩闹", "initiative": "想主动找人",
            "curiosity": "好奇", "sociability": "想社交", "focus": "能专注", "sensitivity": "敏感"}
# latent factors -> trait loadings (correlation structure)
FACTORS = {
    "vitality":  {"chattiness": 0.6, "playfulness": 0.5, "initiative": 0.6, "sociability": 0.5, "curiosity": 0.3},
    "openness":  {"curiosity": 0.7, "assertiveness": 0.3, "playfulness": 0.3},
    "tension":   {"sensitivity": 0.7, "focus": -0.3, "chattiness": -0.2, "assertiveness": 0.2},
}
PHASES = {
    "normal":   {"zh": "", "shift": {}, "dwell_days": 5},
    "lively":   {"zh": "这几天莫名话多、心情也挺好", "shift": {"chattiness": 0.15, "playfulness": 0.12, "initiative": 0.12,
                                                       "sociability": 0.1}, "dwell_days": 3},
    "low":      {"zh": "这几天有点提不起劲，话也少了", "shift": {"chattiness": -0.15, "initiative": -0.15, "playfulness": -0.1,
                                                        "sensitivity": 0.1}, "dwell_days": 3},
    "absorbed": {"zh": "最近有点沉迷在自己的东西里", "shift": {"focus": 0.15, "curiosity": 0.12, "sociability": -0.08},
                 "dwell_days": 4},
    "busy":     {"zh": "这阵子自己的事比较多", "shift": {"focus": 0.1, "chattiness": -0.08, "initiative": -0.08},
                 "dwell_days": 3},
}


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def means_from_big_five(bf: dict) -> dict:
    """Long-term trait means derived from the persona's Big Five (0..1)."""
    O, C, E, A, N = (float(bf.get(k_, 0.5)) for k_ in "OCEAN")
    return {"chattiness": _clip(0.2 + 0.6 * E), "assertiveness": _clip(0.25 + 0.3 * E + 0.25 * (1 - A) + 0.1 * O),
            "playfulness": _clip(0.2 + 0.4 * E + 0.3 * O), "initiative": _clip(0.2 + 0.5 * E + 0.1 * O),
            "curiosity": _clip(0.15 + 0.7 * O), "sociability": _clip(0.2 + 0.6 * E), "focus": _clip(0.2 + 0.6 * C),
            "sensitivity": _clip(0.15 + 0.6 * N)}


class LifeTexture:
    def __init__(self, kernel, *, seed: Optional[int] = None, means: Optional[dict] = None,
                 config: Optional[dict] = None):
        self.k = kernel
        cfg = config or {}
        self.theta = float(cfg.get("theta_per_day", 1.2))        # mean reversion speed (1/day)
        self.sigma = float(cfg.get("sigma_per_sqrt_day", 0.12))  # volatility
        self.jump_rate = float(cfg.get("jumps_per_day", 0.25))
        self.jump_half_life_days = float(cfg.get("jump_half_life_days", 3.0))
        self.interest_mutation_share = float(cfg.get("interest_mutation_share", 0.4))
        self.enabled = bool(cfg.get("enabled", True))
        self.means = {t: 0.5 for t in TRAITS}
        if means:
            self.means.update({k_: _clip(float(v)) for k_, v in means.items() if k_ in self.means})
        self.x = dict(self.means)
        self.jumps: list = []          # [{"trait"|"topic", "size", "t0", "half_life"}]
        self.interests: dict = {}      # topic -> weight (0..1)
        self.phase = "normal"
        self.phase_since = 0.0
        self.phase_day = ""
        self.last_t: Optional[float] = None
        self.rng = random.Random(seed)
        self.mood_trace: list = []     # recent daily mood (P) samples, nudges phase transitions
        self.recent_topics: list = []  # topics actually encountered (for interest mutations)
        self.mutations_log: list = []
        self.daily: dict = {}          # "YYYY-MM-DD|key" -> value: stable-within-a-day draws (alive/runtime/chance.daily)

    # ------------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        return {"x": self.x, "jumps": self.jumps, "interests": self.interests, "phase": self.phase,
                "phase_since": self.phase_since, "phase_day": self.phase_day, "last_t": self.last_t,
                "rng": self.rng.getstate(), "mood_trace": self.mood_trace[-14:],
                "recent_topics": self.recent_topics[-40:], "mutations_log": self.mutations_log[-20:],
                "daily": {k_: v for k_, v in self.daily.items() if isinstance(v, (int, float, str))}}

    def load_state(self, st: dict) -> None:
        if not st:
            return
        self.x.update({k_: float(v) for k_, v in (st.get("x") or {}).items() if k_ in self.x})
        self.jumps = list(st.get("jumps") or [])
        self.interests = dict(st.get("interests") or {})
        self.phase = st.get("phase", "normal") if st.get("phase") in PHASES else "normal"
        self.phase_since = float(st.get("phase_since") or 0.0)
        self.phase_day = st.get("phase_day", "")
        self.last_t = st.get("last_t")
        rs = st.get("rng")
        if rs:
            try:
                self.rng.setstate((rs[0], tuple(rs[1]), rs[2]))
            except Exception:
                pass
        self.mood_trace = list(st.get("mood_trace") or [])
        self.recent_topics = list(st.get("recent_topics") or [])
        self.mutations_log = list(st.get("mutations_log") or [])
        self.daily = dict(st.get("daily") or {})

    async def save(self) -> None:
        await self.k.db.kv_set("texture", "state", self.to_state())

    async def start(self) -> None:
        self.load_state(await self.k.db.kv_get("texture", "state", {}) or {})
        if self.last_t is None:
            self.last_t = self.k.clock.now()
            self.phase_since = self.last_t

    # ------------------------------------------------------------------ dynamics
    def _jump_value(self, j: dict, now: float) -> float:
        age_days = max(0.0, (now - j["t0"]) / 86400)
        return j["size"] * 0.5 ** (age_days / max(0.1, j.get("half_life", self.jump_half_life_days)))

    def effective_mean(self, trait: str, now: float) -> float:
        m = self.means[trait] + PHASES[self.phase]["shift"].get(trait, 0.0)
        m += sum(self._jump_value(j, now) for j in self.jumps if j.get("trait") == trait)
        return _clip(m, 0.02, 0.98)

    def tick(self, now: Optional[float] = None) -> list:
        """Advance the processes to ``now``. Returns mutation/phase descriptions that happened (for events)."""
        if not self.enabled:
            return []
        now = self.k.clock.now() if now is None else now
        if self.last_t is None:
            self.last_t = now
        dt_days = max(0.0, (now - self.last_t) / 86400)
        self.last_t = now
        events: list = []
        if dt_days <= 0:
            return events
        # sub-step for stability (max 2 h per step)
        steps = max(1, int(math.ceil(dt_days / (2 / 24))))
        h = dt_days / steps
        for s in range(steps):
            t_s = now - (steps - s - 1) * h * 86400
            z = {f: self.rng.gauss(0, 1) for f in FACTORS}
            for tr in TRAITS:
                noise = self.rng.gauss(0, 1) * 0.6
                for f, load in FACTORS.items():
                    noise += load.get(tr, 0.0) * z[f]
                mu = self.effective_mean(tr, t_s)
                self.x[tr] = _clip(self.x[tr] + self.theta * (mu - self.x[tr]) * h + self.sigma * math.sqrt(h) * noise,
                                   0.0, 1.0)
            for topic in list(self.interests):
                w = self.interests[topic]
                w += 0.8 * (0.2 - w) * h + 0.08 * math.sqrt(h) * self.rng.gauss(0, 1)
                if w < 0.03:
                    self.interests.pop(topic, None)
                else:
                    self.interests[topic] = _clip(w)
            # mutations: Poisson arrivals
            if self.rng.random() < 1 - math.exp(-self.jump_rate * h):
                ev = self._mutate(t_s)
                if ev:
                    events.append(ev)
        # phase transitions at local day boundaries
        day = self.k.clock.today(now)
        if day != self.phase_day:
            self.phase_day = day
            ev = self._maybe_switch_phase(now)
            if ev:
                events.append(ev)
        self.jumps = [j for j in self.jumps if abs(self._jump_value(j, now)) > 0.01]
        return events

    def _mutate(self, now: float) -> Optional[dict]:
        if self.recent_topics and self.rng.random() < self.interest_mutation_share:
            topic = self.rng.choice(self.recent_topics[-20:])
            self.interests[topic] = _clip(self.interests.get(topic, 0.2) + 0.5 + 0.2 * self.rng.random())
            ev = {"kind": "interest", "topic": topic, "text": f"最近突然对「{topic}」很感兴趣", "ts": now}
        else:
            tr = self.rng.choice(TRAITS)
            size = max(-0.3, min(0.3, 0.12 * self.rng.gauss(0, 1) / max(0.35, abs(self.rng.gauss(0, 1)) ** 0.5)))
            if abs(size) < 0.04:
                return None
            self.jumps.append({"trait": tr, "size": size, "t0": now,
                               "half_life": self.jump_half_life_days * (0.6 + 0.8 * self.rng.random())})
            word = TRAIT_ZH[tr]
            ev = {"kind": "trait", "trait": tr, "size": round(size, 3),
                  "text": f"这两天突然{'更' if size > 0 else '没那么'}{word}了", "ts": now}
        self.mutations_log.append(ev)
        return ev

    def _maybe_switch_phase(self, now: float) -> Optional[dict]:
        dwell = (now - self.phase_since) / 86400
        mean_dwell = PHASES[self.phase]["dwell_days"]
        p_leave = 1 - math.exp(-1.0 / max(1.0, mean_dwell))
        if self.phase == "normal":
            p_leave *= 0.6
        if dwell < 1 or self.rng.random() > p_leave:
            return None
        mood = sum(self.mood_trace[-3:]) / len(self.mood_trace[-3:]) if self.mood_trace else 0.0
        weights = {"normal": 3.0, "lively": 1.0 + 2.0 * max(0.0, mood), "low": 1.0 + 2.5 * max(0.0, -mood),
                   "absorbed": 1.0 + 1.5 * self.means["curiosity"], "busy": 0.8}
        weights.pop(self.phase, None)
        total = sum(weights.values())
        r = self.rng.random() * total
        for ph, w in weights.items():
            r -= w
            if r <= 0:
                break
        prev = self.phase
        self.phase, self.phase_since = ph, now
        ev = {"kind": "phase", "from": prev, "to": ph, "text": PHASES[ph]["zh"] or "最近又恢复平常了", "ts": now}
        self.mutations_log.append(ev)
        return ev

    # ------------------------------------------------------------------ inputs from real experience
    def observe_mood(self, valence: float) -> None:
        """Daily-ish mood samples (from the affect system) nudge phase transitions."""
        self.mood_trace.append(_clip(float(valence), -1.0, 1.0))
        self.mood_trace = self.mood_trace[-30:]

    def observe_topic(self, topic: str) -> None:
        """A topic actually encountered (reading, conversation). Candidates for interest mutations."""
        topic = (topic or "").strip()[:20]
        if topic and topic not in self.recent_topics[-5:]:
            self.recent_topics.append(topic)
            self.recent_topics = self.recent_topics[-60:]

    # ------------------------------------------------------------------ outputs
    def value(self, trait: str) -> float:
        return self.x.get(trait, 0.5) if self.enabled else self.means.get(trait, 0.5)

    def multiplier(self, trait: str, strength: float = 1.0) -> float:
        """Around 1.0: (value - long-term mean) mapped to [0.5, 1.5] scaled by strength."""
        d = self.value(trait) - self.means.get(trait, 0.5)
        return max(0.5, min(1.5, 1.0 + strength * 2.0 * d))

    def top_interests(self, n: int = 3) -> list:
        return [t for t, _ in sorted(self.interests.items(), key=lambda x: -x[1])[:n]]

    def choose(self, options: Sequence[str], weights: Optional[Sequence[float]] = None) -> Optional[str]:
        if not options:
            return None
        w = list(weights) if weights else [1.0] * len(options)
        w = [max(0.0, x) * (1.0 + self.interests.get(o, 0.0)) for o, x in zip(options, w)]
        total = sum(w) or 1.0
        r = self.rng.random() * total
        for o, x in zip(options, w):
            r -= x
            if r <= 0:
                return o
        return options[-1]

    def describe(self) -> str:
        """Natural-language self description of the current texture (for the self state)."""
        if not self.enabled:
            return ""
        parts = []
        if PHASES[self.phase]["zh"]:
            parts.append(PHASES[self.phase]["zh"])
        now = self.k.clock.now()
        recent = [m for m in self.mutations_log if now - m.get("ts", 0) < 3 * 86400 and m.get("kind") != "phase"]
        if recent:
            parts.append(recent[-1]["text"])
        return "；".join(parts)

    def status(self) -> dict:
        return {"phase": self.phase, "traits": {k_: round(v, 3) for k_, v in self.x.items()},
                "interests": dict(sorted(self.interests.items(), key=lambda x: -x[1])[:5]),
                "recent": self.mutations_log[-3:]}
