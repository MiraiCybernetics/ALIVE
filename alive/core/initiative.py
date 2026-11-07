"""Initiative propensity (docs/05 §5.6, docs/21 §5): when do I speak, reach out or act on my own?

There are no quotas ("n times per person per day", "at least 4 h apart", "one message in a group"). Behaviour of
that kind is the RESULT of state, not a rule. Each opportunity to act on my own is a draw from a state-dependent
point process: an intensity (per hour) built from smooth factors, turned into a probability for the time that
passed since this entity was last considered, and sampled from the life texture's persisted RNG. So there is
tendency (temperament, today's texture, relationship) and variation (mood, energy, chance), never a fixed count.

Factors for speaking unprompted toward an entity (a person, or a shared space). Priors from the research note
research_notes/拟人自主AI框架技术调研/air_reading_timing_priors.md (§4, §6, 建模建议 d-f):
    reason        how strong the reason is (urge pressure x content value, workspace salience, intention)
    me            today's texture (initiative / chattiness), mood, energy, social battery
                  mood: positive affect -> more initiating overall; LOW mood turns me toward close ties (x1.2-1.5)
                  and away from weak ones (x0.6-0.8)
    them          how likely they are around now (their hourly rhythm) / how lively the space is
    bond          Dunbar layers: contact rates fall roughly geometrically from the innermost layer (~1-4 per day)
                  to the 150-layer (~0.001-0.005 per day): exp(5 (closeness - 0.9) (1 - 0.5 reason)) -- with the
                  reasons themselves rarer for distant people this gives roughly that gradient; a strong reason
                  flattens it, and a commitment (promise) keeps a floor: I keep my word to acquaintances too.
                  x (1 + 0.3 reciprocity)
    reception     how my earlier initiatives were received (Beta posterior; ignored ones lower it smoothly)
    refractory    1 - exp(-time since I last spoke to them unprompted / tau); tau shorter for close people
    unanswered    my own messages there still unanswered (double-texting inhibition), exp(-0.9 n): x0.4 / x0.17
    momentum      self-excitation (Hawkes): having just been social somewhere makes reaching out more likely for a
                  while (1 + 0.3 exp(-t / 20 min)); bursts and long lulls instead of a Poisson drizzle
    night         hour-of-day profile of messaging (Rosenfeld et al.: ~4.7% of messages 0-4h, ~2.4% 4-8h, ~18% 8-12h,
                  peak ~1.9x mean around 21h), shifted with my own day (today's wake time), flattened by closeness
    wellbeing     (1 - dependency risk)^2 from governance (docs/09 §7.5)
Safety and legality are NOT here (kill switch, pairing, opt-out, crisis, budget, legally required disclosures):
governance keeps those, plus a malfunction fuse that trips only on bursts no person would produce.
"""
from __future__ import annotations

import math
import random
from typing import Optional

from alive.runtime import chance as C

KV_NS = "initiative"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# relative messaging activity by local hour (mean ~1), from the research note's time-of-day shares
HOUR_PROFILE = [0.55, 0.35, 0.20, 0.12, 0.08, 0.08, 0.15, 0.35, 0.70, 1.00, 1.15, 1.25,
                1.30, 1.25, 1.30, 1.35, 1.40, 1.45, 1.50, 1.65, 1.80, 1.90, 1.70, 1.10]


def hour_profile(hour: float) -> float:
    """Piecewise-linear interpolation of HOUR_PROFILE (continuous, periodic)."""
    h = hour % 24.0
    i = int(h)
    frac = h - i
    return HOUR_PROFILE[i] * (1 - frac) + HOUR_PROFILE[(i + 1) % 24] * frac


def night_curve(hour: float, shift_h: float = 0.0) -> float:
    """Social acceptability / activity of reaching out at a local hour, normalised so the evening peak is 1:
    ~0.06 at 3-5 am, ~0.5 at 9 am, 1 around 21 h. ``shift_h`` moves the profile with my own day (a late riser's
    morning starts later)."""
    return hour_profile(hour - shift_h) / max(HOUR_PROFILE)


class Initiative:
    def __init__(self, kernel):
        self.k = kernel
        self.cfg = kernel.cfg.section("initiative") if hasattr(kernel.cfg, "section") else {}
        self._state: Optional[dict] = None      # entity -> {"last": ts, "eval": ts, "acts": [ts...]}
        self._rng_fallback = random.Random(17)
        self._saved_at = -1e18

    # ------------------------------------------------------------------ persistence
    async def _load(self) -> dict:
        if self._state is None:
            d = None
            try:
                d = await self.k.db.kv_get(KV_NS, "state")
            except Exception:
                d = None
            self._state = d if isinstance(d, dict) else {}
        return self._state

    async def _save(self) -> None:
        if self._state is None:
            return
        if len(self._state) > 3000:
            for key in sorted(self._state, key=lambda x: self._state[x].get("eval", 0))[:500]:
                self._state.pop(key, None)
        try:
            await self.k.db.kv_set(KV_NS, "state", self._state)
            self._saved_at = self.k.clock.now()
        except Exception:
            pass

    def _rng(self) -> random.Random:
        r = C.rng_of(self.k)
        return r if isinstance(r, random.Random) else self._rng_fallback

    # ------------------------------------------------------------------ factors
    def _me(self, shared: bool) -> dict:
        tex = getattr(self.k, "texture", None)
        trait = "chattiness" if shared else "initiative"
        t = tex.multiplier(trait, 0.8) if tex is not None else 1.0
        P, A, energy, battery = 0.0, 0.4, 0.7, 0.8
        try:
            s = self.k.affect.snapshot()
            P, A, energy, battery = float(s.P), float(s.A), float(s.energy), float(s.social_battery)
        except Exception:
            pass
        mood = math.exp(0.5 * P + 0.3 * (A - 0.4))
        return {"texture": round(t, 3), "mood": round(mood, 3), "energy": round(0.3 + 0.7 * _clamp(energy), 3),
                "battery": round(0.4 + 0.6 * _clamp(battery), 3), "_P": P, "_A": A}

    def _shift_h(self) -> float:
        """How far my day is shifted from a 7:30 wake (today's sampled wake time, if the affect system has one)."""
        aff = getattr(self.k, "affect", None)
        for name in ("today_wake_hour", "wake_hour"):
            fn = getattr(aff, name, None) if aff is not None else None
            if fn is None:
                continue
            try:
                h = fn() if callable(fn) else fn
                if h is not None:
                    return max(-4.0, min(4.0, float(h) - 7.5))
            except Exception:
                pass
        return 0.0

    def _momentum(self, now: float) -> float:
        """Self-excitation: I was just social somewhere (sent or got a message) -> a little more likely to reach out."""
        last = 0.0
        soc = getattr(self.k, "social", None)
        try:
            last = float(getattr(soc, "last_social_ts", lambda: 0.0)() or 0.0)
        except Exception:
            last = 0.0
        if not last or now < last:
            return 1.0
        return 1.0 + 0.3 * math.exp(-(now - last) / 1200.0)

    def factors(self, entity: str, *, shared: bool, reason: float, now: float, ctx: Optional[dict] = None,
                st: Optional[dict] = None) -> dict:
        ctx = ctx or {}
        st = st or {}
        f = {"reason": round(_clamp(reason), 3)}
        me = self._me(shared)
        P = float(me.pop("_P", 0.0))
        f.update(me)
        closeness = _clamp(float(ctx.get("closeness", 0.5 if shared else 0.3)))
        A = float(f.pop("_A", 0.4))
        if P < 0 and not shared:      # feeling low: toward close people, away from weak ties
            f["mood"] = round(math.exp(P * (0.5 - 0.9 * closeness) + 0.3 * (A - 0.4)), 3)
        if shared:
            n30 = float(ctx.get("msgs_30min", 0) or 0)
            f["them"] = round(0.3 + 0.7 * (1 - math.exp(-n30 / 4.0)), 3)
            share, fair = float(ctx.get("my_share", 0.0) or 0.0), float(ctx.get("fair_share", 0.0) or 0.0)
            f["floor"] = round(math.exp(-max(0.0, share / fair - 1.0)) if fair > 0 else 1.0, 3)
        else:
            f["them"] = round(0.15 + 0.85 * _clamp(float(ctx.get("active_now_prob", 0.5) or 0.0)), 3)
        recip = max(-1.0, min(1.0, float(ctx.get("reciprocity", 0.0) or 0.0)))
        if shared:
            bond = 0.3 + 0.7 * closeness
        else:
            # a strong reason flattens the gradient: I do message an acquaintance when I really have something for them
            bond = math.exp(5.0 * (closeness - 0.9) * (1.0 - 0.5 * _clamp(reason)))
            bond = max(bond, 0.6 * _clamp(float(ctx.get("commitment", 0.0) or 0.0)))
        f["bond"] = round(bond * (1 + 0.3 * recip), 4)
        warm, flat, ign = (float(ctx.get(x, 0) or 0) for x in ("warm", "flat", "ignored"))
        rec = (warm + 0.5 * flat + 1.0) / (warm + flat + ign + 1.5)
        f["reception"] = round((rec / 0.667) ** 1.5, 3)
        last = st.get("last")
        tau_h = float(self.cfg.get("refractory_h", 3.0)) * (1.4 - closeness) / max(0.3, f["texture"])
        if shared:
            tau_h *= 0.5
        f["refractory"] = round(1 - math.exp(-((now - last) / 3600.0) / tau_h), 3) if last else 1.0
        f["unanswered"] = round(math.exp(-0.9 * float(ctx.get("my_unanswered", 0) or 0)), 3)
        f["momentum"] = round(self._momentum(now), 3)
        try:
            hour = self.k.clock.hour_of_day(now)
        except Exception:
            hour = 15.0
        f["night"] = round(night_curve(hour, self._shift_h()) ** (1 - 0.6 * closeness), 3)
        damp = 1.0
        if not shared and entity:
            try:
                damp = (1 - _clamp(float(self.k.gov.dependency_risk(entity)))) ** 2
            except Exception:
                damp = 1.0
        f["wellbeing"] = round(damp, 3)
        return f

    def intensity(self, f: dict) -> float:
        lam = float(self.cfg.get("base_per_h", 1.2))
        for v in f.values():
            lam *= float(v)
        return lam

    # ------------------------------------------------------------------ decide
    async def decide(self, entity: str, *, shared: bool, reason: float, ctx: Optional[dict] = None,
                     now: Optional[float] = None, rng=None) -> tuple:
        """(act?, p, factors). Each call is one opportunity; p covers the time since this entity was last
        considered, so the outcome doesn't depend on how often the caller happens to ask."""
        now = self.k.clock.now() if now is None else now
        state = await self._load()
        st = state.setdefault(entity, {})
        f = self.factors(entity, shared=shared, reason=reason, now=now, ctx=ctx, st=st)
        lam = self.intensity(f)
        dt_h = min(3.0, max(0.0005, (now - st["eval"]) / 3600.0)) if st.get("eval") else 1.0
        p = 1 - math.exp(-lam * dt_h)
        st["eval"] = now
        clock = C.HazardClock.from_dict(st.get("hz"))     # time rescaling: same law as Bernoulli(p) per call
        ok = clock.step(rng or self._rng(), lam, dt_h)
        st["hz"] = clock.to_dict()
        f["lambda_per_h"] = round(lam, 4)
        if now - self._saved_at > 60:            # evaluation stamps are cheap to lose; spoken acts save at once
            await self._save()
        return ok, round(p, 4), f

    async def note_spoke(self, entity: str, now: Optional[float] = None) -> None:
        now = self.k.clock.now() if now is None else now
        state = await self._load()
        st = state.setdefault(entity, {})
        st["last"] = now
        st["acts"] = ([t for t in st.get("acts", []) if now - t < 7 * 86400] + [now])[-50:]
        await self._save()

    def last_spoke(self, entity: str) -> Optional[float]:
        return (self._state or {}).get(entity, {}).get("last")

    # ------------------------------------------------------------------ how much to say, when to yield
    def message_count(self, *, shared: bool, mentioned: bool, upper: int, floor_ratio: float = 1.0,
                      their_burst: Optional[float] = None) -> int:
        """How many messages I send in one go: 1 + a geometric run (research note 建模建议 c: continuation ~0.3,
        i.e. ~70% one message, ~21% two, ~9% three or more), with a rare 'flood' mode (~3%: 3 + Poisson(2)) when
        I'm excited. The continuation follows today's chattiness, arousal and energy; in a shared space it shrinks
        with how much of the floor I already hold (unless I'm the one addressed); and it moves halfway toward the
        other person's own style (accommodation: someone who sends bursts gets bursts back). No fixed '1 in a
        group'; ``upper`` is only a technical ceiling."""
        me = self._me(shared)
        me.pop("_P", None)
        me.pop("_A", None)
        q = 0.3 * me["texture"] * (0.6 + 0.8 * min(1.0, max(0.0, me["mood"] - 0.5))) * me["energy"]
        if shared:
            q *= (0.6 if not mentioned else 0.9) * math.exp(-max(0.0, floor_ratio - 1.0))
        if their_burst and their_burst > 0:
            theirs = 1.0 - 1.0 / max(1.0, float(their_burst))         # geometric continuation implied by their mean
            q = 0.5 * q + 0.5 * theirs
        q = _clamp(q, 0.0, 0.85)
        r = self._rng()
        excited = max(0.0, me["mood"] - 1.0)
        if r.random() < 0.03 * (0.5 + excited) * me["texture"] and not (shared and floor_ratio > 1.5):
            n = 2 + _poisson(r, 2.0)
        else:
            n = 0
            while n < 50 and r.random() < q:
                n += 1
        return max(1, min(int(upper), 1 + n))

    def yield_floor(self, *, my_share: float, fair_share: float, msgs_30min: float, mentioned: bool) -> bool:
        """Let others talk when I hold well over my fair share of a lively space (sampled, smooth). Being addressed
        makes it much less likely, not impossible (someone hogging the floor may still let a question pass)."""
        if fair_share <= 0:
            return False
        ratio = my_share / fair_share
        lively = 1 - math.exp(-msgs_30min / 5.0)
        p = lively / (1 + math.exp(-3.0 * (ratio - 1.6)))
        if mentioned:
            p *= 0.15
        return self._rng().random() < p


def _poisson(r: random.Random, lam: float) -> int:
    return C.poisson(r, lam)


def get_initiative(kernel) -> Initiative:
    ini = getattr(kernel, "_initiative", None)
    if ini is None:
        ini = Initiative(kernel)
        try:
            setattr(kernel, "_initiative", ini)
        except Exception:
            pass
    return ini
