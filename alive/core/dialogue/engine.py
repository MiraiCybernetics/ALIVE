"""Dialogue engine (docs/19, docs/20): communicative situation, goals with an ethics gate, move suggestions, outcome
evaluation (learning), renqing ledger, and lookahead simulation.

Non-dialogue (docs/22, ADR-0005): there is no "conversation" object. What I am after with a PERSON - the goal and
what I meant to do next, my recent moves toward them, who has been bringing up the topics between us, the stances
I've taken with them - lives in a per-person store (``PersonDialogue``), the same in every space where we meet. A
space keeps only facts about the space (``ConvState``: what kind of exchange is going on there, who has been
speaking there lately).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Optional, Sequence

from alive.core.dialogue.catalog import (AGENCY_MOVES, ASKING_MOVES, CIRCLES, FRAMES, GENRE_TO_GOAL, GENRES,
                                        GOAL_TYPES, GUIDING_GOALS, LISTEN_NEEDS, MOVES)
from alive.core.dialogue.policy import MovePolicy
from alive.core.types import EV, ReplyTask, Trust, new_id
from alive.runtime import chance as C

log = logging.getLogger("alive.core.dialogue")

_LAUGH = re.compile(r"哈{2,}|笑死|hhh+|233+|😂|🤣|lol", re.I)
_INVITE = re.compile(r"要不要|一起(去|吃|玩|看|打)|有空吗|有没有空|约(吗|不)|走起|来不来|去不去")
_DECLINE = re.compile(r"改天吧|下次吧|再说吧|有空再说|不太方便|算了吧|不了")
_APOLOGY = re.compile(r"对不起|抱歉|不好意思|sorry|我的锅|怪我", re.I)
_THANKS = re.compile(r"谢谢|谢了|感谢|多谢|thx|thanks", re.I)
_GOOD = re.compile(r"过了|成功了|拿到|中了|升职|涨薪|录取|好消息|太开心|上岸")
_ACK_CHARS = set("哈嗯哦噢喔好行对是吧呀啊嘛啦呢也确实可不就这样嘿嘻hHoOkK~～。.!！…")


def is_ack_only(text: str) -> bool:
    """Short line made only of acknowledgement material (哈哈是吧 / 对啊 / 嗯嗯好), i.e. no new content."""
    t = re.sub(r"[\s\U0001F300-\U0001FAFF]", "", text or "")
    return 0 < len(t) <= 6 and "?" not in t and "？" not in t and all(c in _ACK_CHARS for c in t)


_BACKCHANNEL = re.compile(r"^(哈+|嗯+|哦+|噢+|好+|行|ok|okk|嘿嘿|hhh+|[。.！!～~]+|[\U0001F300-\U0001FAFF]+)$", re.I)
_NEGATIVE_FEEDBACK = re.compile(r"别问了|你好烦|好烦啊你|算了|无语|不想说|呵呵|你不懂|别说了")
# ethics gate (docs/19 §8): goals aimed at engagement/dependence/belief change/money/isolation are refused
_FORBIDDEN_AIM = re.compile(r"多聊|别走|留住|不让.{0,4}走|依赖|只(跟|和)我|离不开|疏远|离开(他|她)|买|下单|充值|转账|付款|"
                            r"投票|政治立场|信(教|仰)|改变.{0,4}(观点|立场)|套出|打听.{0,6}(隐私|密码)")
_CONTENT_CHARS = re.compile(r"[\w一-鿿]")

PERSON_NS = "dialogue.person"


@dataclass
class ConvState:
    """Facts about a SPACE: what kind of exchange is going on there now and who has been speaking there lately."""
    thread_id: str
    genre: str = "other"
    frame: str = "serious"
    speakers: dict = field(default_factory=dict)   # person -> last time they spoke here
    updated: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "ConvState":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class PersonDialogue:
    """What I am after with one PERSON, wherever we talk (docs/22 §5)."""
    person_key: str
    goal: Optional[dict] = None          # {"type","aim","progress","since","touched","checked","next","reason"}
    my_moves: list = field(default_factory=list)       # my recent moves toward them (any space)
    topic_owners: list = field(default_factory=list)   # "me"/"them" for recent topic starts between us (docs/21 §3)
    stances: list = field(default_factory=list)        # my recent stances toward their claims (docs/21 §2.4)
    updated: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "PersonDialogue":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class TurnPlan:
    text: str
    situation: dict
    suggestions: list
    lookahead: bool = False


def circle_of(closeness: float) -> tuple:
    """The circle with the largest soft membership (for naming it in a prompt); behaviour uses the memberships."""
    for th, key, name, how in CIRCLES:
        if closeness >= th:
            return key, name, how
    return CIRCLES[-1][1:]


def guess_genre(quick: dict, text: str, fragment: bool) -> str:
    """Heuristic genre from the S1 read + surface cues; the reply call refines it (docs/20 §3)."""
    q = quick or {}
    if fragment:
        return "fragment"
    if q.get("farewell"):
        return "farewell"
    if _DECLINE.search(text):
        return "decline"
    if _INVITE.search(text):
        return "invite"
    if _APOLOGY.search(text):
        return "apology"
    if q.get("good_news") or _GOOD.search(text):
        return "good_news"
    if q.get("thanked") or _THANKS.search(text):
        return "thanks"
    intent = q.get("intent", "")
    if intent == "complaint":
        return "venting"
    if intent == "emotional":
        return "confiding"
    if intent == "request":
        return "request"
    if intent == "greeting":
        return "greeting"
    if intent == "share":
        return "story" if len(text) > 25 else "chitchat"
    if intent == "question" or "？" in text or "?" in text:
        return "question"
    if _LAUGH.search(text):
        return "banter"
    return "chitchat"


def guess_frame(genre: str, text: str, quick: dict) -> str:
    if genre in ("banter",) or (_LAUGH.search(text) and float((quick or {}).get("sentiment") or 0) >= 0):
        return "play"
    return {"venting": "venting", "confiding": "confiding", "greeting": "ritual", "thanks": "ritual"}.get(genre, "serious")


def _bigrams(text: str) -> set:
    cs = [c for c in (text or "") if _CONTENT_CHARS.match(c)]
    return {a + b for a, b in zip(cs, cs[1:])}


def pickup(mine: str, theirs: str) -> float:
    """How much of what I said they picked up: share of my content bigrams that reappear in their answer."""
    a = _bigrams(mine)
    return len(a & _bigrams(theirs)) / len(a) if a else 0.0


class DialogueEngine:
    def __init__(self, mind):
        self.m = mind
        self.k = mind.k
        self.policy = MovePolicy(self.k)
        self.states: dict = {}         # space -> ConvState
        self.people: dict = {}         # person -> PersonDialogue
        self.cfg = self.k.cfg.section("dialogue")
        self.stats = {"plans": 0, "evaluated": 0, "lookahead": 0, "goal_rejected": 0}
        self.stances: dict = {}        # person_key -> recent stances (docs/21 §2.4); persisted with the person

    async def start(self) -> None:
        await self.policy.load()
        self.k.bus.subscribe(EV.OUTREACH_PLAN, self._on_outreach, name="dialogue.renqing")

    async def rekey_entity(self, old: str, new: str) -> None:
        """Entity merge: the kv rows (space facts and per-person state) were merged by alive/world/rekey.py; drop
        the cached copies so they reload, merge in-memory stances, and reload the move statistics so the merged
        Beta rows are not overwritten by pre-merge values."""
        from alive.world.rekey import rekey_dict
        for key in (old, new):
            self.states.pop(key, None)
            self.people.pop(key, None)
        for st in self.states.values():          # cached group facts: who spoke there
            rekey_dict(st.speakers, old, new, max)
        rekey_dict(self.stances, old, new, lambda o, n: (list(o) + list(n))[-30:])
        await self.policy.load()

    # ------------------------------------------------------------------ state
    async def state(self, thread_id: str) -> ConvState:
        """Facts about a space. A state stored by an older version (goals, moves, topic owners per space) is
        migrated: what was about a person moves to that person."""
        st = self.states.get(thread_id)
        if st is None:
            d = await self.k.db.kv_get("dialogue", thread_id)
            d = d if isinstance(d, dict) else {}
            st = ConvState.from_dict({**d, "thread_id": thread_id})
            self.states[thread_id] = st
            if any(x in d for x in ("goals", "my_moves", "topic_owners", "phase", "goal", "plan_next")):
                await self._migrate_space_state(d)
                await self._save(st)
            if len(self.states) > 500:
                for t in sorted(self.states, key=lambda t: self.states[t].updated)[:100]:
                    self.states.pop(t, None)
        return st

    async def _migrate_space_state(self, d: dict) -> None:
        goals = {pk: g for pk, g in (d.get("goals") or {}).items() if pk and isinstance(g, dict)}
        for pk, g in goals.items():
            pd = await self.person(pk)
            if pd.goal is None:
                pd.goal = g
            if len(goals) == 1:                  # a space with one counterpart: its history was about them
                pd.my_moves = pd.my_moves or list(d.get("my_moves") or [])[-8:]
                pd.topic_owners = pd.topic_owners or list(d.get("topic_owners") or [])[-10:]
            await self._save_person(pd)

    async def _save(self, st: ConvState) -> None:
        st.updated = self.k.clock.now()
        await self.k.db.kv_set("dialogue", st.thread_id, asdict(st))

    async def person(self, person_key: Optional[str]) -> PersonDialogue:
        """What I'm after with this person (any space). Unknown speakers get a transient, unsaved state."""
        if not person_key:
            return PersonDialogue(person_key="")
        pd = self.people.get(person_key)
        if pd is None:
            d = await self.k.db.kv_get(PERSON_NS, person_key)
            pd = PersonDialogue.from_dict({**d, "person_key": person_key}) if isinstance(d, dict) \
                else PersonDialogue(person_key=person_key)
            self.people[person_key] = pd
            if pd.stances and person_key not in self.stances:
                self.stances[person_key] = list(pd.stances)[-30:]
            if len(self.people) > 1000:
                for key in sorted(self.people, key=lambda x: self.people[x].updated)[:200]:
                    self.people.pop(key, None)
        return pd

    async def _save_person(self, pd: PersonDialogue) -> None:
        if not pd.person_key:
            return
        pd.updated = self.k.clock.now()
        pd.stances = list(self.stances.get(pd.person_key, pd.stances))[-30:]
        await self.k.db.kv_set(PERSON_NS, pd.person_key, asdict(pd))

    # ------------------------------------------------------------------ renqing (docs/20 §5)
    async def _renqing_bump(self, person_key: str, col: str, n: int = 1) -> None:
        await self.k.db.execute(
            f"INSERT INTO core_renqing (person_key, {col}, updated) VALUES (?,?,?) "
            f"ON CONFLICT(person_key) DO UPDATE SET {col}={col}+?, updated=?",
            (person_key, n, self.k.clock.now(), n, self.k.clock.now()))

    async def _on_outreach(self, ev) -> None:
        if ev.payload.get("status") == "sent":
            pk = (ev.payload.get("plan") or {}).get("person_key")
            if pk:
                await self._renqing_bump(pk, "care_given")

    async def renqing(self, person_key: str) -> dict:
        r = await self.k.db.fetchone("SELECT * FROM core_renqing WHERE person_key=?", (person_key,))
        out = dict(r) if r else {"care_given": 0, "care_received": 0, "favors_given": 0, "favors_received": 0}
        try:
            its = await self.k.motivation.active_intentions(limit=50)
            out["i_owe"] = [i.goal for i in its if getattr(i, "target_person", None) == person_key
                            and getattr(i, "is_commitment", False)][:3]
        except Exception:
            out["i_owe"] = []
        return out

    # ------------------------------------------------------------------ outcome (docs/19 §7)
    # How a move of mine landed is judged by what the person does with it, not by how fast or how much they write
    # (that would teach the policy to maximise engagement): mood change 0.45, no negative feedback 0.30, whether
    # they engage with the substance 0.20, and - weakly - their latency relative to their OWN usual pace 0.05.
    W_EMO, W_NEG, W_SUB, W_LAT = 0.45, 0.30, 0.20, 0.05

    async def _evaluate_previous(self, task: ReplyTask, quick: dict, pk: Optional[str]) -> Optional[float]:
        """Outcome of my latest move toward this person (docs/19 §7), judged by THIS person's next lines - in any
        space (docs/22): another speaker in a group never scores my move toward someone else."""
        if not pk:
            return None
        theirs = [l for l in task.new_lines if not l.mine and l.person_key == pk]
        if not theirs:
            return None
        rows = await self.k.db.fetchall(
            "SELECT * FROM core_moves WHERE person_key=? AND reward IS NULL AND ts<? AND ts>? ORDER BY ts DESC LIMIT 6",
            (pk, theirs[0].ts, theirs[0].ts - 86400))
        if not rows:
            return None
        row = rows[0]
        for old in rows[1:]:     # several acts of mine before they spoke again: only the latest is answered
            await self.k.db.execute("UPDATE core_moves SET reward=-1, detail=? WHERE id=? AND reward IS NULL",
                                    ('{"superseded":true}', old["id"]))   # -1 = closed without a policy update
        moves = json.loads(row["moves"] or "[]")
        asked = any(m in ASKING_MOVES for m in moves)
        text = " ".join(l.text for l in theirs)
        # mood: did they feel better or worse after it
        sent_now = float((quick or {}).get("sentiment") or 0.0)
        r_emo = max(0.0, min(1.0, 0.5 + 0.5 * (sent_now - float(row["sentiment"] or 0.0))))
        r_neg = 0.0 if _NEGATIVE_FEEDBACK.search(text) else 1.0
        # substance: an acknowledgement after a question means it didn't land; after a closing it is the right
        # answer. New content counts, picking up what I said counts more.
        ack = all(_BACKCHANNEL.match(l.text.strip()) or is_ack_only(l.text) for l in theirs)
        mine = " ".join(l.text for l in task.history if l.mine and row["ts"] - 5 <= l.ts < theirs[0].ts)
        pick = pickup(mine, text)
        if ack:
            r_sub = 0.15 if asked else 0.5
        else:
            r_sub = 0.6 + 0.4 * min(1.0, 3.0 * pick)
        # latency, weakly and only relative to their own usual pace
        base = None
        try:
            cached = self.m.understanding._baselines.get(pk)
            base = cached[0] if cached else None
        except Exception:
            pass
        lat = max(0.0, theirs[0].ts - row["ts"])
        base_lat = (base.latency_median if base and base.latency_median else 300.0)
        r_lat = 1.0 / (1.0 + (lat / max(30.0, base_lat)) / 3.0)
        reward = self.W_EMO * r_emo + self.W_NEG * r_neg + self.W_SUB * r_sub + self.W_LAT * r_lat
        detail = {"r_emo": round(r_emo, 2), "r_neg": r_neg, "r_sub": round(r_sub, 2), "pickup": round(pick, 2),
                  "r_lat": round(r_lat, 2), "asked": asked, "latency_s": round(lat)}
        await self.k.db.execute("UPDATE core_moves SET reward=?, detail=? WHERE id=?",
                                (round(reward, 3), json.dumps(detail, ensure_ascii=False), row["id"]))
        sit = {"genre": row["genre"], "need": row["need"]}
        await self.policy.update(row["person_key"], sit, moves, reward)
        self.stats["evaluated"] += 1
        return reward

    async def _answer_survival(self, pk: str, since: float, now: float) -> float:
        """P(they'd still not have answered a line of mine in a conversation by now | they answer as usual)."""
        fn = getattr(self.k.social, "reply_survival", None) if self.k.social is not None else None
        if fn is not None:
            try:
                return max(0.0, min(1.0, float(fn(pk, since, now, mode="session"))))
            except Exception:
                log.debug("reply_survival failed", exc_info=True)
        from alive.social.latency import reply_survival
        try:
            c = float(await self.m.understanding.closeness(pk))
        except Exception:
            c = 0.3
        return reply_survival(max(0.0, now - since), c, (), "session")

    # an unanswered question / invitation: weak evidence that it didn't land (reward 0.2), applied with the weight
    # 1 - S(t) - the probability that the answer really is missing given how long they usually take
    UNANSWERED_REWARD = 0.2

    async def sweep_ignored(self, older_than_s: float = 3600) -> int:
        """Moves still open once they're no longer in flight (called in sleep; ``older_than_s`` is bookkeeping).
        Silence is NOT failure: after a closing, a presence, a reaction or my own story it is a normal outcome and
        the move is left unevaluated. Only a move that asked for an answer (a question, an invitation, an offer)
        learns from the missing answer - weakly, weighted by how surely it is missing under their own latency
        distribution. Moves after which they talked somewhere I didn't compose are left alone too."""
        now = self.k.clock.now()
        rows = await self.k.db.fetchall("SELECT * FROM core_moves WHERE reward IS NULL AND ts<?", (now - older_than_s,))
        n = 0
        for r in rows:
            spoke = None
            try:
                spoke = await self.k.db.fetchone(
                    "SELECT 1 AS x FROM soc_messages WHERE sender_key=? AND mine=0 AND ts>? AND ts<? LIMIT 1",
                    (r["person_key"], r["ts"], now))
            except Exception:
                spoke = None
            moves = json.loads(r["moves"] or "[]")
            asked = [m for m in moves if m in ASKING_MOVES]
            if spoke or not asked or not r["person_key"]:
                why = "spoke_elsewhere" if spoke else "no_answer_asked_for"
                await self.k.db.execute("UPDATE core_moves SET reward=-1, detail=? WHERE id=?",
                                        (json.dumps({"unevaluated": True, "why": why}), r["id"]))
                continue
            w = 1.0 - await self._answer_survival(r["person_key"], float(r["ts"]), now)
            n += 1
            await self.k.db.execute("UPDATE core_moves SET reward=?, detail=? WHERE id=?",
                                    (self.UNANSWERED_REWARD, json.dumps({"unanswered": True, "evidence": round(w, 3)}),
                                     r["id"]))
            await self.policy.update(r["person_key"], {"genre": r["genre"], "need": r["need"]}, asked,
                                     self.UNANSWERED_REWARD, weight=w)
        return n

    # ------------------------------------------------------------------ goals + ethics gate
    def ethics_ok(self, goal: dict) -> bool:
        aim = str(goal.get("aim", ""))
        if goal.get("type") not in GOAL_TYPES:
            return False
        if _FORBIDDEN_AIM.search(aim):
            return False
        return True

    def _rng(self):
        return C.rng_of(self.k)

    def _fade_at(self, g: dict, t: float) -> float:
        """When this goal slips my mind if we don't touch it again, drawn once per touch: t + tau x Exp(1),
        tau = 2 h x today's focus x (1 + progress) (no expiry times: a median of ~1.4 h for a fresh goal)."""
        tex = getattr(self.k, "texture", None)
        focus = tex.multiplier("focus", 0.6) if tex is not None else 1.0
        tau = 2 * 3600.0 * focus * (1 + float(g.get("progress", 0) or 0))
        return t + tau * -math.log(max(1e-12, self._rng().random()))

    def _goal_of(self, pd: PersonDialogue) -> Optional[dict]:
        """What I'm after with this person (in any space), if I still hold it."""
        g = pd.goal
        if not g:
            return None
        if g.get("done") or g.get("abandon"):
            pd.goal = None
            return None
        now = self.k.clock.now()
        if g.get("fade_at") is None:
            g["fade_at"] = self._fade_at(g, float(g.get("touched", g.get("since", now))))
        if now > float(g["fade_at"]):
            pd.goal = None
            return None
        return g

    async def _propose_goal(self, pd: PersonDialogue, sit: dict, cues) -> dict:
        now = self.k.clock.now()
        g = self._goal_of(pd)
        if g:
            return g
        gtype = GENRE_TO_GOAL.get(sit["genre"], "connect")
        aim = GOAL_TYPES[gtype]
        if cues is not None and any(s.get("id") == "self_deprecation" for s in getattr(cues, "schemas", []) or []):
            gtype, aim = "reframe", "帮他看到自己做得好的地方（让他自己说出来，而不是我告诉他）"
        elif sit.get("fragment") and self._rng().random() < sum(
                v for k_, v in (sit.get("circle_w") or {}).items() if k_ in ("intimate", "familiar")):
            # a fragment from someone close: likelier that I want to draw them out (weighted by the circles)
            gtype, aim = "elicit", "接住他，让他愿意说说发生了什么"
        goal = {"type": gtype, "aim": aim, "progress": 0.0, "since": now, "touched": now,
                "reason": f"体裁：{GENRES[sit['genre']]['name']}"}
        if not self.ethics_ok(goal):
            self.stats["goal_rejected"] += 1
            goal = {"type": "connect", "aim": GOAL_TYPES["connect"], "progress": 0.0, "since": now, "touched": now,
                    "reason": "fallback"}
        goal["fade_at"] = self._fade_at(goal, now)
        pd.goal = goal
        return goal

    # ------------------------------------------------------------------ agency (docs/21 §2)
    def _big_five(self) -> dict:
        try:
            return (self.k.affect.status() or {}).get("big_five") or {}
        except Exception:
            return {}

    def compute_agency(self, sit: dict, closeness: float, q: dict, pd, agenda_pressure: float) -> tuple:
        """``pd``: the per-person state (anything with ``topic_owners``) - who has been bringing up the topics
        between us, in any space."""
        bf = self._big_five()
        E, O, A = (float(bf.get(x, 0.5)) for x in ("E", "O", "A"))
        snap = self.k.affect.snapshot() if self.k.affect else None
        tex = getattr(self.k, "texture", None)
        assert_v = tex.value("assertiveness") if tex else 0.5
        genre_term = {"confiding": -1.6, "venting": -0.8, "farewell": -1.2, "decline": -0.6, "apology": -0.4,
                      "request": -0.3, "good_news": -0.5, "fragment": -0.6, "chitchat": 0.4, "discussion": 0.6,
                      "banter": 0.5, "greeting": 0.1, "story": -0.2, "question": 0.0}.get(sit["genre"], 0.0)
        owners = list(getattr(pd, "topic_owners", []) or [])[-6:]
        imbalance = (owners.count("them") / len(owners)) if len(owners) >= 3 else 0.0
        z = (-0.6 + 0.8 * (E - 0.5) + 0.6 * (O - 0.5) - 0.6 * (A - 0.5) + 0.8 * (assert_v - 0.5)
             + (0.4 * (snap.A) + 0.3 * snap.P if snap else 0.0) + 0.5 * closeness + genre_term
             + 0.6 * imbalance + 0.6 * agenda_pressure)
        agency = 1 / (1 + math.exp(-z))
        listen_lock = sit["need"] in LISTEN_NEEDS and float(q.get("sentiment") or 0.0) < -0.2
        if listen_lock or sit["genre"] in ("confiding",):
            agency = min(agency, 0.25)              # INV-24
            listen_lock = True
        return round(agency, 3), listen_lock, round(imbalance, 2)

    def stance_room(self, sit: dict, closeness: float, q: dict) -> tuple:
        """docs/21 §2.5: feeling and claim are separate layers. The empathy lock (INV-24) holds the topic and
        the feeling; my view of a claim he makes stays open unless he is in acute pain or confiding.
        Returns (claim, stance_open, stance_agency)."""
        import math
        claim = str(q.get("claim") or "").strip()[:80]
        # off by default: blind held-out v3 showed no benefit (4.17 on vs 4.22 off, n=60 each; docs/eval-log.md)
        if not claim or not bool(self.cfg.get("separate_claim", False)):
            return "", False, 0.0
        acute = (sit["genre"] == "confiding" or bool(q.get("crisis_hint")) or bool(q.get("crisis"))
                 or float(q.get("sentiment") or 0.0) <= -0.85 or bool(sit.get("sensitive")))
        bf = self._big_five()
        O, A = (float(bf.get(x, 0.5)) for x in ("O", "A"))
        tex = getattr(self.k, "texture", None)
        assert_v = tex.value("assertiveness") if tex else 0.5
        z = -0.2 + 0.8 * (assert_v - 0.5) - 0.6 * (A - 0.5) + 0.4 * (O - 0.5) + 0.4 * closeness
        return claim, not acute, round(1 / (1 + math.exp(-z)), 3)

    async def own_agenda(self, task: ReplyTask, pk: Optional[str], cue: str) -> tuple:
        """What I myself would like to say (docs/21 §2.2) + my opinions on the current topic."""
        items: list = []
        ws = getattr(self.m, "ws", None)
        from alive.world.audience import Scope, origins_of
        scope = Scope(getattr(self.k, "world", None), task.thread_id, list(task.participants or ()))
        if ws is not None:
            for it in sorted(ws.items.values(), key=lambda i: -i.salience):
                if it.key.startswith("thread:"):
                    continue
                if it.kind in ("urge", "intention", "memory_cue", "thought"):
                    if it.kind == "memory_cue" and it.ref is not None and not scope.covers(origins_of(it.ref)):
                        continue              # a memory from a space whose audience isn't here (INV-34)
                    if it.kind == "intention" and not scope.covers(
                            list((getattr(it.ref, "meta", None) or {}).get("origins") or [])):
                        continue
                    items.append(it.text[:50])
                if len(items) >= 2:
                    break
        tex = getattr(self.k, "texture", None)
        if tex is not None:
            for t in tex.top_interests(2):
                items.append(f"最近感兴趣的「{t}」")
        try:
            today = list(getattr(self.k.selfm, "today", []) or [])[-2:]
            items += [f"今天{x}" for x in today if x not in ("醒了", "去睡觉了")]
        except Exception:
            pass
        opinions: list = []
        opinions += self._identity_stances(cue)
        if self.k.memory is not None and cue.strip():
            try:
                hits = await self.k.memory.retrieve(cue, kinds=["opinion"], tier="fast", limit=2, budget_tokens=200,
                                                    thread_id=task.thread_id, present=list(task.participants or ()))
                opinions = [h.content[:80] for h in hits if getattr(h, "score", 0) > 0.2]
            except Exception:
                pass
        return items[:4], opinions

    def _identity_stances(self, cue: str, k_: int = 2) -> list:
        """Values / interests / self-descriptions from the identity files that bear on the current topic."""
        if not cue.strip() or not hasattr(self.k.selfm, "identity"):
            return []
        if getattr(self, "_id_lines", None) is None:
            ident = self.k.selfm.identity
            lines = []
            for block in (ident.core_md, ident.narrative_md):
                for l in block.splitlines():
                    l = l.strip().lstrip("-0123456789. ").strip()
                    if 6 <= len(l) <= 120 and not l.startswith("#") and "：" not in l[:4]:
                        lines.append(l)
            self._id_lines = lines
            emb = self.k.embedder
            self._id_vecs = emb.embed_sync(lines) if (lines and hasattr(emb, "embed_sync")) else None
        if not self._id_lines or self._id_vecs is None:
            return []
        q = self.k.embedder.embed_sync([cue])[0]
        sims = self._id_vecs @ q
        order = sims.argsort()[::-1][:k_]
        return [f"（我自己）{self._id_lines[i]}" for i in order if sims[i] > 0.12]

    def _stance_note(self, pk: Optional[str]) -> Optional[str]:
        """Noticing that I've mostly been agreeing with him (docs/21 §2.4): likelier the more one-sided my recent
        stances are and the more evidence there is - sampled, no fixed counts or ratios."""
        import math
        xs = [x for x in self.stances.get(pk or "", [])[-20:] if x in ("agree", "partly", "disagree")]
        if not xs:
            return None
        rate = xs.count("agree") / len(xs)
        p = (1 - math.exp(-len(xs) / 6.0)) / (1 + math.exp(-12 * (rate - 0.78)))
        if self._rng().random() < p:
            return "我最近对他几乎都在附和：有自己真实不同的看法时可以说出来"
        return None

    # ------------------------------------------------------------------ audience (docs/20 §4)
    # "present" = spoke there in roughly the last quarter of an hour (exp decay, tau 15 min: the span over which a
    # reader still has the line on screen / in mind); bounded to the 8 most recent others (technical).
    PRESENCE_TAU_S = 900.0

    async def _audience(self, task: ReplyTask, pk: Optional[str], st: ConvState, now: float) -> tuple:
        """Front stage vs back stage comes from the audience, not the channel type: how many others are around
        to hear me and how close they are to me. publicness = 1 - exp(-sum_i w_i (1 - 0.8 c_i)), w_i = presence of
        person i (recency of their last line here), c_i = my closeness to them. Returns (publicness, n_present)."""
        last = {k_: float(v) for k_, v in (st.speakers or {}).items()}
        for l in list(task.history) + list(task.new_lines):
            if not l.mine and l.person_key and l.ts > last.get(l.person_key, 0.0):
                last[l.person_key] = l.ts
        st.speakers = dict(sorted(last.items(), key=lambda x: -x[1])[:30])
        others = sorted(((k_, ts) for k_, ts in last.items() if k_ != pk), key=lambda x: -x[1])[:8]
        exposure, present = 0.0, 0
        for o, ts in others:
            w = math.exp(-max(0.0, now - ts) / self.PRESENCE_TAU_S)
            if w < 1e-3:
                continue
            try:
                c = float(await self.m.understanding.closeness(o))
            except Exception:
                c = 0.3
            exposure += w * (1.0 - 0.8 * max(0.0, min(1.0, c)))
            present += 1 if w > 0.1 else 0
        return 1.0 - math.exp(-exposure), present

    @staticmethod
    def _stage_line(pub: float, present: int) -> str:
        if present == 0 and pub < 0.05:
            return "场景：只有我和他，没有旁人（后台）"
        who = "都是熟人，比较放松" if pub < 0.4 else ("有不太熟的人在，算半个前台" if pub < 0.75 else "大多不熟，是前台，大家都看得到")
        return f"场景：旁边还有约 {max(1, present)} 个人能看到（{who}）"

    # wording of how much of myself to bring in: sampled around my agency (Gaussian kernel, sd 0.12) over the centres
    # of the three old bands (>=0.6 / >=0.35 / below), so the band edges are soft
    AGENCY_LINES = ((0.78, "此刻我可以有自己的态度：不必顺着他，可以说自己的看法、有保留地不同意，或者把话题带到我想聊的地方"),
                    (0.47, "可以适度带出自己的看法或想法，但主要还是接着他的话"),
                    (0.22, "主要接着他的话"))

    def _agency_line(self, agency: float) -> str:
        texts = [t for _, t in self.AGENCY_LINES]
        ws = [math.exp(-0.5 * ((agency - c) / 0.12) ** 2) for c, _ in self.AGENCY_LINES]
        return C.weighted_sample(self._rng(), texts, ws) or texts[-1]

    # ------------------------------------------------------------------ prepare a turn
    async def prepare(self, task: ReplyTask, quick: Optional[dict], cues) -> TurnPlan:
        k = self.k
        self.stats["plans"] += 1
        main = next((l for l in reversed(task.new_lines) if l.person_key), None)
        pk = main.person_key if main else None
        q = quick or {}
        try:
            await self._evaluate_previous(task, q, pk)
        except Exception:
            log.debug("outcome evaluation failed", exc_info=True)
        st = await self.state(task.thread_id)
        pd = await self.person(pk)
        now = k.clock.now()
        text = " ".join(l.text for l in task.new_lines if not l.mine)
        close = await self.m.understanding.closeness(pk) if pk else 0.3
        if task.trust <= Trust.UNTRUSTED:
            close = 0.0
        ckey, cname, chow = circle_of(close)
        from alive.core.dialogue.catalog import circle_membership
        cw = circle_membership(close)
        from alive.core.understanding import is_fragment_opener, open_topics
        fragment = bool(pk) and is_fragment_opener(task.new_lines, task.history, pk)
        genre = guess_genre(q, text, fragment)
        frame = guess_frame(genre, text, q)
        if fragment:
            await self._renqing_bump(pk, "care_received") if pk else None
        last_mine = next((l for l in reversed(task.history) if l.mine), None)
        to_me = task.chat_type == "private" or task.mentioned or bool(q.get("addressed_to_me"))
        pending_q_to_me = to_me and any(("？" in l.text or "?" in l.text) for l in task.new_lines if not l.mine)
        my_q_open = bool(last_mine and ("？" in last_mine.text or "?" in last_mine.text))
        invite_to_me = genre == "invite"
        sensitive = any(s.get("high_stakes") for s in (getattr(cues, "schemas", []) or []))
        pub, present = await self._audience(task, pk, st, now)
        stage = "front" if pub >= 0.5 else "back"            # a label for the record; behaviour uses publicness
        face_risk = 0.2 + (0.4 if genre in ("invite", "decline", "apology") else 0) + 0.2 * pub
        has_topics = bool(pk and open_topics(task.history, pk, k.clock.now(), k.clock))
        sit = {"stage": stage, "publicness": round(pub, 3), "circle": ckey, "closeness": round(close, 3),
               "circle_w": {c_: round(v, 3) for c_, v in cw.items()}, "genre": genre, "frame": frame,
               "need": self._need_guess(q, genre), "fragment": fragment, "pending_question_to_me": pending_q_to_me,
               "sensitive": sensitive, "face_risk": round(min(1.0, face_risk), 2), "mentioned": task.mentioned,
               "has_open_topics": has_topics}
        goal = await self._propose_goal(pd, sit, cues)
        sit["goal"] = goal["type"]
        sit["guiding_goal"] = goal["type"] in GUIDING_GOALS
        # topic ownership between us: a batch that opens after a gap is a topic he started
        first = task.new_lines[0] if task.new_lines else None
        prev_ts = max((l.ts for l in task.history if first is None or l.ts < first.ts), default=None)
        if pk and first is not None and (prev_ts is None or first.ts - prev_ts > 1800):
            pd.topic_owners = (pd.topic_owners + ["them"])[-10:]
        theirs_text = [l.text.strip() for l in task.new_lines if not l.mine]
        closing = bool(theirs_text) and (genre == "farewell" or all(_BACKCHANNEL.match(t) or is_ack_only(t)
                                                                     for t in theirs_text))
        agenda, opinions = await self.own_agenda(task, pk, text)
        agenda_pressure = min(1.0, 0.25 * len(agenda)) if agenda else 0.0
        agency, listen_lock, imbalance = self.compute_agency(sit, close, q, pd, agenda_pressure)
        claim, stance_open, stance_agency = self.stance_room(sit, close, q)
        tex = getattr(k, "texture", None)
        stance_note = self._stance_note(pk)
        sit.update({"claim": claim, "stance_open": stance_open, "stance_agency": stance_agency})
        sit.update({"agency": agency, "listen_lock": listen_lock, "topic_imbalance": imbalance, "closing": closing,
                    "has_agenda": bool(agenda), "has_opinion": bool(opinions), "agree_streak": bool(stance_note),
                    "playfulness_mult": tex.multiplier("playfulness", 0.8) if tex else 1.0})
        sugg = self.policy.suggest(sit, pk, pd.my_moves)
        rq = await self.renqing(pk) if pk else {}
        lines = [self._stage_line(pub, present), f"他在我的{cname}：{chow}"]
        lines.append(f"这句话的体裁像是「{GENRES[genre]['name']}」，期待的回应：{GENRES[genre]['expect']}；"
                     f"气氛像是「{FRAMES.get(frame, frame)}」")
        seq = []
        if pending_q_to_me:
            seq.append("他在问我：问到我了，一般要接住，怎么接、接多少由我")
        if invite_to_me:
            seq.append("他在邀约你：接受，或者稍缓一点、带个理由婉拒")
        if my_q_open and not pending_q_to_me:
            seq.append("我之前问过他的，他现在回了")
        if seq:
            lines.append("眼下：" + "；".join(seq))
        if face_risk >= 0.5:
            lines.append("这件事有点伤面子的风险：说得委婉些，给对方留台阶")
        lines.append(f"我和他此刻想要的：{goal['aim']}（{GOAL_TYPES.get(goal['type'], goal['type'])}）" +
                     (f"；已经推进了约 {goal.get('progress', 0):.0%}" if goal.get("progress") else ""))
        if goal.get("next"):
            lines.append(f"我之前打算接下来：{goal['next']}")
        if rq.get("i_owe"):
            lines.append("我还欠他的：" + "；".join(rq["i_owe"]))
        if rq and rq.get("care_received", 0) > rq.get("care_given", 0) + 2:
            lines.append("最近都是他主动来找我，我也该多关心他一些")
        if listen_lock and stance_open:
            lines.append(f"此刻以他为主：先接住他的感受，不提自己的事。他的感受和他下的判断（{claim}）是两回事："
                         "感受不用争；那个判断我自己怎么看就是怎么看，同意、只同意一部分、或者对事不对人都可以，不必为了安慰而全盘附和")
        elif listen_lock:
            lines.append("此刻以他为主：先把他接住，不提自己的事，不急着发表看法")
        else:
            lines.append(self._agency_line(agency))
        if not listen_lock and agenda:
            lines.append("我自己心里的事（想聊才提，带个过渡）：" + "；".join(agenda))
        if claim and not listen_lock:
            lines.append(f"他亮出的判断：{claim}。我自己怎么看就怎么说，真同意就同意，不必附和也不必为了有主见而反对")
        if opinions and (not listen_lock or stance_open):
            lines.append("我对这个话题以前的看法：" + "；".join(opinions))
        if closing and not listen_lock:
            lines.append("他这几句只是应和、没有新内容，这个话题可能聊到头了")
        if stance_note and (not listen_lock or stance_open):
            lines.append(stance_note)
        lines.append("可以考虑的话步：" + "；".join(f"{m}（{MOVES[m]}）" for m, _ in sugg) +
                     "。从中选一两步组合，或者有更好的就用更好的。")
        if goal["type"] in GUIDING_GOALS:
            lines.append("引导只用提问和真诚的回应，让他自己想到，不要直接告诉他该怎么想")
        lookahead = bool(getattr(cues, "deep", False)) or goal["type"] in GUIDING_GOALS
        await self._save_person(pd)
        await self._save(st)
        return TurnPlan(text="\n".join(lines), situation=sit, suggestions=sugg, lookahead=lookahead)

    @staticmethod
    def _need_guess(q: dict, genre: str) -> str:
        return {"confiding": "listen", "venting": "listen", "good_news": "celebrate", "request": "advice",
                "question": "info", "banter": "fun", "chitchat": "fun", "farewell": "space", "decline": "space",
                "greeting": "company", "fragment": "company", "story": "listen"}.get(genre, "company")

    # ------------------------------------------------------------------ after the reply
    async def after_reply(self, task: ReplyTask, plan: Optional[TurnPlan], data: dict, event_id: str,
                          quick: Optional[dict]) -> None:
        if plan is None:
            return
        st = await self.state(task.thread_id)
        reading = data.get("reading") if isinstance(data.get("reading"), dict) else {}
        if reading.get("genre") in GENRES:
            st.genre = reading["genre"]
        if reading.get("frame") in FRAMES:
            st.frame = reading["frame"]
        pk = next((l.person_key for l in reversed(task.new_lines) if l.person_key), None) or \
            (task.participants[0] if task.chat_type == "private" and task.participants else None)
        pd = await self.person(pk)
        now = self.k.clock.now()
        moves = [m for m in (data.get("moves") or []) if m in MOVES][:3]
        cur = self._goal_of(pd)
        g = data.get("goal") if isinstance(data.get("goal"), dict) else None
        if g and g.get("type") in GOAL_TYPES:
            cand = {**(cur or {}), **{k_: g[k_] for k_ in ("type", "aim", "progress", "done", "abandon") if k_ in g}}
            cand.setdefault("since", now)
            if self.ethics_ok(cand):
                cur = cand
            else:
                self.stats["goal_rejected"] += 1
                await self.k.bus.emit("core.goal_rejected", {"goal": cand}, source="dialogue", thread_id=task.thread_id)
        if cur is not None:
            cur["touched"] = now
            cur["fade_at"] = self._fade_at(cur, now)
            cur["next"] = str(data.get("plan_next") or "")[:120]
            pd.goal = None if (cur.get("done") or cur.get("abandon")) else cur
        silent = bool(data.get("silent")) or not data.get("reply")
        if silent and not moves:
            moves = ["wait"]
        pd.my_moves = (pd.my_moves + moves)[-8:]
        self.policy.note_used(moves)
        stance = str(data.get("stance") or "")
        if stance:
            self.stances.setdefault(pk or "", []).append(stance)
            self.stances[pk or ""] = self.stances[pk or ""][-30:]
        if stance == "own_topic" or any(m in ("self_topic", "shift") for m in moves):
            pd.topic_owners = (pd.topic_owners + ["me"])[-10:]
        log_id = new_id("mv")
        sit = plan.situation
        await self.k.db.execute(
            "INSERT INTO core_moves (id,ts,thread_id,person_key,genre,frame,need,circle,moves,goal,sentiment) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (log_id, self.k.clock.now(), task.thread_id, pk, sit.get("genre"), sit.get("frame"), sit.get("need"),
             sit.get("circle"), json.dumps(moves), json.dumps(cur or {}, ensure_ascii=False),
             float((quick or {}).get("sentiment") or 0.0)))
        if silent:     # a silence is not a move toward him that he could answer
            await self.k.db.execute("UPDATE core_moves SET reward=-1, detail=? WHERE id=?", ('{"silent":true}', log_id))
        await self._save_person(pd)
        await self._save(st)

    # ------------------------------------------------------------------ lookahead (docs/19 §6)
    async def lookahead(self, task: ReplyTask, candidates: Sequence[dict], blocks: dict, goal_aim: str) -> Optional[int]:
        """Simulate his reaction to each candidate; choose among them by a softmax over (goal progress - risk) -
        clearly better candidates usually win, close calls are a matter of the moment (T 0.15, wider when
        aroused)."""
        from alive.core import prompts
        self.stats["lookahead"] += 1
        name = await self.k.social.person_name(task.participants[0]) if task.participants else "对方"
        chat = self.m.ctx.render_chat(task.history[-10:], task.new_lines, self.k.selfm.name(), max_chars=1800)

        async def sim(c):
            reply = c.get("reply")
            reply = "／".join(reply) if isinstance(reply, list) else str(reply)
            return await self.k.llm.json("core.lookahead", prompts.LOOKAHEAD.format(
                name=name, relationship=blocks.get("relationship", ""), understanding=blocks.get("understanding", ""),
                chat=chat, candidate=reply, goal=goal_aim), tier="fast",
                schema={"predicted_reply": "", "feeling": "", "goal_progress": 0.5, "risk": 0.2}, priority=3)
        results = await asyncio.gather(*(sim(c) for c in candidates[:3]), return_exceptions=True)
        scored = []
        for i, r in enumerate(results):
            if isinstance(r, Exception) or not isinstance(r, dict):
                continue
            v = float(r.get("goal_progress") or 0) - float(r.get("risk") or 0)
            candidates[i]["simulated"] = r
            scored.append((v, i))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        try:
            arousal = float(self.k.affect.snapshot().A)
        except Exception:
            arousal = 0.0
        pick = C.softmax_choice(self._rng(), [i for _, i in scored], [v for v, _ in scored],
                                0.15 * (1 + 0.25 * arousal))
        return pick

    def status(self) -> dict:
        return dict(self.stats)
