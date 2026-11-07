"""Level-0 understanding cues (docs/17 §3.1, §4, §8): per-person baselines and group context. No model calls.

PersonBaseline (A-REL-03) — incremental statistics of how a person usually writes:
    message length quartiles, burst size, reply latency, emoji / laugh / punctuation habits, catchphrases.
Deviation rules (docs/17 §4): length < ½·p25 or > 2·p75; latency > 3× (notable) / > 10× (unusual silence)
the usual; local hour with < 2 % of their history; usual laugh missing; a sentence-final "。" from someone
who never uses it; a burst ≥ 2× usual (≥ 4). Fewer than 15 samples -> weak cues, flagged.

GroupContext (A-REL-04): active members (30 min), my share vs fair share (warn > 1.5×), who the latest
message addresses, and a heuristic frame (joking | serious | venting | planning | quiet).
"""
from __future__ import annotations

import re
import statistics
from collections import deque
from typing import Optional, Sequence

from alive.social import text as T

_LAUGH = re.compile(r"(哈哈|嘿嘿|hhh|233|笑死|lol|lmao|😂|🤣|xswl)", re.I)
_PLAN = re.compile(r"(几点|哪里|在哪|约|周末|明天|今晚|集合|出发|见面|安排)")
_VENT = re.compile(r"(烦|累死|崩溃|无语|气死|吐了|服了|离谱|裂开|难受)")
_STOP_PHRASES = {"一个", "这个", "那个", "我们", "你们", "他们", "什么", "就是", "然后", "因为", "所以", "可以",
                 "没有", "不是", "知道", "觉得", "现在", "今天"}


def _q(xs: list, q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = q * (len(s) - 1)
    lo = int(i)
    hi = min(len(s) - 1, lo + 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


class PersonBaseline:
    RATE = 0.08  # EWMA step for habit rates (~ last 25 messages)

    def __init__(self, d: Optional[dict] = None):
        d = d or {}
        self.n = int(d.get("n", 0))
        self.lengths = deque(d.get("lengths", []), maxlen=120)
        self.bursts = deque(d.get("bursts", []), maxlen=40)
        self.latencies = deque(d.get("latencies", []), maxlen=40)
        self.gaps = deque(d.get("gaps", []), maxlen=60)    # pauses between the pieces of one split utterance
        self.rates = dict(d.get("rates", {}))
        self.phrases = dict(d.get("phrases", {}))
        self.updated = float(d.get("updated", 0.0))

    def to_dict(self) -> dict:
        return {"n": self.n, "lengths": list(self.lengths), "bursts": list(self.bursts),
                "latencies": [round(x, 1) for x in self.latencies], "gaps": [round(x, 1) for x in self.gaps],
                "rates": {k: round(v, 4) for k, v in self.rates.items()},
                "phrases": self.phrases, "updated": self.updated}

    def _rate(self, key: str, hit: bool) -> None:
        r = self.rates.get(key)
        x = 1.0 if hit else 0.0
        self.rates[key] = x if r is None else r + self.RATE * (x - r)

    def observe(self, text: str, ts: float) -> None:
        t = (text or "").strip()
        if not t:
            return
        self.n += 1
        self.updated = ts
        self.lengths.append(len(t))
        self._rate("emoji", bool(T._EMOJI.search(t)) or "[表情" in t)
        self._rate("laugh", bool(_LAUGH.search(t)))
        self._rate("exclaim", "!" in t or "！" in t)
        self._rate("tilde", "~" in t or "～" in t)
        self._rate("ellipsis", "..." in t or "…" in t or "。。" in t)
        self._rate("period_end", t.endswith("。"))
        for tok in T.tokens(t):
            if len(tok) >= 2 and tok not in _STOP_PHRASES:
                self.phrases[tok] = self.phrases.get(tok, 0) + 1
        if len(self.phrases) > 400:
            keep = sorted(self.phrases.items(), key=lambda kv: (-kv[1], kv[0]))[:200]
            self.phrases = {k: max(1, v // 2) for k, v in keep}

    def observe_burst(self, n: int) -> None:
        if n > 0:
            self.bursts.append(int(n))

    def observe_latency(self, seconds: float) -> None:
        if seconds > 0:
            self.latencies.append(float(seconds))

    def observe_gap(self, seconds: float) -> None:
        """Pause between two consecutive lines of theirs inside one burst (how they split what they say)."""
        if 0 < seconds < 120:
            self.gaps.append(float(seconds))

    @property
    def latency_median(self) -> Optional[float]:
        """Their typical reply latency to me (needs a few samples)."""
        return statistics.median(self.latencies) if len(self.latencies) >= 3 else None

    @property
    def gap_median(self) -> Optional[float]:
        return statistics.median(self.gaps) if len(self.gaps) >= 5 else None

    @property
    def burst_mean(self) -> Optional[float]:
        return (sum(self.bursts) / len(self.bursts)) if len(self.bursts) >= 5 else None

    def summary(self) -> dict:
        ls = list(self.lengths)
        cps = [k for k, v in sorted(self.phrases.items(), key=lambda kv: (-kv[1], kv[0]))
               if self.n and v / self.n >= 0.15][:5]
        return {"n_messages": self.n, "len_median": _q(ls, 0.5), "len_p25": _q(ls, 0.25), "len_p75": _q(ls, 0.75),
                "burst_median": statistics.median(self.bursts) if self.bursts else 1,
                "reply_latency_median": statistics.median(self.latencies) if self.latencies else None,
                "emoji_rate": self.rates.get("emoji", 0.0), "laugh_rate": self.rates.get("laugh", 0.0),
                "punct": {k: self.rates.get(k, 0.0) for k in ("exclaim", "tilde", "ellipsis", "period_end")},
                "catchphrases": cps, "reliable": self.n >= 15, "updated_at": self.updated}


def deviation_cues(b: PersonBaseline, texts: Sequence[str], *, name: str = "TA", latency: Optional[float] = None,
                   hour_share: Optional[float] = None, gap_since_contact: Optional[float] = None) -> list:
    """Chinese cue strings for the reply prompt; empty when nothing stands out."""
    cues: list = []
    s = b.summary()
    texts = [t for t in texts if (t or "").strip()]
    if texts and s["n_messages"] >= 5:
        ln = sum(len(t.strip()) for t in texts) / len(texts)
        if s["len_p25"] and ln < 0.5 * s["len_p25"]:
            cues.append(f"{name}这次比平时短很多：约 {int(ln)} 字，平时约 {int(s['len_median'])} 字")
        elif s["len_p75"] and ln > 2 * s["len_p75"]:
            cues.append(f"{name}这次写得比平时长很多：约 {int(ln)} 字，平时约 {int(s['len_median'])} 字")
        if len(texts) >= 4 and len(texts) >= 2 * float(s["burst_median"] or 1):
            cues.append(f"{name}一口气连发了 {len(texts)} 条")
        if s["laugh_rate"] > 0.5 and len(texts) >= 2 and not any(_LAUGH.search(t) for t in texts):
            cues.append(f"{name}平时常说“哈哈”，这几条都没有")
        if s["punct"]["period_end"] < 0.1 and s["n_messages"] >= 10 and any(t.strip().endswith("。") for t in texts):
            cues.append(f"{name}平时不用句号结尾，这次用了（可能有点冷淡或认真）")
    med = s["reply_latency_median"]
    if latency is not None and med:
        if latency > 10 * med:
            cues.append(f"{name}隔了很久才回（约 {_dur(latency)}，平时约 {_dur(med)}）")
        elif latency > 3 * med:
            cues.append(f"{name}比平时晚回了不少（约 {_dur(latency)}，平时约 {_dur(med)}）")
    if hour_share is not None and s["n_messages"] >= 15 and hour_share < 0.02:
        cues.append(f"这个时间点{name}平时很少在线")
    if gap_since_contact is not None and gap_since_contact > 7 * 86400:
        cues.append(f"这是很久没联系（约 {int(gap_since_contact // 86400)} 天）后{name}第一次来消息")
    if cues and not s["reliable"]:
        cues.append(f"（对{name}还不太了解，以上只作弱参考）")
    return cues


def _dur(s: float) -> str:
    if s < 90:
        return f"{int(s)} 秒"
    if s < 5400:
        return f"{int(s // 60)} 分钟"
    return f"{s / 3600:.1f} 小时"


def group_context(lines: Sequence, now: float, *, names: dict, last_addressed: str = "",
                  window_s: float = 1800.0) -> dict:
    """lines: ChatLines (oldest first). names: person_key -> display name."""
    win = [l for l in lines if now - l.ts <= window_s]
    others = [l for l in win if not l.mine]
    active = list(dict.fromkeys(l.person_key for l in others if l.person_key))
    mine = sum(1 for l in win if l.mine)
    share = mine / len(win) if win else 0.0
    fair = 1.0 / (len(active) + 1)
    recent = [l.text or "" for l in others[-12:]]
    if not others or now - others[-1].ts > 600:
        frame = "quiet"
    elif sum(1 for t in recent if _LAUGH.search(t)) >= max(2, len(recent) // 3):
        frame = "joking"
    elif sum(1 for t in recent if _VENT.search(t)) >= 2:
        frame = "venting"
    elif sum(1 for t in recent if _PLAN.search(t)) >= 2:
        frame = "planning"
    elif recent and sum(len(t) for t in recent) / len(recent) > 30:
        frame = "serious"
    else:
        frame = "chatting"
    return {"active_members": [names.get(k, k) for k in active], "frame": frame, "my_share": round(share, 3),
            "fair_share": round(fair, 3), "over_share": share > 1.5 * fair and mine >= 2, "mine": mine,
            "addressed_to": last_addressed}


FRAME_WORDS = {"quiet": "冷场", "joking": "大家在开玩笑", "venting": "有人在吐槽", "planning": "在约事情",
               "serious": "在认真讨论", "chatting": "随便闲聊"}


def group_cues(ctx: dict, rng=None, sensitivity: float = 1.0) -> list:
    """Cue strings for a shared space. Whether I NOTICE that I've been talking a lot is itself a draw (self-
    monitoring varies by person and moment, ADR-0007): p = σ(3·(share/fair − 1.4)) × (0.6 + 0.4·sensitivity),
    and the cue states the fact; holding back is my choice (Initiative.yield_floor, the mind), not an order."""
    cues = [f"群里的气氛：{FRAME_WORDS.get(ctx['frame'], ctx['frame'])}"]
    fair = float(ctx.get("fair_share") or 0.0)
    ratio = float(ctx.get("my_share") or 0.0) / fair if fair > 0 else 0.0
    if ctx.get("mine", 2) >= 2 and ratio > 1.0:
        import math as _m
        p = 1.0 / (1.0 + _m.exp(-3.0 * (ratio - 1.4))) * min(1.0, 0.6 + 0.4 * max(0.0, sensitivity))
        noticed = (rng.random() < p) if rng is not None else bool(ctx.get("over_share"))
        if noticed:
            cues.append(f"我最近在这儿说得比别人多（占 {int(ctx['my_share'] * 100)}%，平均每人约 {int(fair * 100)}%）")
    if ctx.get("addressed_to"):
        cues.append(f"最新一条是在对{ctx['addressed_to']}说")
    return cues
