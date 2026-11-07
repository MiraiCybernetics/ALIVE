"""Engagement decision (docs/07 §3, ADR-0007): once I look at a space, do I look properly, glance, or just take it in?

One model for every space. A one-to-one space is a space with one other person in it, where every line is addressed
to me; a busy group is a space with many people, most lines addressed to nobody in particular. The differences a
person shows between the two follow from those FACTS -- audience size, addressedness, liveliness, who is talking to
whom -- through the same terms, never from rules keyed on the space type (INV-28, INV-31).

There is no gate that keeps my reply count near a target, no hard "@ -> must", no hard "talking to someone else ->
silent". Each look is one draw on a logit (from the life's RNG). The result decides how much attention the batch
gets, not whether I speak: a deliberate look can still end in silence (the mind decides), and what I only take in
stays perceived (workspace) and may be picked up later.

Logit = MaiBot terms + ALIVE state terms + air-reading terms (centred so an "average" situation adds ~0).

MaiBot logistic regression (reply_likelihood.py, fitted on group rounds "new external messages, bot not @"):
    logit = -1.2005 + 1.5852·mentions_name - 1.1157·at_others + 0.4807·has_question - 1.0281·all_media
            + 2.6525·bot_share_5min - 0.0367·msgs_5min + 0.0571·ln(1+secs_since_bot) + 0.1012·min(pending,20)

ALIVE individual-state terms (docs/07 OQ-29; smooth, no knees):
    closeness  +1.0 × (closeness − 0.3)        interest  +1.5 × topic similarity       social_need  +0.8 × pressure
    low_battery −1.2 × smooth deficit below ~0.35   low_energy −0.8 × smooth deficit below ~0.3
    mode_focus −1.0   mode_drowsy −0.8   arousal +0.4 A   pleasure +0.2 P   willingness +1.0 × modulators["willingness"]

Air-reading terms (priors; research_notes/拟人自主AI框架技术调研/air_reading_timing_priors.md; refit from logs):
    addressed        +3.2   the line is to me (@me, a reply to me, my name as vocative, or I'm the only one there):
                            responsibility is mine (diffusion of responsibility only applies when it is shared)
    question_to_me   +0.8   a question addressed to me (the pull of a first pair part)
    addressed_other  −1.0   (on top of MaiBot's at_others) the line is pointed at someone else: not my turn
    bystanders       −0.7 × (ln n_others − ln 5) × (1 − addressed): the more people present, the less any one of
                            them speaks up unasked -- odds ~ (N/5)^-0.7, i.e. ~0.2 at N=5, ~0.08 at 20, ~0.025 at
                            100 (research note §3: lurking / diffusion of responsibility); centred at the ~5 active
                            people MaiBot's data came from
    followup         +2.0 × exp(−Δt/τ), τ = 150 s × (0.5 + closeness): they are answering what I just said
    subconversation  −1.5 × s: two others are going back and forth (s ∈ 0..1): don't cut in (读空气)
    closing          −4.0 × ack_only: only 嗯/好的/哈哈/晚安/a sticker -- a closing or a back-channel (one-to-one with
                            a close tie: ~0.25 answered, and then with a short closer; vs ~0.85 for a statement)
    floor            −0.9 × softplus(3·(share·(n+1)/2 − 1))/3 × (1 − addressed): I already hold more than twice my
                            fair share of the recent lines (×0.3-0.6 on the odds when well over)
    heat             −0.8 × heat × (1 − addressed): a heated exchange I'm not part of
    talk_value       ln(talk_value) × (1 − addressed): temperament × today's chattiness, how readily I chime in
p_look   = σ(logit)                          deliberate look (the mind composes, and may still stay silent)
p_glance = (1 − p_look) · σ(logit + 1.2)     a cheap S1 read; it may still find something worth answering
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional, Sequence

MAIBOT_WEIGHTS = {
    "intercept": -1.2005,
    "mentions_name": 1.5852,
    "at_others": -1.1157,
    "has_question": 0.4807,
    "all_media": -1.0281,
    "bot_share_5min": 2.6525,
    "msgs_5min": -0.0367,
    "log_secs_since_bot": 0.0571,
    "pending": 0.1012,
}

ALIVE_WEIGHTS = {
    "closeness": 1.0,
    "interest": 1.5,
    "social_need": 0.8,
    "low_battery": -1.2,
    "low_energy": -0.8,
    "mode_focus": -1.0,
    "mode_drowsy": -0.8,
    "arousal": 0.4,
    "pleasure": 0.2,
    "willingness": 1.0,
}

AIR_WEIGHTS = {
    "addressed": 3.2,
    "question_to_me": 0.8,
    "addressed_other": -1.0,
    "bystanders": -0.7,
    "bystanders_ref": 5.0,
    "followup": 2.0,
    "followup_tau_s": 150.0,
    "subconversation": -1.5,
    "closing": -4.0,
    "floor": -0.9,
    "heat": -0.8,
    "glance_offset": 1.2,
}


@dataclass
class GateFeatures:
    # MaiBot features
    mentions_name: float = 0.0
    at_others: float = 0.0
    has_question: float = 0.0
    all_media: float = 0.0
    bot_share_5min: float = 0.0
    msgs_5min: float = 0.0
    secs_since_bot: float = 3600.0
    pending: float = 1.0
    # ALIVE features (raw values; centring happens in the logit)
    closeness: float = 0.3
    interest: float = 0.0
    social_need: float = 0.0
    social_battery: float = 0.8
    energy: float = 0.8
    mode: str = "active"
    arousal: float = 0.0
    pleasure: float = 0.0
    willingness: float = 0.0
    # air-reading facts about the space and the batch
    addressed: float = 0.0          # 0..1: the batch is to me (in a one-to-one space everything is)
    question_to_me: float = 0.0
    addressed_other: float = 0.0
    n_others: float = 1.0           # people other than me present / active in the space
    followup: float = 0.0           # 0..1: they are answering what I said (fades with time since)
    subconversation: float = 0.0    # 0..1: two others going back and forth
    ack_only: float = 0.0           # the batch only acknowledges (closing / back-channel)
    heat: float = 0.0               # 0..1: a heated exchange
    talk_value: float = 1.0
    my_share: float = 0.0           # my share of the space's recent lines (30 min)

    def as_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _softplus(z: float) -> float:
    return z if z > 30 else math.log1p(math.exp(z))


def _soft_deficit(x: float, center: float, width: float) -> float:
    """Smooth version of max(0, center − x)/center: ~0 well above ``center``, ~linear below it."""
    z = (center - x) / width
    sp = z if z > 30 else math.log1p(math.exp(z))
    return width * sp / max(1e-9, center)


def maibot_terms(f: GateFeatures, w: Optional[dict] = None) -> dict:
    w = w or MAIBOT_WEIGHTS
    return {
        "intercept": w["intercept"],
        "mentions_name": w["mentions_name"] * f.mentions_name,
        "at_others": w["at_others"] * f.at_others,
        "has_question": w["has_question"] * f.has_question,
        "all_media": w["all_media"] * f.all_media,
        "bot_share_5min": w["bot_share_5min"] * f.bot_share_5min,
        "msgs_5min": w["msgs_5min"] * f.msgs_5min,
        "log_secs_since_bot": w["log_secs_since_bot"] * math.log1p(max(0.0, f.secs_since_bot)),
        "pending": w["pending"] * min(20.0, f.pending),
    }


def alive_terms(f: GateFeatures, w: Optional[dict] = None) -> dict:
    w = w or ALIVE_WEIGHTS
    mode = (f.mode or "active").lower()
    return {
        "closeness": w["closeness"] * (f.closeness - 0.3),
        "interest": w["interest"] * f.interest,
        "social_need": w["social_need"] * max(0.0, min(1.0, f.social_need)),
        "low_battery": w["low_battery"] * _soft_deficit(f.social_battery, 0.35, 0.06),
        "low_energy": w["low_energy"] * _soft_deficit(f.energy, 0.30, 0.06),
        "mode_focus": w["mode_focus"] if mode in ("focus", "overwhelmed") else 0.0,
        "mode_drowsy": w["mode_drowsy"] if mode == "drowsy" else 0.0,
        "arousal": w["arousal"] * max(-1.0, min(1.0, f.arousal)),
        "pleasure": w["pleasure"] * max(-1.0, min(1.0, f.pleasure)),
        "willingness": w["willingness"] * f.willingness,
    }


def air_terms(f: GateFeatures, w: Optional[dict] = None) -> dict:
    w = {**AIR_WEIGHTS, **(w or {})}
    a = max(0.0, min(1.0, f.addressed))
    n = max(1.0, f.n_others)
    return {
        "addressed": w["addressed"] * a,
        "question_to_me": w["question_to_me"] * f.question_to_me,
        "addressed_other": w["addressed_other"] * f.addressed_other * (1 - a),
        "bystanders": w["bystanders"] * (math.log(n) - math.log(w["bystanders_ref"])) * (1 - a),
        "followup": w["followup"] * max(0.0, min(1.0, f.followup)),
        "subconversation": w["subconversation"] * max(0.0, min(1.0, f.subconversation)) * (1 - a),
        "closing": w["closing"] * f.ack_only,
        "heat": w["heat"] * max(0.0, min(1.0, f.heat)) * (1 - a),
        "talk_value": math.log(max(1e-3, f.talk_value)) * (1 - a),
        "floor": w["floor"] * _softplus(3.0 * (f.my_share * (n + 1) / 2.0 - 1.0)) / 3.0 * (1 - a),
    }


def reply_probability(f: GateFeatures, maibot_w: Optional[dict] = None, alive_w: Optional[dict] = None,
                      use_alive: bool = True, air_w: Optional[dict] = None, use_air: bool = True) -> tuple:
    """Returns (p_look, logit, terms). ``p_look`` is the probability of a deliberate look."""
    terms = maibot_terms(f, maibot_w)
    if use_alive:
        terms.update({"alive." + k: v for k, v in alive_terms(f, alive_w).items()})
    if use_air:
        terms.update({"air." + k: v for k, v in air_terms(f, air_w).items()})
    logit = sum(terms.values())
    return _sigmoid(logit), logit, {k: round(v, 4) for k, v in terms.items()}


def engagement(f: GateFeatures, rng, *, maibot_w: Optional[dict] = None, alive_w: Optional[dict] = None,
               air_w: Optional[dict] = None, use_alive: bool = True) -> tuple:
    """Sample how I attend to a batch. Returns (kind, p_look, p_glance, logit, terms); kind is
    "look" (deliberate), "glance" (S1 read) or "take_in" (perceived only)."""
    p, logit, terms = reply_probability(f, maibot_w, alive_w, use_alive, air_w)
    off = float({**AIR_WEIGHTS, **(air_w or {})}["glance_offset"])
    p_glance = (1 - p) * _sigmoid(logit + off)
    u = rng.random()
    kind = "look" if u < p else ("glance" if u < p + p_glance else "take_in")
    return kind, p, p_glance, logit, terms


# ---------------------------------------------------------------------------
# Space dynamics: what the recent lines say about who is talking to whom (formerly a hard "turn ladder")
# ---------------------------------------------------------------------------
@dataclass
class LineView:
    """Minimal view of a recent line (oldest first)."""
    ts: float
    speaker: str              # person_key, "" for me
    mine: bool = False


def followup_strength(*, recent: Sequence[LineView], batch_speakers: Sequence[str], now: float, my_last_out: float,
                      my_last_targets: Sequence[str], closeness: float = 0.3,
                      tau_s: float = AIR_WEIGHTS["followup_tau_s"]) -> float:
    """0..1: the batch answers what I said (the people I spoke to, nobody else in between), fading with time."""
    speakers = [s for s in batch_speakers if s]
    if not my_last_out or not speakers:
        return 0.0
    after = [l for l in recent if l.ts > my_last_out and not l.mine]
    people_after = {l.speaker for l in after} | set(speakers)
    targets = set(my_last_targets or [])
    if not people_after or not targets:
        return 0.0
    overlap = len(people_after & targets) / len(people_after)
    tau = tau_s * (0.5 + max(0.0, min(1.0, closeness)))
    return overlap * math.exp(-max(0.0, now - my_last_out) / max(1.0, tau))


def subconversation_strength(recent: Sequence[LineView], batch_speakers: Sequence[str], now: float,
                             window_s: float = 180.0) -> float:
    """0..1: the recent lines are two other people going back and forth, and this batch continues it."""
    window = [l for l in recent if now - l.ts <= window_s][-8:]
    if len(window) < 3:
        return 0.0
    tail = window[-6:]
    mine = sum(1 for l in tail if l.mine) / len(tail)
    seq = [l.speaker for l in window if not l.mine]
    if len(seq) < 3:
        return 0.0
    counts: dict = {}
    for s in seq:
        counts[s] = counts.get(s, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if len(ranked) < 2:
        return 0.0
    pair = {ranked[0][0], ranked[1][0]}
    pair_share = (ranked[0][1] + ranked[1][1]) / len(seq)
    switches = sum(1 for a, b in zip(seq, seq[1:]) if a != b) / max(1, len(seq) - 1)
    continues = 1.0 if set(s for s in batch_speakers if s) <= pair else 0.4
    density = 1 - math.exp(-len(window) / 4.0)
    return max(0.0, min(1.0, pair_share ** 2 * switches * continues * density * (1 - mine)))
