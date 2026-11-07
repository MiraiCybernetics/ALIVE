"""Attention and pace (读空气, docs/07 §3-4, ADR-0007): WHEN I actually look at a space, and how fast I go once I do.

A person in IM is not a reply server. Most of the time they are not looking at the chat: a message waits until they
next pick the phone up, unless the notification itself pulls them in. Once they are "in" a space they answer within
seconds, and they stay a while after they speak, waiting for the answer with the chat open. That is where the
characteristic rhythm of IM comes from -- quick back-and-forth bursts separated by long, heavy-tailed gaps
(Barabási 2005; Wu et al. 2010) -- and none of it is a rule about channels or a fixed delay.

Attention, per space (all draws from the life's RNG; priors in the research note air_reading_timing_priors.md):
    attending      after I look I linger  ~ LogNormal(median 60 s × f, 0.7); after I speak ~ LogNormal(4 min × f, 0.7)
                   f = (0.6 + 0.8·closeness)(0.7 + 0.6·interest)(0.5 + 0.5·battery)·sociability(texture)
                   a line arriving while I'm attending is seen at once (only the debounce applies)
    not attending  time until I deal with it ~ mixture of
                     at hand   w = 0.22·handiness·(0.5 + pull)       LogNormal(20 s, 0.8)
                     next look w = 1                                 LogNormal(m, 1.0)
                     away      w = 0.10·busy                         LogNormal(75 min, 0.8)
                   m for a line to me = priority by closeness (in a shared space: the sooner of that and my next
                     look at the space): ~4.5 min for the closest, ~16 min for good friends,
                     ~1 h casual, ~2-3 h weak ties (Martin et al. 2026: 69% vs 44% within 5 min by tie strength;
                     Pielot et al. 2014: median 6.15 min to attend a message; priority queueing, Barabási 2005)
                     m = 270 s · exp(4.3·(0.9 − closeness)·load) -- one-to-one; load ∈ 0.3..1 is how much else
                     competes for me (mode + open demands): the research medians are for full lives (load ~1)  an ambient line in a crowd: 6 min / (0.4 + pull)
                   × mode (active 0.6 … focus 2.0)  × (1 − 0.35·social need): wanting company, I check more often
                   mirror (Communication Accommodation; partners' speeds correlate, slope ~0.8): the log-median
                     moves halfway toward their own median latency to me (geometric mean)
    pull           how much a line draws me in: lines to me (@me, reply to me, vocative, or the only person
                   there) pull; ambient lines in a crowd hardly do -- a fact about addressedness, not a channel rule
    notifications  every later line while I haven't looked is another chance to be pulled in:
                   q = pull·(0.06 + 0.30·closeness) + 0.25·urgency_marks + 0.15·[3rd+ line in a row from them]
                   if it catches me I look within LogNormal(8 s, 0.6)
    asleep         I look after the sampled wake time + LogNormal(20 min, 0.8)/energy, unless a notification wakes
                   me: affect.wake_probability(salience) per line
"""
from __future__ import annotations

import math
import re
from typing import Optional

from alive.runtime import chance as C

MODE_CHECK = {"active": 0.6, "idle": 1.0, "focus": 2.0, "overwhelmed": 1.6, "drowsy": 1.5, "asleep": 6.0}
# how much else competes for me (priority-queue load): with little to do, low-priority lines don't wait long
MODE_LOAD = {"active": 0.6, "idle": 0.4, "focus": 1.0, "overwhelmed": 1.0, "drowsy": 0.6, "asleep": 1.0}
MODE_HANDY = {"active": 1.0, "idle": 0.7, "focus": 0.25, "overwhelmed": 0.5, "drowsy": 0.5, "asleep": 0.0}
MODE_BUSY = {"active": 0.4, "idle": 0.8, "focus": 2.5, "overwhelmed": 1.5, "drowsy": 1.0, "asleep": 0.0}

_URGENT = re.compile(r"急|快|救命|在吗|在不在|出事|马上|赶紧|速|！！|!!|\?\?|？？|help|urgent|asap", re.I)


class Attention:
    def __init__(self, mgr):
        self.mgr = mgr
        self.k = mgr.k
        c = mgr.cfg.section("attention") if hasattr(mgr.cfg, "section") else {}
        self.enabled = bool(c.get("enabled", True))
        self.linger_look_s = float(c.get("linger_look_s", 60.0))
        self.linger_spoke_s = float(c.get("linger_spoke_s", 240.0))
        self.check_median_s = float(c.get("check_median_s", 360.0))
        self.priority_median_s = float(c.get("priority_median_s", 270.0))
        self.away_median_s = float(c.get("away_median_s", 4500.0))

    def rng(self):
        return C.rng_of(self.k)

    # ------------------------------------------------------------------ state probes
    def _snap(self):
        try:
            return self.k.affect.snapshot() if self.k.affect is not None else None
        except Exception:
            return None

    def _texture(self, trait: str, strength: float = 0.8) -> float:
        tex = getattr(self.k, "texture", None)
        try:
            return float(tex.multiplier(trait, strength)) if tex is not None else 1.0
        except Exception:
            return 1.0

    def _closeness(self, pk: Optional[str], now: float) -> float:
        if not pk:
            return 0.2
        try:
            r = self.mgr.relations.peek_rel(pk)
            return float(self.mgr.relations.closeness_at(r, now)) if r is not None else 0.1
        except Exception:
            return 0.2

    def _their_latency(self, pk: Optional[str]) -> Optional[float]:
        if not pk:
            return None
        try:
            b = self.mgr._baseline(pk)
            return b.latency_median if b is not None and getattr(b, "latency_median", None) else None
        except Exception:
            return None

    def _load(self, mode: str) -> float:
        demands = 0
        fn = getattr(getattr(self.k, "mind", None), "open_demands", None)
        if fn is not None:
            try:
                demands = int(fn())
            except Exception:
                demands = 0
        return C.clamp(MODE_LOAD.get(mode, 0.6) + 0.08 * demands, 0.3, 1.0)

    def _social_need(self) -> float:
        try:
            needs = self.k.motivation.needs() if self.k.motivation is not None else {}
            return C.clamp(float((needs.get("social") or {}).get("pressure", 0.0)))
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ attention
    def attending(self, st, now: float) -> bool:
        return float(getattr(st, "attend_until", 0.0) or 0.0) > now

    def linger(self, st, now: float, *, spoke: bool, person_key: Optional[str] = None, interest: float = 0.3) -> float:
        """After I look at (or speak in) a space I stay with it for a while. Returns the sampled duration."""
        snap = self._snap()
        battery = float(getattr(snap, "social_battery", 0.7)) if snap is not None else 0.7
        clo = self._closeness(person_key, now)
        f = (0.6 + 0.8 * clo) * (0.7 + 0.6 * C.clamp(interest)) * (0.5 + 0.5 * C.clamp(battery))
        f *= self._texture("sociability", 0.8)
        med = (self.linger_spoke_s if spoke else self.linger_look_s) * f
        d = C.lognormal(self.rng(), med, 0.7)
        st.attend_until = max(float(getattr(st, "attend_until", 0.0) or 0.0), now + d)
        return d

    def pull(self, st, item, now: float) -> float:
        """0..1: how much this line draws me in by itself (addressedness and who it's from)."""
        to_me = 1.0 if (item.addressed or self.mgr.one_to_one(st)) else 0.0
        clo = self._closeness(item.msg.person_key, now)
        return C.clamp(0.12 + 0.88 * to_me) * (0.7 + 0.3 * clo) if to_me else 0.08 + 0.25 * clo

    def _salience(self, st, item, now: float) -> float:
        urgent = 1.0 if _URGENT.search(item.msg.text or "") else 0.0
        return C.clamp(0.6 * self.pull(st, item, now) + 0.25 * urgent + 0.15 * self._closeness(item.msg.person_key, now))

    def notice_delay(self, st, item, now: float) -> float:
        """Seconds until I look at the space this line arrived in (0 if I'm already there)."""
        if not self.enabled or self.attending(st, now):
            return 0.0
        rng = self.rng()
        snap = self._snap()
        if snap is not None and getattr(snap, "asleep", False):
            return self._asleep_delay(st, item, now, snap)
        mode = self.mgr.mind_mode()
        pull = self.pull(st, item, now)
        handy = MODE_HANDY.get(mode, 0.7)
        busy = MODE_BUSY.get(mode, 0.8) * (2.0 - self._texture("focus", 0.6))
        to_me = bool(item.addressed or self.mgr.one_to_one(st))
        if to_me:     # a line to me: how soon I deal with it is a priority, and priority follows the tie --
            # but only as strongly as other things compete for me (Barabási 2005: in a lightly loaded queue even
            # low-priority tasks are done soon). The research medians come from people with full lives (load ~1).
            load = self._load(mode)
            m = self.priority_median_s * math.exp(4.3 * (0.9 - self._closeness(item.msg.person_key, now)) * load)
        else:         # ambient lines in a shared space: I see them when I next look at the space
            m = self.check_median_s / (0.4 + pull)
        m *= MODE_CHECK.get(mode, 1.0) * (1.0 - 0.35 * self._social_need())
        lat = self._their_latency(item.msg.person_key) if to_me else None
        if lat:
            m = math.exp(0.5 * math.log(m) + 0.5 * math.log(max(5.0, lat)))
        mode_k = MODE_CHECK.get(mode, 1.0) * (1.0 - 0.35 * self._social_need())

        def draw(median: float) -> float:
            v, _ = C.lognormal_mixture(rng, [(0.22 * handy * (0.5 + pull), 20.0, 0.8, "hand"),
                                             (1.0, median, 1.0, "check"),
                                             (0.10 * busy, self.away_median_s, 0.8, "away")])
            return v
        d = draw(m)
        if to_me and not self.mgr.one_to_one(st):
            # a line to me in a shared space: I also see it whenever I next look at the space anyway
            d = min(d, draw(self.check_median_s / (0.4 + pull) * mode_k))
        return d

    def _asleep_delay(self, st, item, now: float, snap) -> float:
        rng = self.rng()
        wp = getattr(self.k.affect, "wake_probability", None)
        if wp is not None:
            try:
                if C.bernoulli(rng, float(wp(self._salience(st, item, now)))):
                    return C.lognormal(rng, 25.0, 0.7)
            except Exception:
                pass
        wake = None
        ew = getattr(self.k.affect, "expected_wake", None)
        if ew is not None:
            try:
                wake = ew(now)
            except Exception:
                wake = None
        energy = float(getattr(snap, "energy", 0.6) or 0.6)
        catch_up = C.lognormal(rng, 1200.0, 0.8) / max(0.3, energy)
        base = (wake - now) if wake and wake > now else C.lognormal(rng, 3 * 3600.0, 0.5)
        return base + catch_up

    def catches(self, st, item, now: float, run_len: int = 1) -> Optional[float]:
        """A later line while I haven't looked yet: does its notification pull me in? Returns the delay or None."""
        if not self.enabled:
            return 0.0
        rng = self.rng()
        snap = self._snap()
        if snap is not None and getattr(snap, "asleep", False):
            wp = getattr(self.k.affect, "wake_probability", None)
            try:
                if wp is not None and C.bernoulli(rng, float(wp(self._salience(st, item, now)))):
                    return C.lognormal(rng, 25.0, 0.7)
            except Exception:
                pass
            return None
        mode = self.mgr.mind_mode()
        urgent = 1.0 if _URGENT.search(item.msg.text or "") else 0.0
        q = self.pull(st, item, now) * (0.06 + 0.30 * self._closeness(item.msg.person_key, now))
        q += 0.25 * urgent + (0.15 if run_len >= 3 else 0.0)
        q *= MODE_HANDY.get(mode, 0.7) ** 0.5
        return C.lognormal(rng, 8.0, 0.6) if C.bernoulli(rng, q) else None


# ---------------------------------------------------------------------------------------------- the burst a line ends
def line_heat(text: str) -> float:
    """0..1: how heated a line reads (exclamation/question runs, insults/conflict words)."""
    t = text or ""
    marks = len(re.findall(r"[!！?？]{2,}", t))
    words = len(re.findall(r"凭什么|有病|闭嘴|滚|傻|你才|吵|骂|气死|离谱|无语|服了|垃圾|恶心", t))
    return C.clamp(0.35 * marks + 0.4 * words)


def ack_only(items) -> bool:
    from alive.social import text as T
    return bool(items) and all(i.msg.media_only or T.is_trivial_ack(i.msg.text or "") or T.is_emoji_only(i.msg.text or "")
                               for i in items)


def heat_of(items) -> float:
    return max((line_heat(i.msg.text or "") for i in items), default=0.0)

