"""Move policy (docs/19 §5.2): prior(genre, need, frame, circle, stage) x learned(person, situation) x diversity.

Learned effects are Beta distributions per key, sampled with Thompson sampling so that the policy keeps
exploring a little (this is also the countermeasure to LLM strategy-preference bias: the generator only
chooses among diverse, evidence-weighted suggestions). Which moves come to mind is a Plackett-Luce draw
(Gumbel-top-k on log weights), with a temperature from today's playfulness / curiosity and arousal - not a top-k.
Closeness circles enter as soft memberships (catalog.circle_membership), never as buckets that zero a move.
"""
from __future__ import annotations

import math
import random
from typing import Optional, Sequence

from alive.core.dialogue.catalog import (AGENCY_MOVES, CIRCLE_CLOSENESS, GENRES, MOVES, NEED_PRIORS, STANCE_MOVES,
                                        circle_membership)
from alive.runtime import chance as C
from alive.runtime.store import register_schema

# frames in which teasing / playful counters hurt (he is confiding, venting or we are in conflict): a fact about
# the situation, like INV-24's listening lock, not a quota
NO_PLAY_FRAMES = {"confiding", "venting", "conflict"}
# base temperature of the move draw on log-weights: T = 0.15 -> weight^6.7, so a move the situation favours 1.5x is
# ~15x likelier to come to mind first; today's playfulness / curiosity and arousal widen it
MOVE_TEMPERATURE = 0.15

register_schema("core_dialogue", 1, [
    """CREATE TABLE IF NOT EXISTS core_move_stats (
        key TEXT PRIMARY KEY, alpha REAL, beta REAL, n INTEGER, updated REAL)""",
    """CREATE TABLE IF NOT EXISTS core_moves (
        id TEXT PRIMARY KEY, ts REAL, thread_id TEXT, person_key TEXT, genre TEXT, frame TEXT, need TEXT,
        circle TEXT, moves TEXT, goal TEXT, sentiment REAL, reward REAL, detail TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_core_moves_thread ON core_moves(thread_id, ts)",
    """CREATE TABLE IF NOT EXISTS core_renqing (
        person_key TEXT PRIMARY KEY, care_given INTEGER DEFAULT 0, care_received INTEGER DEFAULT 0,
        favors_given INTEGER DEFAULT 0, favors_received INTEGER DEFAULT 0, updated REAL)""",
])


class MovePolicy:
    def __init__(self, kernel, rng: Optional[random.Random] = None):
        self.k = kernel
        self.rng = rng                 # injected (tests); otherwise the life's RNG (chance.rng_of)
        self.stats: dict = {}        # key -> [alpha, beta, n]
        self.recent_global: list = []

    def _r(self) -> random.Random:
        return self.rng if self.rng is not None else C.rng_of(self.k)

    @staticmethod
    def circles(sit: dict) -> dict:
        """Soft circle memberships of the situation: from its closeness, else from the circle label."""
        c = sit.get("closeness")
        if c is None:
            c = CIRCLE_CLOSENESS.get(sit.get("circle"), 0.3)
        return circle_membership(float(c))

    def temperature(self, sit: dict) -> float:
        if sit.get("move_temperature") is not None:
            return float(sit["move_temperature"])
        t = MOVE_TEMPERATURE
        tex = getattr(self.k, "texture", None)
        if tex is not None:
            try:
                t *= tex.multiplier("playfulness", 0.5) * tex.multiplier("curiosity", 0.5)
            except Exception:
                pass
        try:
            t *= 1.0 + 0.25 * float(self.k.affect.snapshot().A)
        except Exception:
            pass
        return max(0.03, t)

    async def load(self) -> None:
        rows = await self.k.db.fetchall("SELECT key, alpha, beta, n FROM core_move_stats")
        self.stats = {r["key"]: [r["alpha"], r["beta"], r["n"]] for r in rows}

    def _sample(self, key: str) -> Optional[float]:
        a, b, n = self.stats.get(key, (1.0, 1.0, 0))
        if n == 0:
            return None
        return self._r().betavariate(max(0.05, a), max(0.05, b))

    def prior(self, move: str, sit: dict) -> float:
        w = 1.0
        w *= GENRES.get(sit.get("genre", "other"), GENRES["other"])["pref"].get(move, 1.0)
        w *= NEED_PRIORS.get(sit.get("need", ""), {}).get(move, 1.0)
        frame, stage = sit.get("frame"), sit.get("stage")
        if move in AGENCY_MOVES:                     # docs/21 §2: own stance scales with agency
            agency = float(sit.get("agency", 0.4))
            if move in STANCE_MOVES and sit.get("stance_open"):
                # docs/21 §2.5: the empathy lock holds the topic and the feeling, not my view of his claim
                agency = max(agency, float(sit.get("stance_agency", agency)))
            w *= 0.15 + 1.7 * agency
            if move == "self_topic" and (sit.get("listen_lock") or agency < 0.3):
                return 0.0                          # INV-24
            if move == "self_topic":
                w *= 1.0 + 0.8 * float(sit.get("topic_imbalance", 0.0)) + (0.6 if sit.get("closing") else 0.0)
                if not sit.get("has_agenda"):
                    w *= 0.3
            locked = sit.get("listen_lock") and not sit.get("stance_open")
            if move == "disagree_soft" and (locked or not (sit.get("has_opinion") or sit.get("stance_open"))):
                w *= 0.15
            if move == "own_view" and locked:
                w *= 0.3
            if move == "qualify" and not sit.get("claim"):
                return 0.0
            if move in STANCE_MOVES and sit.get("agree_streak"):
                w *= 1.5                            # anti-sycophancy (docs/21 §2.4)
            if move == "counter":
                if sit.get("sensitive") or frame in NO_PLAY_FRAMES:
                    return 0.0
                # a playful counter belongs to the inner circle and to play: weighted by both, not switched
                w *= self.circles(sit)["intimate"] * (1.0 if frame == "play" else 0.1)
        if move in ("tease", "play_along", "counter"):
            w *= float(sit.get("playfulness_mult", 1.0))
        if move == "tease":
            if sit.get("sensitive") or frame in NO_PLAY_FRAMES:
                return 0.0
            cw = self.circles(sit)
            w *= (cw["intimate"] + cw["familiar"]) * (1.0 if frame == "play" else 0.1)
            # in front of others (an audience I'm not close to) a tease costs more face (docs/20 §4)
            pub = sit.get("publicness")
            pub = (1.0 if stage == "front" else 0.0) if pub is None else float(pub)
            w *= 1.0 - 0.3 * pub
        if move == "self_disclose":
            cw = self.circles(sit)
            w *= 1.2 * cw["intimate"] + 1.0 * cw["familiar"] + 0.6 * cw["acquaintance"] + 0.3 * cw["stranger"]
        if move in ("guide_q", "reframe_q") and not sit.get("guiding_goal"):
            w *= 0.4
        if move in ("decline", "cushion") and sit.get("genre") != "invite" and sit.get("face_risk", 0) < 0.5:
            w *= 0.3
        if move == "invite" and not sit.get("goal") == "invite":
            w *= 0.3
        if move == "wait" and sit.get("mentioned"):
            w *= 0.25          # being @-ed makes silence costly, not impossible (INV-28)
        if move in ("inform", "suggest") and sit.get("pending_question_to_me"):
            w *= 1.3
        if move == "close" and sit.get("genre") != "farewell":
            w *= 0.3
        if move in ("return", "callback") and not sit.get("has_open_topics"):
            w *= 0.4
        if move == "pre_sequence" and sit.get("goal") != "invite":
            w *= 0.2
        return w

    def suggest(self, sit: dict, person_key: Optional[str], my_recent_moves: Sequence[str], k: int = 3) -> list:
        """``k`` moves that come to mind (the size of the menu shown to the composer), sampled without replacement
        in Plackett-Luce order from prior x learned (Thompson) x diversity."""
        sk = f"{sit.get('genre','other')}|{sit.get('need','')}"
        scored = {}
        for m in MOVES:
            p = self.prior(m, sit)
            if p <= 0:
                continue
            learned = 1.0
            for key in (f"p:{person_key}|{sk}|{m}" if person_key else None, f"g:{sk}|{m}"):
                if key is None:
                    continue
                s = self._sample(key)
                if s is not None:
                    learned *= 0.5 + s       # 0.5..1.5
            div = 1.0
            if m in my_recent_moves[-2:]:
                div *= 0.6
            if self.recent_global:
                share = self.recent_global.count(m) / len(self.recent_global)
                if share > 0.35:
                    div *= 0.8
            scored[m] = p * learned * div
        if not scored:
            return []
        order = C.gumbel_topk(self._r(), {m: math.log(max(1e-12, s)) for m, s in scored.items()}, k,
                              self.temperature(sit))
        return [(m, round(scored[m], 3)) for m in order]

    async def update(self, person_key: Optional[str], sit: dict, moves: Sequence[str], reward: float,
                     weight: float = 1.0) -> None:
        """Beta update; ``weight`` < 1 for partial evidence (e.g. an answer that is only probably missing)."""
        reward = max(0.0, min(1.0, reward))
        moves = [m for m in moves if m in MOVES] or []
        if not moves or weight <= 0:
            return
        share = max(0.0, min(1.0, float(weight))) / len(moves)
        sk = f"{sit.get('genre','other')}|{sit.get('need','')}"
        rows = []
        now = self.k.clock.now()
        for m in moves:
            for key in ([f"p:{person_key}|{sk}|{m}"] if person_key else []) + [f"g:{sk}|{m}"]:
                a, b, n = self.stats.get(key, [1.0, 1.0, 0])
                a += reward * share
                b += (1 - reward) * share
                n += 1
                self.stats[key] = [a, b, n]
                rows.append((key, a, b, n, now))
        await self.k.db.executemany("INSERT OR REPLACE INTO core_move_stats VALUES (?,?,?,?,?)", rows)

    def note_used(self, moves: Sequence[str]) -> None:
        self.recent_global.extend(m for m in moves if m in MOVES)
        if len(self.recent_global) > 60:
            del self.recent_global[:-60]

    def mean(self, key: str) -> Optional[float]:
        a, b, n = self.stats.get(key, (1.0, 1.0, 0))
        return a / (a + b) if n else None
