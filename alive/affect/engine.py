"""AffectEngine: the AffectSystem implementation (docs/04).

State lives in memory; every sync method is cheap (no I/O). Persistence is write-behind to ``kv``
(namespace "affect", key "state") on change and every ``save_every_s`` virtual seconds; an
``aff.snapshot`` event is emitted at low frequency and ``aff.delta`` whenever emotions are created.

Time handling: the authoritative time is ``kernel.clock.now()``. Reads (snapshot/labels/...) lazily
advance the dynamics to "now" with the last known context, so the state is consistent even if the
heartbeat calls ``tick`` irregularly. Large gaps (restart after downtime) are integrated in stable
substeps following the persona's sleep rhythm; gaps over ``max_catchup_days`` reset to baseline and
simulate only the final two days.
"""
from __future__ import annotations

import logging
import math
import os
from collections import deque
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from alive.affect import appraisal as ap
from alive.affect import expression as ex
from alive.affect._bg import Background, WriteBehind
from alive.affect import dynamics as dyn
from alive.affect.dynamics import (MoodParams, NightParams, SleepParams, SleepRhythm, alma_default_mood, clamp,
                                   energy_value, max_stable_dt_h, mood_octant, mood_step, norm,
                                   regression_half_life, sleep_pressure_step)
from alive.affect.prompts import REAPPRAISE, REAPPRAISE_SCHEMA, REAPPRAISE_SYSTEM
from alive.affect.tables import (EMOTION_PAD, FAMILY, HALF_LIFE_H, HIGH_AROUSAL_NEGATIVE, is_negative,
                                 normalize_type)
from alive.core.types import EV, new_id
from alive.interfaces import AffectSnapshot, AffectSystem
from alive.runtime import chance

log = logging.getLogger("alive.affect")

# Social field (docs/04 §3.3): who I am "with" right now. Presence of a person decays with tau 10 min after their
# last line (the old fixed 600 s window, made smooth); an unaddressed line in a shared space is background (0.4).
SOCIAL_TAU_S = 600.0
SOCIAL_BACKGROUND_W = 0.4
SOCIAL_REF_CLOSENESS = 0.3     # a typical acquaintance: the per-person cost factor is 1.0 there

KV_NS = "affect"
KV_KEY = "state"
STATE_VERSION = 1
REMOVE_BELOW = 0.05
MAX_ACTIVE = 16


@dataclass
class Emotion:
    id: str
    type: str
    intensity: float
    initial: float
    born_at: float
    updated_at: float
    half_life_h: float
    target: Optional[str] = None
    target_name: Optional[str] = None
    cause: str = ""            # Event.id that caused it
    cause_text: str = ""       # short human description for labels
    source: str = "deep"       # quick | deep | need | system
    ruminations: int = 0

    @property
    def pad(self) -> Tuple[float, float, float]:
        return EMOTION_PAD.get(self.type, (0.0, 0.0, 0.0))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["intensity"] = round(self.intensity, 4)
        d["initial"] = round(self.initial, 4)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Emotion":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})


def _load_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception:  # pragma: no cover
        log.exception("failed to read %s", path)
        return {}


class AffectEngine(AffectSystem):
    def __init__(self, kernel):
        super().__init__(kernel)
        self.cfg = kernel.cfg.section("affect")
        pdir = kernel.persona_dir or ""
        self.temperament: dict = _load_yaml(os.path.join(pdir, "temperament.yaml")) if pdir else {}
        self.temperament.update(self.cfg.get("temperament", {}) or {})
        self.rhythm_cfg: dict = _load_yaml(os.path.join(pdir, "rhythm.yaml")) if pdir else {}
        self.rhythm_cfg.update(self.cfg.get("rhythm", {}) or {})
        self.big5: Dict[str, float] = {k: float(v) for k, v in (self.temperament.get("big_five") or {}).items()}
        for k in ("O", "C", "E", "A", "N"):
            self.big5.setdefault(k, 0.5)
        self.regulation_style = str(self.temperament.get("regulation_style", "reappraise"))
        self.sensitivity: Dict[str, float] = {str(k): float(v) for k, v in
                                              (self.temperament.get("emotion_sensitivity") or {}).items()}
        self.mood_phrases = self.temperament.get("mood_phrases") or self.cfg.get("mood_phrases", {}) or {}
        self.bands = tuple(self.cfg.get("mood_bands", (2.0 / 3.0, 4.0 / 3.0)))

        # personality -> baseline mood (ALMA)
        self.default: Tuple[float, float, float] = alma_default_mood(
            self.big5, scale=self.cfg.get("big_five_scale", "centered"), coeffs=self.cfg.get("alma_coefficients"))
        lo, hi = self.cfg.get("mood_half_life_h", [12.0, 48.0])
        self.mood_params = MoodParams(
            half_life_h=regression_half_life(self.big5["N"], float(lo), float(hi)),
            k_emotion=float(self.cfg.get("k_emotion", 0.8)), k_push=float(self.cfg.get("k_push", 0.05)),
            k_bored=float(self.cfg.get("k_bored", 0.015)), damping_ratio=float(self.cfg.get("damping_ratio", 0.9)))
        sp_cfg = self.cfg.get("sleep", {}) or {}
        self.sleep_params = SleepParams(**{k: float(v) for k, v in sp_cfg.items()
                                           if k in SleepParams.__dataclass_fields__})
        self.rhythm = SleepRhythm.from_cfg(kernel.clock.tz, self.rhythm_cfg)
        self.night_params = NightParams.from_cfg(self.cfg.get("night", {}) or {})
        # rhythm.yaml's weekend_shift_hours is the mean of the per-night weekend draw
        self.weekend_mean_h = float(self.rhythm_cfg.get("weekend_shift_hours", 0.0) or 0.0)
        self._night_len_h = self.rhythm.habitual_length_h()
        self._sp_bed_star = dyn.typical_bed_pressure(self.sleep_params, self._night_len_h)
        self._theta_star = dyn.typical_wake_threshold(self.sleep_params, self._night_len_h)
        self.half_lives = self._half_lives()

        # tunables
        self.save_every_s = float(self.cfg.get("save_every_s", 900))
        self.snapshot_event_every_s = float(self.cfg.get("snapshot_event_every_s", 3600))
        self.max_catchup_days = float(self.cfg.get("max_catchup_days", 3))
        self.regulation_cooldown_s = float(self.cfg.get("regulation_cooldown_s", 1800))
        self.follow_rhythm = bool(self.cfg.get("follow_rhythm", True))
        self.boredom_rate_h = float(self.cfg.get("boredom_rate_h", 0.12))
        self.neg_mood_p = float(self.cfg.get("negative_mood_p", -0.1))
        self.lockin_after_s = float(self.cfg.get("lockin_after_h", 48)) * 3600

        # state
        self.mood: Tuple[float, float, float] = self.default
        self.vel: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.emotions: List[Emotion] = []
        self.sp = self.sleep_params.sp_wake
        self.battery = 0.8
        self.stress = 0.1
        self.boredom = 0.0
        self.asleep = False
        self.t: Optional[float] = None
        self.ctx: Dict[str, float] = {"load": 0.0, "open_loops": 0.0, "social_active": 0.0}
        self.neg_since: Optional[float] = None
        self.anti_lockin = False
        self.woke_at: Optional[float] = None
        self.slept_at: Optional[float] = None
        self.nights: Dict[str, dict] = {}   # committed nights: "YYYY-MM-DD" -> {"start","end","sp_bed","theta"}
        self._sp_ts: Optional[float] = None  # the (simulated) time the physiological state refers to
        self._people: Dict[str, dict] = {}  # social field: person -> {"t": last line, "w": weight, "c": closeness}
        self._external_sleep = False
        self._catching_up = False
        self.divergences: deque = deque(maxlen=200)
        self.counters = {"emotions": 0, "divergences": 0, "regulations": 0, "lockins": 0}
        self._reg_cooldown: Dict[str, float] = {}
        self._last_throttle_bump = -1e18
        self._last_snapshot_event = -1e18
        self._handles: list = []
        self._sub = None
        self.bg = Background(log)
        self._wb = WriteBehind(self.bg, self._persist)

    # ------------------------------------------------------------------ config helpers
    def _half_lives(self) -> Dict[str, float]:
        N, E = self.big5["N"], self.big5["E"]
        out = {}
        for t in EMOTION_PAD:
            h = HALF_LIFE_H.get(t, 2.0)
            out[t] = h * ((0.8 + 0.5 * N) if is_negative(t) else (0.9 + 0.2 * E))
        for src in (self.temperament.get("half_life_h") or {}, self.cfg.get("half_life_h", {}) or {}):
            for t, h in src.items():
                nt = normalize_type(t)
                if nt:
                    out[nt] = float(h)
        return out

    def _tex_mult(self, trait: str, strength: float = 1.0) -> float:
        """Today's life-texture multiplier (around 1.0; 1.0 without a texture)."""
        tex = getattr(self.k, "texture", None)
        if tex is None:
            return 1.0
        try:
            return float(tex.multiplier(trait, strength))
        except Exception:
            return 1.0

    def _sens(self, etype: str) -> float:
        """Temperament's emotion sensitivity x today's sensitivity texture (strength 1: a typical day moves it by
        ~+-15%; the persona value stays the long-run mean)."""
        if etype in self.sensitivity:
            base = self.sensitivity[etype]
        else:
            fam = FAMILY.get(etype)
            base = self.sensitivity.get(fam, 1.0) if fam else 1.0
        return base * self._tex_mult("sensitivity", 1.0)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        now = self.k.clock.now()
        saved = None
        try:
            saved = await self.k.db.kv_get(KV_NS, KV_KEY)
        except Exception:
            log.exception("failed to load affect state")
        if isinstance(saved, dict) and saved.get("version") == STATE_VERSION:
            self._restore(saved)
            self.catch_up(now)
        else:
            self._init_fresh(now)
        try:
            self._sub = self.k.bus.subscribe("percept.message|rt.budget_throttled", self._on_event,
                                             name="affect.events")
        except RuntimeError:  # pragma: no cover
            self._sub = None
        sched = self.k.scheduler
        self._handles.append(sched.every(self.save_every_s, self._periodic, "affect.save"))
        if self.cfg.get("energy_route_hook", True) and self.k.llm is not None:
            self.k.llm.add_hook(self.route_hook)
        if self.k.llm is not None and hasattr(self.k.llm, "set_state_source"):
            # per-request sampling temperature for composition follows playfulness and arousal (docs/10 §4.2)
            self.k.llm.set_state_source(self.llm_state, **(self.k.cfg.get("llm.state_temperature") or {}))
        self._wb.mark()

    async def stop(self) -> None:
        for h in self._handles:
            h.cancel()
        self._handles.clear()
        if self._sub is not None:
            self._sub.cancel()
        try:
            await self.save(emit=False)
        except Exception:
            log.exception("affect save on stop failed")
        await self.bg.drain(2.0)

    async def rekey_entity(self, old: str, new: str) -> None:
        """Entity merge (docs/22 §3.3): emotions aimed at ``old`` are now aimed at ``new`` (the persisted state in
        kv was rewritten by alive/world/rekey.py; this moves the live one)."""
        moved = 0
        for e in self.emotions:
            if e.target == old:
                e.target = new
                moved += 1
        if moved:
            self._wb.mark()

    def _init_fresh(self, now: float) -> None:
        """First boot: baseline mood; physiology consistent with the current time of day."""
        self.mood, self.vel, self.emotions = self.default, (0.0, 0.0, 0.0), []
        sp = self.sleep_params
        n = self._night_at(now, commit=False, prior=True)      # no lived state yet: tonight's draws, no state shift
        if n is not None:
            self.sp = n["sp_bed"] * math.exp(-sp.r_sleep * max(0.0, now - n["start"]) / 3600)
            self.nights[n["key"]] = {k_: n[k_] for k_ in ("start", "end", "sp_bed", "theta")}
            self.asleep = self.follow_rhythm
        else:
            awake_h = max(0.0, now - self._last_wake(now, prior=True)) / 3600
            self.sp = 1.0 - (1.0 - sp.sp_wake) * math.exp(-sp.r_wake * awake_h)
            self.asleep = False
        self.t = self._sp_ts = now

    def _restore(self, s: dict) -> None:
        self.mood = tuple(float(x) for x in s.get("mood", self.default))[:3]  # type: ignore[assignment]
        self.vel = tuple(float(x) for x in s.get("vel", (0, 0, 0)))[:3]  # type: ignore[assignment]
        self.emotions = [Emotion.from_dict(e) for e in s.get("emotions", []) if e.get("type") in EMOTION_PAD]
        self.sp = float(s.get("sp", self.sp))
        self.battery = float(s.get("battery", self.battery))
        self.stress = float(s.get("stress", self.stress))
        self.boredom = float(s.get("boredom", self.boredom))
        self.asleep = bool(s.get("asleep", False))
        self.t = float(s.get("t")) if s.get("t") is not None else None
        self._sp_ts = self.t
        self.neg_since = s.get("neg_since")
        self.anti_lockin = bool(s.get("anti_lockin", False))
        self.woke_at = s.get("woke_at")
        self.slept_at = s.get("slept_at")
        self._external_sleep = bool(s.get("external_sleep", False))
        # (a persisted social level is not restored: presence is rebuilt from what I perceive after the restart)
        self.ctx.update({k: float(v) for k, v in (s.get("ctx") or {}).items() if k in self.ctx and k != "social_active"})
        self.nights ={str(k): {f: float(v[f]) for f in ("start", "end", "sp_bed", "theta") if f in v}
                       for k, v in (s.get("nights") or {}).items() if isinstance(v, dict) and "end" in v}

    def state_dict(self) -> dict:
        return {"version": STATE_VERSION, "t": self.t, "mood": list(self.mood), "vel": list(self.vel),
                "emotions": [e.to_dict() for e in self.emotions], "sp": self.sp, "battery": self.battery,
                "stress": self.stress, "boredom": self.boredom, "asleep": self.asleep,
                "neg_since": self.neg_since, "anti_lockin": self.anti_lockin, "woke_at": self.woke_at,
                "slept_at": self.slept_at, "external_sleep": self._external_sleep, "ctx": dict(self.ctx),
                "nights": dict(self.nights)}

    async def _persist(self) -> None:
        await self.k.db.kv_set(KV_NS, KV_KEY, self.state_dict(), ts=self.k.clock.now())

    async def save(self, emit: bool = True) -> None:
        """Persist snapshot (kv) and emit ``aff.snapshot``."""
        self._sync()
        await self._wb.flush()
        if emit:
            await self._emit_snapshot()

    async def _emit_snapshot(self) -> None:
        self._last_snapshot_event = self.k.clock.now()
        snap = asdict(self.snapshot())
        snap["labels"] = self.labels()
        snap["octant"] = list(self.mood_octant()[:2])
        await self.k.bus.emit(EV.AFFECT_SNAPSHOT, snap, source="affect")

    async def _periodic(self) -> None:
        self._sync()
        await self._wb.flush()
        if self.k.clock.now() - self._last_snapshot_event >= self.snapshot_event_every_s:
            await self._emit_snapshot()

    async def _on_event(self, ev) -> None:
        if ev.type == EV.MESSAGE_IN:
            self._sync()
            p = ev.payload or {}
            direct = p.get("chat_type") == "private" or p.get("mentions_me") or p.get("reply_to_me")
            self.boredom *= 0.5 if direct else 0.85
            self._note_presence(p, direct or bool(p.get("addressed")))
        elif ev.type == EV.BUDGET_THROTTLED:
            now = self.k.clock.now()
            if now - self._last_throttle_bump >= 1800:
                self._last_throttle_bump = now
                self.bump_stress(0.05, "budget_throttled")

    # ------------------------------------------------------------------ dynamics
    def _sync(self, now: Optional[float] = None) -> None:
        self._advance(self.k.clock.now() if now is None else now)

    def catch_up(self, now: float) -> None:
        """Integrate from the persisted timestamp to ``now`` assuming the persona followed its rhythm."""
        if self.t is None:
            self._init_fresh(now)
            return
        gap = now - self.t
        if gap <= 0:
            return
        self._catching_up = True
        try:
            if gap > self.max_catchup_days * 86400:
                # everything has regressed long ago: baseline mood, no emotions; simulate the last 2 days
                self.mood, self.vel, self.emotions = self.default, (0.0, 0.0, 0.0), []
                self.neg_since, self.anti_lockin = None, False
                self.t = now - 2 * 86400
                self.boredom = 0.0
            self._advance(now)
        finally:
            self._catching_up = False
        log.info("affect caught up %.1f h of downtime", gap / 3600)

    def _asleep_at(self, ts: float) -> bool:
        if self._catching_up or (self.follow_rhythm and not self._external_sleep):
            return self._night_at(ts, commit=True) is not None
        return self.asleep

    # ------------------------------------------------------------------ nights (docs/04 §3.3, ADR-0007)
    def _draw(self, d, name: str, sampler) -> float:
        """One persisted draw per night (keyed by the night's date in the life texture's daily store)."""
        return float(chance.daily(self.k, f"sleep.{name}", sampler, ts=self.rhythm.noon(d)))

    def _sp_at(self, ts: float) -> float:
        """Sleep pressure projected from the integrated state to ``ts`` assuming I stay awake until then."""
        t0 = self._sp_ts if self._sp_ts is not None else ts
        if ts <= t0:
            return self.sp
        return clamp(1.0 - (1.0 - self.sp) * math.exp(-self.sleep_params.r_wake * (ts - t0) / 3600.0), 0.0, 1.0)

    def _expected_night(self, d, *, prior: bool = False) -> dict:
        """Night ``d`` as it would go now: the persisted draws of that night plus - for the coming night only - the
        state shift (homeostatic offset and arousal), re-evaluated until I actually fall asleep."""
        np_, sp = self.night_params, self.sleep_params
        weekend = self.rhythm.is_weekend_night(d)
        wk = self._draw(d, "weekend_h", lambda r: r.gauss(self.weekend_mean_h, np_.weekend_sd_h)) if weekend else 0.0
        z_bed = self._draw(d, "z_bed", lambda r: r.gauss(0.0, 1.0))
        z_th = self._draw(d, "z_theta", lambda r: r.gauss(0.0, 1.0))
        snooze = self._draw(d, "snooze_h", lambda r: chance.lognormal(r, np_.snooze_median_h, np_.snooze_sigma,
                                                                        hi=1.5))
        hab = self.rhythm.habitual_start(d)
        now = self._sp_ts if self._sp_ts is not None else (self.t if self.t is not None else self.k.clock.now())
        coming = not prior and self.rhythm.night_of(now) == d
        off, a_dev = 0.0, 0.0
        if coming:
            off = dyn.homeostatic_offset_h(self._sp_at(now), now, hab, self._sp_bed_star, sp)
            a_dev = self.mood[1] - self.default[1]
        start = dyn.bedtime_ts(hab, wk, z_bed, off, a_dev, np_)
        sp_bed = self._sp_at(start) if coming else self._sp_bed_star
        theta = self._theta_star * math.exp(np_.theta_sigma * z_th)
        end = dyn.wake_ts(start, sp_bed, self.rhythm.habitual_wake(d) + 3600.0 * wk, theta, snooze, sp, np_)
        return {"key": d.isoformat(), "start": start, "end": end, "sp_bed": sp_bed, "theta": theta,
                "snooze": snooze, "anchor": self.rhythm.habitual_wake(d) + 3600.0 * wk, "date": d}

    def _commit(self, n: dict, ts: float) -> dict:
        """I fell asleep at ``ts`` (inside the expected window): fix tonight's wake from the pressure I go to bed with."""
        start = max(n["start"], min(ts, n["end"]))
        sp_bed = self._sp_at(start)
        end = dyn.wake_ts(start, sp_bed, n["anchor"], n["theta"], n["snooze"], self.sleep_params, self.night_params)
        night = {"start": start, "end": end, "sp_bed": sp_bed, "theta": n["theta"]}
        self.nights[n["key"]] = night
        for old in sorted(self.nights)[:-6]:
            self.nights.pop(old, None)
        self._wb.mark()
        return {**night, "key": n["key"]}

    def _night_at(self, ts: float, *, commit: bool = False, prior: bool = False) -> Optional[dict]:
        """The night containing ``ts`` (committed, or as expected now), or None when ``ts`` is waking time.
        ``commit``: the first time a present (or simulated) moment falls inside a night, that night is fixed."""
        for key, n in self.nights.items():
            if n["start"] <= ts < n["end"]:
                return {**n, "key": key}
        from datetime import timedelta
        d = self.rhythm.night_of(ts)
        for dd in (d - timedelta(days=1), d):
            if dd.isoformat() in self.nights:      # that night already happened (ts is outside it)
                continue
            n = self._expected_night(dd, prior=prior)
            if n["start"] <= ts < n["end"]:
                return self._commit(n, ts) if commit else n
        return None

    def _last_wake(self, ts: float, prior: bool = False) -> float:
        """Most recent wake time (end of a night) <= ts."""
        from datetime import timedelta
        best = None
        for n in self.nights.values():
            if n["end"] <= ts and (best is None or n["end"] > best):
                best = n["end"]
        d = self.rhythm.night_of(ts)
        for k_ in range(-2, 1):
            dd = d + timedelta(days=k_)
            if dd.isoformat() in self.nights:
                continue
            e = self._expected_night(dd, prior=True)["end"]
            if e <= ts and (best is None or e > best):
                best = e
        return best if best is not None else ts - 8 * 3600

    def _next_night(self, ts: float) -> dict:
        """The night containing ``ts`` or, if awake, the coming one."""
        n = self._night_at(ts)
        if n is not None:
            return n
        from datetime import timedelta
        d = self.rhythm.night_of(ts)
        for k_ in range(0, 3):
            dd = d + timedelta(days=k_)
            if dd.isoformat() in self.nights:
                continue
            n = self._expected_night(dd)
            if n["start"] > ts:
                return n
        return self._expected_night(d + timedelta(days=1))  # pragma: no cover

    def _center(self) -> Tuple[Optional[Tuple[float, float, float]], float]:
        tot = sum(e.intensity for e in self.emotions)
        if tot <= 1e-9:
            return None, 0.0
        c = [0.0, 0.0, 0.0]
        for e in self.emotions:
            for i, x in enumerate(e.pad):
                c[i] += x * e.intensity
        strength = min(1.0, tot / len(self.emotions))
        return (c[0] / tot, c[1] / tot, c[2] / tot), strength

    def _advance(self, now: float) -> None:
        if self.t is None:
            self.t = now
            return
        remaining = now - self.t
        if remaining <= 0:
            return
        t = self.t
        while remaining > 1e-6:
            center, strength = self._center()
            dt_s = min(remaining, max(1.0, max_stable_dt_h(self.mood_params, strength) * 3600))
            self._step(t, dt_s, center, strength)
            t += dt_s
            remaining -= dt_s
        self.t = now

    def _step(self, t0: float, dt_s: float, center, strength: float) -> None:
        dt_h = dt_s / 3600.0
        t1 = t0 + dt_s
        self._sp_ts = t0
        asleep = self._asleep_at(t1)
        if asleep != self.asleep and (self._catching_up or not self._external_sleep):
            self._set_sleep_flag(asleep, t1, external=False)
        # emotions: exact exponential decay
        keep = []
        for e in self.emotions:
            e.intensity *= 2.0 ** (-dt_h / max(0.05, e.half_life_h))
            if e.intensity >= REMOVE_BELOW:
                keep.append(e)
        self.emotions = keep
        # mood
        self.mood, self.vel, _ = mood_step(self.mood, self.vel, self.default, center, strength, dt_h,
                                           self.mood_params, boredom=self.boredom, anti_lockin=self.anti_lockin)
        # sleep pressure (exact)
        self.sp = sleep_pressure_step(self.sp, asleep, dt_h, self.sleep_params)
        self._sp_ts = t1
        load = self.ctx.get("load", 0.0)
        # social presence: a smooth level from who has been talking around/to me (no fixed "active" window)
        presence, cost = (0.0, 0.0) if asleep else self._social_field(t0 + dt_s / 2)
        # stress relaxes toward a load/open-loop driven target; sleep washes it out
        if asleep:
            target, tau = 0.0, 3.0
        else:
            target = 0.05 + 0.5 * load + min(0.25, 0.025 * self.ctx.get("open_loops", 0.0))
            tau = 2.0
        self.stress = clamp(self.stress + (target - self.stress) * (1 - math.exp(-dt_h / tau)), 0.0, 1.0)
        # social battery: dB/dt = r (1 - B) (1 - presence) - drain. The drain (docs/04 §3.3) is the old 0.05/h x
        # extraversion factor, times ``cost``: audience size (sublinear, attention is shared) x per-person cost
        # (1 - closeness*E) relative to an acquaintance, divided by today's sociability texture. Recharge happens
        # alone (1 - presence), faster asleep. Integrated exactly for the step (linear ODE).
        E = self.big5["E"]
        drain = 0.05 * (0.6 + 0.8 * (1 - E)) * cost / self._tex_mult("sociability", 1.0)
        r = 0.2 * (0.8 + 0.4 * E) * (1.5 if asleep else 1.0) * (1.0 - presence)
        if r > 1e-9:
            b_eq = 1.0 - drain / r
            self.battery = b_eq + (self.battery - b_eq) * math.exp(-r * dt_h)
        else:
            self.battery -= drain * dt_h
        self.battery = clamp(self.battery, 0.0, 1.0)
        # boredom: grows linearly when awake and nothing salient happens (WASABI); company relieves it in
        # proportion to how present people are
        if asleep or self._catching_up:   # downtime is not an empty lived day: no boredom build-up
            self.boredom *= math.exp(-dt_h / 2.0)
        elif load >= 0.3:
            self.boredom *= math.exp(-dt_h * (1.0 + presence / 0.5))
        else:
            self.boredom = clamp(self.boredom + self.boredom_rate_h * (1.0 - presence) * dt_h, 0.0, 1.0)
            if presence > 0:
                self.boredom *= math.exp(-presence * dt_h / 0.5)
        # anti-lock-in (docs/04 §3.2)
        P = self.mood[0]
        if P < self.neg_mood_p:
            if self.neg_since is None:
                self.neg_since = t1
            elif not self.anti_lockin and t1 - self.neg_since >= self.lockin_after_s:
                self.anti_lockin = True
                self.counters["lockins"] += 1
                for e in self.emotions:
                    if is_negative(e.type):
                        e.intensity *= 0.8
                        e.initial = min(e.initial, e.intensity)
                log.info("affect anti-lock-in engaged after %.0f h of negative mood", (t1 - self.neg_since) / 3600)
        elif P >= -0.05:
            self.neg_since = None
            self.anti_lockin = False

    # ------------------------------------------------------------------ social field (battery)
    def _closeness(self, person: str) -> float:
        rel = getattr(getattr(self.k, "social", None), "relations", None)
        fn = getattr(rel, "cached_closeness", None)
        try:
            c = fn(person) if callable(fn) else None
        except Exception:
            c = None
        return SOCIAL_REF_CLOSENESS if c is None else clamp(float(c), 0.0, 1.0)

    def _note_presence(self, payload: dict, direct: bool) -> None:
        """Someone said something where I am: they are in my social field for a while."""
        who = payload.get("person_key") or payload.get("sender_entity")
        if not who or payload.get("is_bot"):
            return
        ts = self.t or self.k.clock.now()
        w = 1.0 if direct else SOCIAL_BACKGROUND_W
        prev = self._people.get(who)
        if prev is not None and ts - prev["t"] < SOCIAL_TAU_S:
            w = max(w, prev["w"])
        self._people[who] = {"t": ts, "w": w, "c": self._closeness(str(who))}
        if len(self._people) > 200:
            for k_ in sorted(self._people, key=lambda x: self._people[x]["t"])[:50]:
                self._people.pop(k_, None)

    def _social_field(self, ts: float) -> Tuple[float, float]:
        """(presence 0..1, drain cost). Presence pi_p = w_p exp(-age/10 min); presence = 1 - exp(-2 sum pi): ~0.86
        right after a DM line, ~0.5 ten minutes later, integrating to ~13 min of full presence per line (the old
        binary window gave 10). Cost = presence x mean per-person cost x sqrt((1 + N) / 2) with N = sum pi (one
        person -> 1, four -> 1.6, nine -> 2.2) and per-person cost (1 - c E) / (1 - 0.3 E): close people drain an
        extravert less."""
        floor = self.ctx.get("social_active", 0.0)     # an explicit 0..1 level reported by the caller, if any
        if not self._people:
            return floor, floor
        E = self.big5["E"]
        ref = 1.0 - SOCIAL_REF_CLOSENESS * E
        n, kc = 0.0, 0.0
        for who, p in list(self._people.items()):
            age = ts - p["t"]
            if age > 6 * SOCIAL_TAU_S:
                self._people.pop(who, None)
                continue
            pi = p["w"] * math.exp(-max(0.0, age) / SOCIAL_TAU_S)
            n += pi
            kc += pi * (1.0 - p["c"] * E) / max(1e-6, ref)
        if n <= 1e-9:
            return floor, floor
        presence = 1.0 - math.exp(-2.0 * n)
        cost = presence * (kc / n) * math.sqrt((1.0 + n) / 2.0)
        return max(presence, floor), max(cost, floor)

    def _set_sleep_flag(self, asleep: bool, ts: float, external: bool) -> None:
        if asleep == self.asleep:
            return
        self.asleep = asleep
        if asleep:
            self.slept_at = ts
        else:
            self.woke_at = ts

    # ------------------------------------------------------------------ derived values
    def energy(self) -> float:
        self._sync()
        return self._energy_at(self.t or self.k.clock.now())

    def _energy_at(self, ts: float) -> float:
        return energy_value(self.sp, self.rhythm.circadian(ts), self.ctx.get("load", 0.0), self.stress,
                            self.sleep_params)

    def mood_octant(self) -> Tuple[str, str, float]:
        """(octant, band, norm), e.g. ("relaxed", "slightly", 0.33). Extra public method."""
        self._sync()
        return mood_octant(self.mood, self.bands)

    def _expr(self) -> ex.ExprState:
        self._sync()
        now = self.t or self.k.clock.now()
        return ex.ExprState(
            mood=self.mood, default=self.default, emotions=[e.to_dict() for e in self.emotions],
            energy=self._energy_at(now), sleep_pressure=self.sp, circadian=self.rhythm.circadian(now),
            social_battery=self.battery, stress=self.stress, boredom=self.boredom, asleep=self.asleep,
            hour=self.rhythm.hour_of_day(now), now=now, woke_at=self.woke_at, big5=self.big5,
            regulation_style=self.regulation_style, mood_phrases=self.mood_phrases, bands=self.bands,
            texture={t: self._tex_mult(t, 1.0) for t in ("sensitivity", "sociability")})

    # ------------------------------------------------------------------ AffectSystem: reads
    def snapshot(self) -> AffectSnapshot:
        self._sync()
        now = self.t or self.k.clock.now()
        return AffectSnapshot(
            P=round(self.mood[0], 4), A=round(self.mood[1], 4), D=round(self.mood[2], 4),
            emotions=[{"type": e.type, "intensity": round(e.intensity, 4), "target": e.target, "cause": e.cause,
                       "born_at": e.born_at, "id": e.id, "cause_text": e.cause_text, "target_name": e.target_name}
                      for e in sorted(self.emotions, key=lambda x: -x.intensity)],
            energy=round(self._energy_at(now), 4), sleep_pressure=round(self.sp, 4),
            circadian=round(self.rhythm.circadian(now), 4), social_battery=round(self.battery, 4),
            stress=round(self.stress, 4), boredom=round(self.boredom, 4), asleep=self.asleep)

    def labels(self) -> dict:
        return ex.labels(self._expr())

    def style(self) -> dict:
        return ex.style(self._expr())

    def modulators(self) -> dict:
        return ex.modulators(self._expr())

    def high_negative_arousal(self) -> bool:
        st = self._expr()
        P, A, _ = st.mood
        if any(e["type"] in HIGH_AROUSAL_NEGATIVE and e["intensity"] >= 0.6 for e in st.emotions):
            return True
        if P <= -0.25 and A >= 0.3:
            return True
        return st.stress >= 0.75 and P < 0

    def status(self) -> dict:
        snap = self.snapshot()
        octant, band, n = mood_octant(self.mood, self.bands)
        return {"mood": [snap.P, snap.A, snap.D], "default": [round(x, 3) for x in self.default],
                "octant": f"{band} {octant}".strip(), "energy": snap.energy, "asleep": snap.asleep,
                "emotions": [(e["type"], e["intensity"]) for e in snap.emotions[:5]], "stress": snap.stress,
                "social_battery": snap.social_battery, "boredom": snap.boredom, "anti_lockin": self.anti_lockin,
                "big_five": dict(self.big5), "regulation_style": self.regulation_style,
                "labels": self.labels(), "counters": dict(self.counters)}

    # ------------------------------------------------------------------ AffectSystem: tick & sleep
    def tick(self, dt: float, now: float, ctx: dict) -> None:
        """Advance to ``now`` (authoritative; ``dt`` is used only when ``now`` is missing).
        ctx keys: asleep, workspace_load|load (0..1), open_loops (int), social_active (0..1 level; a bare bool is
        ignored - presence comes from the messages I perceive, see ``_social_field``)."""
        ctx = ctx or {}
        if not now:
            now = (self.t if self.t is not None else self.k.clock.now()) + max(0.0, float(dt or 0.0))
        # the elapsed interval is integrated with the context reported now
        if "workspace_load" in ctx or "load" in ctx:
            self.ctx["load"] = clamp(float(ctx.get("workspace_load", ctx.get("load", 0.0)) or 0.0), 0.0, 1.0)
        if "open_loops" in ctx:
            self.ctx["open_loops"] = float(ctx.get("open_loops") or 0)
        sa = ctx.get("social_active")
        if sa is not None and not isinstance(sa, bool):
            try:
                self.ctx["social_active"] = clamp(float(sa), 0.0, 1.0)
            except (TypeError, ValueError):
                pass
        if "asleep" in ctx and ctx["asleep"] is not None:
            self._external_sleep = True
            if bool(ctx["asleep"]) != self.asleep:
                self._advance(now)
                self._set_sleep_flag(bool(ctx["asleep"]), now, external=True)
                self._wb.mark()
        self._advance(now)

    def is_sleep_time(self, now: float) -> bool:
        """Inside tonight's (sampled, state-driven) night. Asking about the present - or the recent past - fixes the
        night the first time it says yes, so the answer does not flip back while I sleep; asking about the future
        does not fix anything (it is the night as it would go now)."""
        cur = self.k.clock.now()
        return self._night_at(now, commit=(cur - 6 * 3600 <= now <= cur + 1.0)) is not None

    def sleep_window(self, now: Optional[float] = None) -> Tuple[float, float]:
        """(start, end) of the current or next night. Extra public method."""
        n = self._next_night(self.k.clock.now() if now is None else now)
        return n["start"], n["end"]

    def expected_wake(self, now: Optional[float] = None) -> Optional[float]:
        """The ts at which I will get up from the night I am in at ``now``; None when that is waking time."""
        n = self._night_at(self.k.clock.now() if now is None else now)
        return n["end"] if n is not None else None

    def waking_span(self, now: Optional[float] = None) -> Tuple[float, float]:
        """(wake_ts, bed_ts) of the current waking period, or of the next one when ``now`` is inside a night.
        Extra public method (day-plan placement, plan prompt)."""
        now = self.k.clock.now() if now is None else now
        n = self._night_at(now)
        if n is not None:
            nxt = self._next_night(n["end"] + 60.0)
            return n["end"], nxt["start"]
        return self._last_wake(now), self._next_night(now)["start"]

    def today_wake_hour(self, now: Optional[float] = None) -> float:
        """Local hour (float) of today's wake time: this morning's, or the coming one if I have not got up yet."""
        from datetime import datetime, timedelta
        now = self.k.clock.now() if now is None else now
        d = datetime.fromtimestamp(now, self.k.clock.tz).date() - timedelta(days=1)   # the night ending today
        n = self.nights.get(d.isoformat()) or self._expected_night(d)
        end = n["end"]
        loc = datetime.fromtimestamp(end, self.k.clock.tz)
        return loc.hour + loc.minute / 60.0 + loc.second / 3600.0 + 24.0 * (loc.date() - d - timedelta(days=1)).days

    def expected_energy(self, ts: float) -> float:
        """Energy I expect at a future moment of the coming waking period (two-process model from the wake time
        before it; no load, light stress). 0 inside a night. Extra public method."""
        if self._night_at(ts) is not None:
            return 0.0
        w = self._last_wake(ts)
        sp = self.sleep_params
        n = next((x for x in self.nights.values() if abs(x["end"] - w) < 1.0), None)
        sp0 = n["theta"] if n is not None else self._theta_star
        sp_t = 1.0 - (1.0 - sp0) * math.exp(-sp.r_wake * max(0.0, ts - w) / 3600.0)
        return energy_value(sp_t, self.rhythm.circadian(ts), 0.0, 0.1, sp)

    def sleep_depth(self, now: Optional[float] = None) -> float:
        """0 (light / about to wake) .. 1 (deepest); 0 when awake. Extra public method."""
        self._sync()
        now = self.t if now is None else now
        if not self.asleep:
            return 0.0
        n = self._night_at(now)
        if n is None:          # a nap outside the night: light-to-moderate after its onset
            onset = 1.0 - math.exp(-max(0.0, now - (self.slept_at or now)) / (60.0 * self.night_params.onset_min))
            return 0.5 * onset
        return dyn.sleep_depth(now, n["start"], self.sp, n["sp_bed"], n["theta"], self.night_params)

    def wake_probability(self, salience: float) -> float:
        """Chance that a notification of ``salience`` 0..1 (urgency x closeness, computed by the caller) wakes me
        now. 1.0 when awake. Falls with sleep depth: deep early in the night, lighter toward the morning and in
        the minutes after falling asleep. A more sensitive day (texture) lowers the threshold a little."""
        if not self.snapshot().asleep:
            return 1.0
        shift = -0.1 * (self._tex_mult("sensitivity", 1.0) - 1.0)
        return dyn.wake_probability(salience, self.sleep_depth(), self.night_params, threshold_shift=shift)

    def set_asleep(self, asleep: bool) -> None:
        self._sync()
        self._external_sleep = True
        if bool(asleep) != self.asleep:
            self._set_sleep_flag(bool(asleep), self.t or self.k.clock.now(), external=True)
            self._wb.mark()

    # ------------------------------------------------------------------ emotion creation
    def _modulated(self, etype: str, base: float, rw: float, cap: float) -> float:
        P = self.mood[0]
        epad = EMOTION_PAD[etype]
        congruence = clamp((1 if epad[0] >= 0 else -1) * P / 0.6) if abs(epad[0]) > 1e-6 else 0.0
        x = base * self._sens(etype) * (0.6 + 0.8 * clamp(rw, 0.0, 1.0)) * (1 + 0.5 * congruence)
        if is_negative(etype) and self._energy_at(self.t or self.k.clock.now()) < 0.3:
            x *= 1.3  # tired -> more easily annoyed
        return clamp(x, 0.0, cap)

    def _add(self, etype: str, base: float, *, rw: float, cap: float, cause: str, cause_text: str,
             target: Optional[str], target_name: Optional[str], source: str) -> Optional[dict]:
        if etype not in EMOTION_PAD:
            return None
        x = self._modulated(etype, base, rw, cap)
        if x < REMOVE_BELOW:
            return None
        now = self.t or self.k.clock.now()
        for e in self.emotions:
            if e.type == etype and e.target == target and e.cause == cause:
                merged = min(max(cap, e.intensity), 1 - (1 - e.intensity) * (1 - x))
                e.intensity = max(e.intensity, merged)
                e.initial = max(e.initial, e.intensity)
                e.updated_at = now
                if cause_text and not e.cause_text:
                    e.cause_text = cause_text
                self._salient(x)
                return e.to_dict()
        e = Emotion(id=new_id("emo"), type=etype, intensity=x, initial=x, born_at=now, updated_at=now,
                    half_life_h=self.half_lives.get(etype, 2.0), target=target, target_name=target_name,
                    cause=cause or "", cause_text=(cause_text or "")[:60], source=source)
        self.emotions.append(e)
        if len(self.emotions) > MAX_ACTIVE:
            self.emotions.sort(key=lambda z: -z.intensity)
            del self.emotions[MAX_ACTIVE:]
        self.counters["emotions"] += 1
        self._salient(x)
        return e.to_dict()

    def _salient(self, strength: float) -> None:
        self.boredom *= max(0.0, 1.0 - min(1.0, 2.0 * strength))

    def note_salient(self, strength: float = 0.5) -> None:
        """Something noteworthy happened (no emotion needed): relieves boredom. Extra public method."""
        self._sync()
        self._salient(clamp(strength, 0.0, 1.0))

    def bump_stress(self, amount: float, reason: str = "") -> None:
        """Transient stress (attack, throttling, overload). Extra public method."""
        self._sync()
        self.stress = clamp(self.stress + amount, 0.0, 1.0)
        log.debug("stress %+0.2f (%s) -> %.2f", amount, reason, self.stress)

    def _emit_delta(self, created: List[dict], source: str, cause_event_id: str, divergence: list,
                    op: str = "create") -> None:
        if not created and not divergence:
            return
        self._wb.mark()
        self.bg.emit(self.k.bus, EV.AFFECT_DELTA,
                     {"op": op, "source": source, "emotions": created, "divergence": divergence,
                      "mood": [round(x, 3) for x in self.mood]},
                     source="affect", causal_parent=cause_event_id or None)

    def appraise_quick(self, signals: dict, cause_event_id: str = "", target: Optional[str] = None) -> list:
        self._sync()
        signals = signals or {}
        rw = signals.get("relationship_weight", 0.5)
        try:
            rw = float(rw)
        except (TypeError, ValueError):
            rw = 0.5
        name = signals.get("target_name")
        created = []
        for etype, base, cause_text in ap.quick_rules(signals):
            d = self._add(etype, base, rw=rw, cap=ap.QUICK_CAP, cause=cause_event_id,
                          cause_text=signals.get("cause_text") or cause_text, target=target, target_name=name,
                          source="quick")
            if d:
                created.append(d)
        if signals.get("insulted"):
            self.stress = clamp(self.stress + 0.05, 0.0, 1.0)
        if signals.get("threatened"):
            self.stress = clamp(self.stress + 0.1, 0.0, 1.0)
        self._emit_delta(created, "quick", cause_event_id, [])
        return created

    def _prior_prospect(self, target: Optional[str], cause_text: str):
        def find() -> Optional[str]:
            best = None
            for e in self.emotions:
                if e.type not in ("hope", "anxiety", "fear"):
                    continue
                if (target and e.target == target) or (cause_text and e.cause_text and
                                                        (cause_text in e.cause_text or e.cause_text in cause_text)):
                    if best is None or e.intensity > best.intensity:
                        best = e
            if best is None:
                return None
            best.intensity *= 0.3  # the prospect resolves into its confirmation emotion
            return "hoped" if best.type == "hope" else "feared"
        return find

    def apply_deltas(self, deltas: Sequence[dict], cause_event_id: str = "", target_weight: float = 0.5) -> list:
        """Each delta: explicit {"type"|"emotion", "intensity", "target", "cause"} and/or appraisal variables
        (see appraisal.occ_rules). With both, the deterministic OCC rule result wins and the disagreement
        is recorded (``divergences``, ``aff.delta`` payload)."""
        self._sync()
        created: List[dict] = []
        divergence: List[dict] = []
        for d in deltas or []:
            if not isinstance(d, dict):
                continue
            target = d.get("target")
            target = str(target) if target not in (None, "") else None
            cause_text = str(d.get("cause") or d.get("cause_text") or "")[:60]
            rw = d.get("relationship_weight", target_weight)
            try:
                rw = float(rw)
            except (TypeError, ValueError):
                rw = 0.5
            llm_type = ap.explicit_type(d)
            raw_type = d.get("type") or d.get("emotion")
            try:
                llm_int = clamp(float(d.get("intensity", 0.5)), 0.0, 1.0)
            except (TypeError, ValueError):
                llm_int = 0.5
            proposals: List[Tuple[str, float, str]] = []
            if ap.has_appraisal(d):
                proposals = ap.occ_rules(d, self._prior_prospect(target, cause_text))
                rule_types = [p[0] for p in proposals]
                if raw_type and llm_type not in rule_types:
                    div = {"llm": raw_type, "rule": rule_types[0] if rule_types else "", "cause": cause_text}
                    divergence.append(div)
                    self.divergences.append(div)
                    self.counters["divergences"] += 1
                    log.debug("appraisal divergence: %s", div)
                    if not proposals and llm_type:  # rules say neutral: keep the LLM view, damped
                        proposals = [(llm_type, llm_int * 0.5, "llm:rule_neutral")]
                elif raw_type and llm_type in rule_types:
                    # agreement: use the LLM's intensity as base for the matching proposal
                    proposals = [(t, llm_int if t == llm_type else i, n) for t, i, n in proposals]
            elif llm_type:
                proposals = [(llm_type, llm_int, "llm")]
            elif raw_type:
                div = {"llm": raw_type, "rule": "", "cause": cause_text, "note": "unknown_type"}
                divergence.append(div)
                self.divergences.append(div)
                self.counters["divergences"] += 1
            for etype, base, _note in proposals:
                e = self._add(etype, base, rw=rw, cap=1.0, cause=cause_event_id, cause_text=cause_text,
                              target=target, target_name=d.get("target_name"), source="deep")
                if e:
                    created.append(e)
        self._emit_delta(created, "deep", cause_event_id, divergence)
        return created

    # need -> emotion coupling (docs/04 §3.4)
    _NEED_POS = {"social": ("joy", "和人有了连接"), "competence": ("pride", "做成了一件事"),
                 "curiosity": ("interest", "看到了新东西"), "autonomy": ("satisfaction", "做了自己想做的事"),
                 "coherence": ("relief", "想通了一些事"), "safety": ("relief", "感觉安全了"),
                 "rest": ("satisfaction", "休息好了")}
    _NEED_NEG = {"social": ("sadness", "有点被冷落"), "competence": ("disappointment", "事情没做好"),
                 "safety": ("fear", "感觉被冒犯或威胁"), "autonomy": ("irritation", "一直在被别人的事推着走"),
                 "coherence": ("shame", "被指出前后矛盾"), "curiosity": ("disappointment", "日子有点重复"),
                 "rest": ("irritation", "太累了")}

    def on_need_event(self, need: str, delta: float) -> None:
        self._sync()
        try:
            delta = float(delta)
        except (TypeError, ValueError):
            return
        if abs(delta) < 0.03:
            return
        table = self._NEED_POS if delta > 0 else self._NEED_NEG
        if need not in table:
            return
        etype, cause_text = table[need]
        if need == "social" and delta > 0 and delta >= 0.15:
            etype = "gratitude" if delta >= 0.25 else "joy"
        if need == "safety" and delta < 0 and delta <= -0.2:
            etype = "anger"
        base = min(0.5, abs(delta) * 1.5)
        created = []
        e = self._add(etype, base, rw=0.5, cap=0.6, cause=f"need:{need}", cause_text=cause_text, target=None,
                      target_name=None, source="need")
        if e:
            created.append(e)
        if need == "safety" and delta < 0:
            self.stress = clamp(self.stress + 0.3 * abs(delta), 0.0, 1.0)
        if need == "competence" and delta > 0:
            self.stress = clamp(self.stress - 0.2 * delta, 0.0, 1.0)
        self._emit_delta(created, "need", "", [])

    # ------------------------------------------------------------------ social, rumination, regulation
    def social_spend(self, amount: float, *, closeness: Optional[float] = None) -> None:
        """An interaction happened. Cost = amount x extraversion factor x per-person cost (1 - c E) relative to an
        acquaintance (c from the caller, else the people currently in my social field) / today's sociability."""
        self._sync()
        try:
            amount = max(0.0, float(amount))
        except (TypeError, ValueError):
            return
        E = self.big5["E"]
        if closeness is None:
            now = self.t or self.k.clock.now()
            ws = [(p["w"] * math.exp(-max(0.0, now - p["t"]) / SOCIAL_TAU_S), p["c"]) for p in self._people.values()]
            tot = sum(w for w, _ in ws)
            closeness = sum(w * c for w, c in ws) / tot if tot > 1e-6 else SOCIAL_REF_CLOSENESS
        kappa = (1.0 - clamp(float(closeness), 0.0, 1.0) * E) / max(1e-6, 1.0 - SOCIAL_REF_CLOSENESS * E)
        cost = amount * (0.6 + 0.8 * (1 - E)) * kappa / self._tex_mult("sociability", 1.0)
        self.battery = clamp(self.battery - cost, 0.0, 1.0)
        self._salient(min(0.5, amount * 3))

    def ruminate(self, cause_event_id: str, relevance: float) -> None:
        """+0.3 x initial x relevance (diminishing with repeats), capped at the initial intensity."""
        self._sync()
        r = clamp(float(relevance or 0.0), 0.0, 1.0)
        for e in self.emotions:
            if cause_event_id and (e.cause == cause_event_id or e.id == cause_event_id):
                bump = 0.3 * e.initial * r * (0.8 ** e.ruminations)
                e.intensity = min(e.initial, e.intensity + bump)
                e.ruminations += 1

    def _strategy(self, reason: str) -> List[str]:
        style = {"reappraise": "reappraise", "express": "express", "suppress": "suppress",
                 "mixed": "reappraise"}.get(self.regulation_style, "reappraise")
        if reason == "negative_mood_48h":
            return [style, "distract"]
        if reason == "high_negative_arousal":
            return ["reappraise", "calm"]
        return [style]

    REG_GRACE_S = 120.0

    def regulation_needed(self) -> Optional[dict]:
        """Triggers (docs/04 §5): a negative emotion > 0.7; negative mood > 48 h; a desperate/panic-like
        high-arousal negative state.

        Idempotent for check-then-act callers: repeated calls within ``REG_GRACE_S`` of the first report
        return the same trigger. After that (or once ``reappraise`` handled the emotion) the trigger is
        quiet until ``regulation_cooldown_s`` has passed since it was first reported. The returned
        emotion's ``cause`` falls back to its id, so ``reappraise(emotion["cause"], f)`` always matches."""
        self._sync()
        now = self.t or self.k.clock.now()
        negs = sorted((e for e in self.emotions if is_negative(e.type)), key=lambda z: -z.intensity)
        cands: List[Tuple[str, str, Optional[Emotion]]] = []
        if negs and negs[0].intensity > 0.7:
            cands.append((f"strong:{negs[0].id}", "strong_negative_emotion", negs[0]))
        hot = [e for e in negs if e.type in HIGH_AROUSAL_NEGATIVE]
        if self.high_negative_arousal() and (hot or self.mood[0] <= -0.25):
            cands.append(("panic", "high_negative_arousal", hot[0] if hot else (negs[0] if negs else None)))
        if self.neg_since is not None and now - self.neg_since >= self.lockin_after_s:
            cands.append(("neg_mood", "negative_mood_48h", negs[0] if negs else None))
        for key, reason, e in cands:
            age = now - self._reg_cooldown.get(key, -1e18)
            if age >= self.regulation_cooldown_s:
                self._reg_cooldown[key] = now
                self.counters["regulations"] += 1
            elif age >= self.REG_GRACE_S:
                continue
            ed = e.to_dict() if e else {}
            if ed and not ed.get("cause"):
                ed["cause"] = ed["id"]
            return {"emotion": ed, "reason": reason, "strategy": self._strategy(reason),
                    "mood": [round(x, 3) for x in self.mood], "key": key}
        return None

    def _regulation_handled(self, now: float) -> None:
        """A reappraisal happened: triggers inside their grace window go quiet for the rest of the cooldown."""
        for key, t0 in list(self._reg_cooldown.items()):
            if now - t0 < self.REG_GRACE_S:
                self._reg_cooldown[key] = now - self.REG_GRACE_S

    def reappraise(self, emotion_cause: str, factor: float) -> None:
        """Scale emotions matched by cause event id, emotion id, cause text or target."""
        self._sync()
        f = clamp(float(factor), 0.0, 1.5)
        changed = []
        for e in self.emotions:
            if emotion_cause and emotion_cause in (e.cause, e.id, e.cause_text, e.target):
                e.intensity = clamp(e.intensity * f, 0.0, 1.0)
                if f < 1:
                    e.initial = min(e.initial, max(e.intensity, REMOVE_BELOW))
                e.updated_at = self.t or self.k.clock.now()
                changed.append({"id": e.id, "type": e.type, "intensity": round(e.intensity, 4)})
        self.emotions = [e for e in self.emotions if e.intensity >= REMOVE_BELOW]
        if changed:
            self._regulation_handled(self.t or self.k.clock.now())
            self._emit_delta(changed, "reappraisal", "", [], op="reappraise")

    async def regulate(self, trigger: Optional[dict] = None) -> Optional[dict]:
        """LLM-assisted reappraisal for a regulation trigger (fast tier, purpose ``aff.reappraise``).
        Extra public method for metacognition. Falls back to a mild deterministic reappraisal."""
        trig = trigger or self.regulation_needed()
        if not trig:
            return None
        emo = trig.get("emotion") or {}
        lab = self.labels()
        text, factor = "也许没我想的那么糟，先缓一缓。", 0.85
        if emo:
            try:
                data = await self.k.llm.json(
                    "aff.reappraise",
                    REAPPRAISE.format(mood=lab["mood"], emotion_line=lab["emotion"],
                                      cause=emo.get("cause_text") or "（说不清的原因）", etype=emo.get("type"),
                                      intensity=float(emo.get("intensity", 0.0)), reason=trig.get("reason", "")),
                    tier="fast", system=REAPPRAISE_SYSTEM, schema=REAPPRAISE_SCHEMA)
                text = str(data.get("reappraisal") or text)[:120]
                factor = clamp(float(data.get("factor", factor)), 0.3, 1.0)
            except Exception as e:  # BudgetDenied, LLMError, bad output
                log.info("reappraisal LLM unavailable (%r); using fallback", e)
            self.reappraise(emo.get("id", ""), factor)
        if trig.get("reason") == "negative_mood_48h":
            self.anti_lockin = True
        return {"trigger": trig, "reappraisal": text, "factor": factor}

    # ------------------------------------------------------------------ router hook (docs/10 §4.2)
    def route_hook(self, req, tier: str) -> str:
        """Tired -> think less deeply: deep -> deliberate when energy < threshold and awake.
        Registered on the ModelRouter at start (config affect.energy_route_hook)."""
        if tier != "deep" or self.asleep:
            return tier
        try:
            if self.energy() < float(self.cfg.get("energy_route_threshold", 0.3)):
                return "deliberate"
        except Exception:  # pragma: no cover
            pass
        return tier

    def llm_state(self) -> dict:
        """State the model router turns into a per-request sampling temperature for composition: today's
        playfulness (texture value, 0..1) and arousal relative to my baseline mood."""
        out: dict = {}
        tex = getattr(self.k, "texture", None)
        if tex is not None:
            try:
                out["playfulness"] = float(tex.value("playfulness"))
            except Exception:
                pass
        try:
            self._sync()
            out["arousal"] = float(self.mood[1] - self.default[1])
        except Exception:  # pragma: no cover
            pass
        return out
