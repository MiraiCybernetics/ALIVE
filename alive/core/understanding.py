"""Understanding engine (A-CORE-08, docs/17): reading what a message means beyond its words.

Level 0 (always, no LLM): person baseline deviations, time context, ongoing storylines, group
context, matched social schemas  ->  a cues block + a deep-read trigger decision.
Level 1 (every reply): the reply call itself must output a structured ``reading`` before the reply.
Level 2 (triggered): a separate deep read with weighted hypotheses + perspective taking.
Level 3 (with level 2): the reply call proposes candidates with predicted reactions; best wins.
Learning: readings are recorded and checked against what happened next during sleep.
"""
from __future__ import annotations

import logging
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

import yaml

from alive.core import prompts
from alive.core.types import ChatLine, ReplyTask, new_id
from alive.runtime import chance as C
from alive.runtime.store import register_schema
from alive.world.audience import origins_of

log = logging.getLogger("alive.core.understanding")

register_schema("core_understanding", 1, [
    """CREATE TABLE IF NOT EXISTS core_readings (
        id TEXT PRIMARY KEY, ts REAL, person_key TEXT, thread_id TEXT, event_id TEXT,
        feeling TEXT, need TEXT, hypothesis TEXT, deep INTEGER, reviewed INTEGER DEFAULT 0, verdict TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_core_readings_ts ON core_readings(ts)",
])

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]|\\[[^\\]]{1,6}\\]")  # unicode emoji or [表情]
_LAUGH = re.compile(r"哈{2,}|hhh+|233+|笑死|lol|haha", re.I)
_TOKEN = re.compile(r"[一-鿿]{2}|[A-Za-z]{3,}")
STOP = set("我们你们他们这个那个就是然后但是因为所以还是什么怎么一个没有可以知道觉得现在今天明天真的")


@dataclass
class PersonBaseline:
    n: int = 0
    len_median: float = 0.0
    len_p25: float = 0.0
    len_p75: float = 0.0
    latency_median: Optional[float] = None
    active_hours: list = field(default_factory=lambda: [0.0] * 24)
    emoji_rate: float = 0.0
    laugh_rate: float = 0.0
    period_end_rate: float = 0.0
    exclaim_rate: float = 0.0
    burst_median: float = 1.0
    catchphrases: list = field(default_factory=list)
    last_seen: float = 0.0

    @property
    def reliable(self) -> bool:
        return self.n >= 15


def compute_baseline(lines: Sequence[ChatLine], person_key: str, clock) -> PersonBaseline:
    """Build a style/rhythm baseline for one person from chat history (theirs + mine for latency)."""
    return compute_person_baseline({"": list(lines)}, person_key, clock)


def compute_person_baseline(by_space: dict, person_key: str, clock) -> PersonBaseline:
    """A person's baseline from their lines in every space ({space: [ChatLine]}, theirs + mine). Style and rhythm
    are the person's; reply latency and bursts are measured within each space (my line in one place followed by
    theirs in another is not a reply)."""
    theirs = sorted((l for ls in by_space.values() for l in ls if l.person_key == person_key and not l.mine),
                    key=lambda l: l.ts)
    b = PersonBaseline(n=len(theirs))
    if not theirs:
        return b
    lens = sorted(len(l.text) for l in theirs)
    q = lambda p: lens[min(len(lens) - 1, int(p * (len(lens) - 1)))]
    b.len_median, b.len_p25, b.len_p75 = float(statistics.median(lens)), float(q(0.25)), float(q(0.75))
    hours = [0.0] * 24
    for l in theirs:
        hours[clock.local(l.ts).hour] += 1
    b.active_hours = [h / len(theirs) for h in hours]
    b.emoji_rate = sum(1 for l in theirs if _EMOJI.search(l.text)) / len(theirs)
    b.laugh_rate = sum(1 for l in theirs if _LAUGH.search(l.text)) / len(theirs)
    b.period_end_rate = sum(1 for l in theirs if l.text.rstrip().endswith("。")) / len(theirs)
    b.exclaim_rate = sum(1 for l in theirs if "!" in l.text or "！" in l.text) / len(theirs)
    # latency: their first line after one of my lines; bursts: consecutive lines from them (per space)
    lat, bursts = [], []
    for lines in by_space.values():
        ordered = sorted(lines, key=lambda l: l.ts)
        last_mine = None
        for l in ordered:
            if l.mine:
                last_mine = l.ts
            elif l.person_key == person_key and last_mine is not None:
                lat.append(l.ts - last_mine)
                last_mine = None
        cur = 0
        for l in ordered:
            if l.person_key == person_key and not l.mine:
                cur += 1
            elif cur:
                bursts.append(cur)
                cur = 0
        if cur:
            bursts.append(cur)
    if len(lat) >= 3:
        b.latency_median = float(statistics.median(lat))
    b.burst_median = float(statistics.median(bursts)) if bursts else 1.0
    counts: dict = {}
    for l in theirs:
        for t in set(_TOKEN.findall(l.text)):
            if t not in STOP:
                counts[t] = counts.get(t, 0) + 1
    b.catchphrases = [t for t, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= max(3, len(theirs) * 0.15)][:5]
    b.last_seen = theirs[-1].ts
    return b


def deviations(b: PersonBaseline, new: Sequence[ChatLine], history: Sequence[ChatLine], clock, now: float) -> list:
    """Human-readable signals where the new lines deviate from the person's own baseline (docs/17 §4)."""
    out: list = []
    if not new:
        return out
    weak = "" if b.reliable else "（还不太了解他，仅供参考）"
    text = "".join(l.text for l in new)
    n_chars = sum(len(l.text) for l in new) / len(new)
    if b.n >= 5:
        if b.len_p25 and n_chars < b.len_p25 * 0.5 and n_chars <= 6:
            out.append(f"这次说得比平时短很多（约 {n_chars:.0f} 字，平时约 {b.len_median:.0f} 字）{weak}")
        elif b.len_p75 and n_chars > b.len_p75 * 2 and n_chars > 40:
            out.append(f"这次说了比平时长得多的一段话（约 {n_chars:.0f} 字，平时约 {b.len_median:.0f} 字）{weak}")
        if len(new) >= max(4, b.burst_median * 3):
            out.append(f"一口气连发了 {len(new)} 条{weak}")
        if b.laugh_rate > 0.5 and not _LAUGH.search(text) and len(new) >= 2:
            out.append(f"平时常说'哈哈'，这次一个都没有{weak}")
        if b.period_end_rate < 0.1 and any(l.text.rstrip().endswith("。") for l in new):
            out.append(f"平时不用句号结尾，这次用了（中文网络里常显得冷淡或认真）{weak}")
        if b.emoji_rate > 0.5 and not _EMOJI.search(text) and len(new) >= 2:
            out.append(f"平时爱用表情，这次没用{weak}")
        h = clock.local(new[0].ts).hour
        if b.reliable and b.active_hours[h] < 0.02:
            out.append(f"这个时间（{h} 点）他平时几乎不发消息")
    # latency relative to his own average (Kalman chronemic zones)
    last_mine = max((l.ts for l in history if l.mine), default=None)
    first_new = new[0].ts
    if last_mine is not None and b.latency_median and first_new > last_mine:
        lat = first_new - last_mine
        ratio = lat / max(30.0, b.latency_median)
        if ratio > 10:
            out.append(f"隔了很久才回（约 {lat/3600:.1f} 小时，平时约 {b.latency_median/60:.0f} 分钟）")
        elif ratio > 3:
            out.append(f"回得比平时慢（约 {lat/60:.0f} 分钟，平时约 {b.latency_median/60:.0f} 分钟）")
    if b.last_seen and new[0].ts - b.last_seen > 14 * 86400:
        out.append(f"已经 {int((new[0].ts - b.last_seen)//86400)} 天没联系了，这是很久以来的第一条")
    return out


def time_context(clock, ts: float) -> list:
    d = clock.local(ts)
    out = []
    if d.hour >= 23 or d.hour < 5:
        out.append("现在是深夜")
    elif d.hour < 8:
        out.append("现在是清早")
    if d.weekday() >= 5:
        out.append("今天是周末")
    return out


def group_context(history: Sequence[ChatLine], new: Sequence[ChatLine], now: float, my_name: str) -> dict:
    recent = [l for l in list(history) + list(new) if now - l.ts < 1800]
    counts: dict = {}
    for l in recent:
        key = "me" if l.mine else (l.sender or l.person_key)
        counts[key] = counts.get(key, 0) + 1
    active = [k for k in counts if k != "me"]
    total = sum(counts.values()) or 1
    my_share = counts.get("me", 0) / total
    fair = 1.0 / (len(active) + 1)
    last = new[-1].text if new else ""
    m = re.findall(r"@(\S{1,16})", last)
    addressed = [x for x in m if my_name not in x]
    return {"active_members": active, "my_share": round(my_share, 2), "fair_share": round(fair, 2),
            "addressed_to_others": addressed, "msgs_30min": total}


_TIMEWORD = re.compile(r"今天|明天|后天|今晚|下午|上午|晚上|待会|等会|一会儿|周[一二三四五六日末]|考|面试|答辩|医院|结果|出发|到了|"
                       r"tomorrow|tonight|later")


def open_topics(history: Sequence[ChatLine], person_key: str, now: float, clock, gap_s: float = 3600,
                window_s: float = 72 * 3600, limit: int = 3) -> list:
    """Questions-under-discussion for fragment resolution (docs/18 §5): informative lines from recent sessions."""
    lines = [l for l in history if now - l.ts < window_s and (l.mine or l.person_key == person_key)]
    if not lines:
        return []
    lines.sort(key=lambda l: l.ts)
    sessions, cur = [], [lines[0]]
    for a, b in zip(lines, lines[1:]):
        if b.ts - a.ts > gap_s:
            sessions.append(cur)
            cur = []
        cur.append(b)
    sessions.append(cur)
    picked = []
    for sess in reversed(sessions[-4:]):
        scored = sorted(sess, key=lambda l: (bool(_TIMEWORD.search(l.text)) * 30 + min(len(l.text), 40)), reverse=True)
        for l in scored[:2]:
            if len(l.text) >= 4:
                picked.append(l)
        if len(picked) >= limit:
            break
    out = []
    for l in sorted(picked[:limit], key=lambda l: l.ts):
        ago = now - l.ts
        when = f"{ago/3600:.0f} 小时前" if ago >= 3600 else f"{max(1, ago/60):.0f} 分钟前"
        who = "我说" if l.mine else "他说"
        out.append(f"[{when}] {who}：{l.text[:60]}")
    return out


def is_fragment_opener(new: Sequence[ChatLine], history: Sequence[ChatLine], person_key: str, gap_s: float = 3600) -> bool:
    """He starts a conversation himself with a very short, context-dependent line (e.g. "无了")."""
    if not new:
        return False
    first = new[0]
    prev = [l for l in history if l.ts < first.ts and l.msg_id != first.msg_id]
    last_ts = max((l.ts for l in prev), default=None)
    started_by_him = last_ts is None or first.ts - last_ts > gap_s
    text = "".join(l.text for l in new if l.person_key == person_key).strip()
    short = 0 < len(text) <= 8
    return started_by_him and short


def deep_read_probability(*, sentiment: float = 0.0, venting: bool = False, strong_deviations: int = 0,
                          high_stakes: bool = False, bad_news: bool = False, closeness: float = 0.3,
                          energy: float = 0.7, crisis: bool = False) -> tuple:
    """(p, reasons): how likely I stop and read a message carefully (docs/17 §3.3). Smooth in every signal - how
    negative it sounds, venting, how far it is from their usual, a sensitive situation, bad news from someone
    close - and modulated by energy (tired, I read less carefully). No signal is a switch on its own; a possible
    crisis is handled by the caller as certain (safety protocol)."""
    neg = max(0.0, -float(sentiment))
    z = (-3.5 + 4.0 * neg + 1.6 * bool(venting) + 0.9 * min(3, max(0, int(strong_deviations)))
         + 2.2 * bool(high_stakes) + 2.4 * max(0.0, min(1.0, float(closeness))) * bool(bad_news)
         + 1.5 * (max(0.0, min(1.0, float(energy))) - 0.5))
    reasons = []
    if neg >= 0.25:
        reasons.append("情绪明显负面" if neg >= 0.4 else "情绪有点低")
    if venting:
        reasons.append("在倾诉或抱怨")
    if crisis:
        reasons.append("可能有危机信号")
    if strong_deviations >= 2:
        reasons.append("和平时差别很大")
    if high_stakes:
        reasons.append("敏感情境")
    if bad_news and closeness >= 0.45:
        reasons.append("亲近的人遇到坏事")
    return (1.0 if crisis else C.sigmoid(z)), reasons


def register_hint(closeness: float) -> str:
    if closeness >= 0.6:
        return "很熟：可以很随意，一两个字、半句话都行，不用客气"
    if closeness >= 0.3:
        return "算是朋友：自然随意，但别太亲昵"
    return "还不太熟：礼貌自然，不过分随便，也不过分热情"


class SchemaLibrary:
    def __init__(self, kernel):
        self.k = kernel
        self.items: list = []
        self._vecs = None

    def load(self) -> None:
        base = os.path.join(os.path.dirname(__file__), "schemas", "zh.yaml")
        items = {s["id"]: s for s in (yaml.safe_load(open(base, encoding="utf-8")) or [])}
        pdir = self.k.persona_dir or ""
        p = os.path.join(pdir, "schemas.yaml")
        if os.path.exists(p):
            for s in yaml.safe_load(open(p, encoding="utf-8")) or []:
                items[s["id"]] = {**items.get(s["id"], {}), **s}
        self.items = list(items.values())
        for it in self.items:  # YAML may parse triggers like 666 as numbers
            it["triggers"] = [str(t) for t in (it.get("triggers") or []) if str(t).strip()]
        texts = [f"{s.get('name','')} {' '.join(s.get('meanings', []))}" for s in self.items]
        try:
            self._vecs = self.k.embedder.embed_sync(texts) if hasattr(self.k.embedder, "embed_sync") else None
        except Exception:
            self._vecs = None

    def match(self, text: str, limit: int = 2, extra_ids: Sequence[str] = ()) -> list:
        if not self.items:
            return []
        scored = []
        for i, s in enumerate(self.items):
            kw = sum(1 for t in s.get("triggers", []) if t and t in text)
            score = min(2, kw) * 1.0
            if s["id"] in extra_ids:
                score += 1.5
            scored.append((score, i))
        if self._vecs is not None and text.strip():
            try:
                q = self.k.embedder.embed_sync([text])[0]
                sims = self._vecs @ q
                scored = [(sc + (0.6 * float(sims[i]) if sims[i] > 0.25 else 0.0), i) for sc, i in scored]
            except Exception:
                pass
        best = [self.items[i] for sc, i in sorted(scored, key=lambda x: -x[0]) if sc >= 0.9][:limit]
        return best

    def menu(self) -> str:
        """Stable list of all situation ids for the S1 reader (same text every call -> cacheable)."""
        return "；".join(f"{s['id']}（{s.get('name','')}）" for s in self.items)

    def by_ids(self, ids: Sequence[str]) -> list:
        want = [i for i in ids if isinstance(i, str)]
        return [s for s in self.items if s["id"] in want]

    @staticmethod
    def render(items: Sequence[dict]) -> str:
        out = []
        for s in items:
            out.append(f"「{s['name']}」可能的意思：{'；'.join(s.get('meanings', []))}。"
                       f"宜：{'；'.join(s.get('do', []))}。忌：{'；'.join(s.get('avoid', []))}。")
        return "\n".join(out)


@dataclass
class Cues:
    text: str
    deep: bool
    reasons: list
    schemas: list
    deviations: list
    group: Optional[dict] = None
    high_stakes: bool = False
    p_deep: float = 0.0


class Understanding:
    def __init__(self, mind):
        self.m = mind
        self.k = mind.k
        self.cfg = self.k.cfg.section("understanding")
        self.schemas = SchemaLibrary(self.k)
        self._baselines: dict = {}      # person_key -> (baseline, computed_at, n_history)
        self._closeness: tuple = (0.0, {})
        self._story: dict = {}          # (person_key, space) -> (ts, [str], [origins])
        self._lines: dict = {}          # person_key -> (ts, {space: [ChatLine]}) their lines in every space
        self.rng = None                 # tests inject; default: the life's rng (alive/runtime/chance.py)
        self.stats = {"cues": 0, "deep": 0, "candidates": 0}

    async def start(self) -> None:
        try:
            self.schemas.load()
        except Exception:
            log.exception("schema library failed to load")

    # ------------------------------------------------------------------ level 0
    async def person_lines(self, person_key: str) -> dict:
        """{space: [ChatLine]}: this person's lines in every space, with mine around them (docs/22 §1: the person
        is the unit, not the space). Short technical cache; falls back to nothing when social can't tell."""
        now = self.k.clock.now()
        c = self._lines.get(person_key)
        if c and now - c[0] < 300:
            return c[1]
        fn = getattr(self.k.social, "person_lines", None)
        by: dict = {}
        if fn is not None:
            try:
                by = dict(await fn(person_key, limit=300) or {})
            except Exception:
                by = {}
        self._lines[person_key] = (now, by)
        if len(self._lines) > 2000:
            for key in sorted(self._lines, key=lambda x: self._lines[x][0])[:500]:
                self._lines.pop(key, None)
        return by

    async def baseline(self, person_key: str, thread_id: str) -> PersonBaseline:
        """The person's baseline from their lines in ANY space (docs/17 §4); without a cross-space source, from
        this space's history."""
        now = self.k.clock.now()
        cached = self._baselines.get(person_key)
        if cached and now - cached[1] < 1800:
            return cached[0]
        by = await self.person_lines(person_key)
        if not by:
            try:
                by = {thread_id: await self.k.social.history(thread_id, 300)}
            except Exception:
                by = {}
        b = compute_person_baseline(by, person_key, self.k.clock)
        self._baselines[person_key] = (b, now, sum(len(v) for v in by.values()))
        return b

    async def person_history(self, person_key: str, task: ReplyTask, scope) -> list:
        """Lines involving this person, for open topics and fragment detection: this space's history plus their
        lines (and mine around them) in other spaces whose audience covers this one (INV-34: what they told me
        one-to-one never becomes a "topic we have open" in a group)."""
        by = await self.person_lines(person_key)
        seen = {l.msg_id for l in task.history if l.msg_id} | {l.msg_id for l in task.new_lines if l.msg_id}
        out = list(task.history)
        for space, lines in by.items():
            if space != task.thread_id and scope is not None and not scope.covers_space(space):
                continue
            out += [l for l in lines if not (l.msg_id and l.msg_id in seen)]
        out.sort(key=lambda l: l.ts)
        return out

    async def closeness(self, person_key: str) -> float:
        """How close I am to this person now (the relationship's decayed closeness)."""
        now = self.k.clock.now()
        fn = getattr(self.k.social, "entity_context", None)
        if fn is not None and person_key:
            try:
                c = (await fn(person_key, now) or {}).get("closeness")
                if c is not None:
                    return float(c)
            except Exception:
                pass
        ts, mp = self._closeness
        if now - ts > 300:
            try:
                cands = await self.k.social.outreach_candidates(now, limit=200)
                mp = {c["person_key"]: float(c.get("closeness", 0.3)) for c in cands if "person_key" in c}
            except Exception:
                mp = {}
            self._closeness = (now, mp)
        return mp.get(person_key, 0.3)

    async def storylines(self, person_key: str, scope=None, withheld: Optional[list] = None,
                         origins: Optional[list] = None) -> list:
        """What is currently going on in this person's life (from memory + my open loops about them). With a
        ``scope`` (where I'm about to speak), only what everyone there may hear (INV-34); left-out contents go to
        ``withheld``, origins of what is used to ``origins``."""
        now = self.k.clock.now()
        space = scope.space if scope is not None else ""
        c = self._story.get((person_key, space))
        if c and now - c[0] < 600:
            if origins is not None:
                origins.extend(c[2])
            if withheld is not None:
                withheld.extend(x for x in c[3] if x not in withheld)
            return c[1]
        out: list = []
        used: list = []
        left: list = []
        try:
            kw = {"thread_id": scope.space or None, "present": list(scope.people), "withheld": left} \
                if scope is not None else {}
            recs = await self.k.memory.about_person(person_key, limit=20, **kw)
            def _get(r, k_, d=None):
                return getattr(r, k_, None) if not isinstance(r, dict) else r.get(k_, d)
            recent = [r for r in recs if (_get(r, "importance", 3) or 3) >= 5 or
                      now - (_get(r, "observed_at", 0) or _get(r, "ts", 0) or 0) < 7 * 86400]
            for r in recent[:6]:
                out.append(str(_get(r, "content", "")))
                used.extend(origins_of(r))
        except Exception:
            pass
        try:
            name = await self.k.social.person_name(person_key)
            for loop in (await self.k.motivation.open_loops(10)) or []:
                if name and name in loop:
                    out.append(f"我心里挂着：{loop}")
        except Exception:
            pass
        self._story[(person_key, space)] = (now, out[:8], used, left)
        if len(self._story) > 2000:
            for key in sorted(self._story, key=lambda x: self._story[x][0])[:500]:
                self._story.pop(key, None)
        if origins is not None:
            origins.extend(used)
        if withheld is not None:
            withheld.extend(x for x in left if x not in withheld)
        return out[:8]

    async def cues(self, task: ReplyTask, quick: Optional[dict], *, scope=None, withheld: Optional[list] = None,
                   origins: Optional[list] = None) -> Cues:
        """Level-0 cues. ``scope``: who would hear me here (alive/world/audience.py); what is used from other
        spaces is limited to what they may hear (INV-34)."""
        k = self.k
        now = k.clock.now()
        self.stats["cues"] += 1
        main = next((l for l in reversed(task.new_lines) if l.person_key), None)
        pk = main.person_key if main else None
        text = " ".join(l.text for l in task.new_lines)
        parts, reasons = [], []
        devs: list = []
        if pk:
            b = await self.baseline(pk, task.thread_id)
            devs = deviations(b, [l for l in task.new_lines if l.person_key == pk], task.history, k.clock, now)
            if devs:
                parts.append("和他平时不一样的地方：" + "；".join(devs))
            story = await self.storylines(pk, scope, withheld, origins)
            if story:
                parts.append("他最近的事：" + "；".join(story))
            hist = await self.person_history(pk, task, scope)          # the person's lines anywhere they may be used
            topics = open_topics(hist, pk, now, k.clock)
            if topics:
                parts.append("最近和他聊过、可能相关的话题：" + "；".join(topics))
            if is_fragment_opener(task.new_lines, hist, pk):
                parts.append("他主动开口，只说了很短的一句、没头没尾：通常是默认你记得前情（对照上面最近聊过的话题），"
                             "也可能是想找你说说话、被接住")
            close_v = await self.closeness(pk)
            style = []
            if b.n >= 5:
                style.append(f"一条消息一般约 {b.len_median:.0f} 字")
                if b.laugh_rate > 0.4:
                    style.append("常说'哈哈'")
                if b.emoji_rate > 0.4:
                    style.append("爱用表情")
                if b.catchphrases:
                    style.append("常用词：" + "、".join(b.catchphrases[:4]))
            parts.append("说话的分寸：" + register_hint(close_v) + (("；他平时" + "，".join(style)) if style else "") +
                         "；跟着他的节奏和长度说话")
        tctx = time_context(k.clock, task.new_lines[-1].ts if task.new_lines else now)
        if tctx:
            parts.append("情境：" + "，".join(tctx))
        group = None
        shared = scope.shared if (scope is not None and scope.kind) else task.chat_type == "group"
        if shared:
            group = group_context(task.history, task.new_lines, now, k.selfm.name())
            g = [f"最近半小时活跃的人：{('、'.join(group['active_members'][:6])) or '不多'}",
                 f"我最近的发言占比 {group['my_share']:.0%}（大家平均 {group['fair_share']:.0%}）"]
            if group["my_share"] > group["fair_share"] * 1.5 and group["msgs_30min"] >= 6:
                g.append("我最近说得有点多，收着点")
            if group["addressed_to_others"]:
                g.append(f"最新一条在对 {'、'.join(group['addressed_to_others'])} 说话")
            # audience design (privacy itself is enforced on what reaches the prompt, INV-34)
            g.append("我说的话这里的人都看得到，要让在场的人都接得住")
            parts.append("这里的情况：" + "；".join(g))
        extra = []
        if pk and devs and any("很久" in d for d in devs):
            extra.append("long_absence")
        picked = [s for s in ((quick or {}).get("situations") or []) if isinstance(s, str)][:2]
        schemas = self.schemas.match(text, limit=2, extra_ids=extra + picked)
        if schemas:
            parts.append("常识提示：\n" + SchemaLibrary.render(schemas))
        # deep read (docs/17 §3.3): whether I stop and read carefully is a draw, likelier the more the signals add
        # up and the more energy I have; a possible crisis always gets a careful read (safety protocol)
        q = quick or {}
        close = await self.closeness(pk) if pk else 0.3
        high = any(s.get("high_stakes") for s in schemas)
        strong_devs = len([d for d in devs if "仅供参考" not in d])
        crisis = bool(q.get("crisis") or q.get("crisis_hint"))
        p_deep, reasons = deep_read_probability(
            sentiment=float(q.get("sentiment") or 0.0), venting=q.get("intent") in ("emotional", "complaint"),
            strong_deviations=strong_devs, high_stakes=high, bad_news=bool(q.get("bad_news")), closeness=close,
            energy=self._energy(), crisis=crisis)
        rng = self.rng if self.rng is not None else C.rng_of(k)
        deep = bool(self.cfg.get("deep_enabled", True)) and (crisis or C.bernoulli(rng, p_deep))
        if deep and not reasons:
            reasons = ["心里有点在意，想认真读一下"]
        return Cues(text="\n".join(parts) or "（没什么特别的）", deep=deep, reasons=reasons, schemas=schemas,
                    deviations=devs, group=group, high_stakes=deep, p_deep=round(p_deep, 4))

    def _energy(self) -> float:
        try:
            return float(self.k.affect.snapshot().energy) if self.k.affect is not None else 0.7
        except Exception:
            return 0.7

    # ------------------------------------------------------------------ the people here (docs/17 §3.1)
    async def present_block(self, task: ReplyTask, focus: Optional[str], scope, *, withheld: Optional[list] = None,
                            origins: Optional[list] = None, max_people: int = 4) -> str:
        """"在场的人": a compact line for each relevant person who would hear me here - whoever just spoke or
        addressed me, the person I'm mainly answering, and people who were active here recently - ranked by how
        involved they are now, then closeness. ``max_people`` is a prompt-size limit. Facts are limited to what
        everyone here may hear (INV-34)."""
        k = self.k
        now = k.clock.now()
        shared = scope.shared if (scope is not None and scope.kind) else task.chat_type == "group"
        name_of: dict = {}
        involved: dict = {}
        for l in task.new_lines:
            if l.person_key and not l.mine:
                involved[l.person_key] = max(involved.get(l.person_key, 0.0), 2.0)
                name_of[l.person_key] = l.sender
        for l in reversed(task.history[-40:]):
            if l.person_key and not l.mine and now - l.ts <= 1800:
                involved.setdefault(l.person_key, 1.0 - min(1.0, (now - l.ts) / 1800.0) * 0.5)
                name_of.setdefault(l.person_key, l.sender)
        if focus:
            involved[focus] = max(involved.get(focus, 0.0), 2.5)
        if scope is not None and scope.kind == "person":
            involved.setdefault(scope.space, 2.5)
        if not involved:
            if shared and scope is not None and scope.people:
                return prompts.PRESENT_MORE.format(n=len(scope.people))
            return prompts.PRESENT_NONE
        close = {pk: await self.closeness(pk) for pk in involved}
        ranked = sorted(involved, key=lambda pk: -(involved[pk] + 0.5 * close[pk]))[:max(1, int(max_people))]
        lines = []
        for pk in ranked:
            name = name_of.get(pk) or await k.social.person_name(pk)
            facts = []
            if pk == focus:
                facts.append("我此刻主要在回应 TA")
            elif involved[pk] >= 2.0:
                facts.append("刚刚在说话")
            else:
                facts.append("刚才也在")
            facts.append(register_hint(close[pk]).split("：", 1)[0])
            if pk != focus:          # the focus person gets the full relationship block below
                story = await self.storylines(pk, scope, withheld, origins)
                if story:
                    facts.append("TA 最近：" + "；".join(s[:40] for s in story[:2]))
            lines.append(prompts.PRESENT_LINE.format(name=name, facts="；".join(facts)))
        if shared:
            others = len([p for p in (scope.people if scope is not None else ()) if p not in ranked])
            if others > 0:
                lines.append(prompts.PRESENT_MORE.format(n=others))
        elif len(ranked) == 1:
            return prompts.PRESENT_ONE_TO_ONE.format(name=name_of.get(ranked[0]) or await k.social.person_name(ranked[0]))
        return "\n".join(lines)

    def speech_context(self, person_key: Optional[str], task: ReplyTask) -> dict:
        """Inputs for the speech self-monitor (docs/18 §4.3)."""
        partner_len = None
        if person_key and person_key in self._baselines:
            b = self._baselines[person_key][0]
            if b.n >= 5:
                partner_len = b.len_median
        if partner_len is None and task.new_lines:
            theirs = [len(l.text) for l in task.new_lines if not l.mine]
            partner_len = (sum(theirs) / len(theirs)) if theirs else None
            if partner_len is not None:
                partner_len = max(partner_len, 8.0)
        recent_mine = [l.text for l in task.history if l.mine][-8:]
        return {"partner_len": partner_len, "recent_mine": recent_mine}

    # ------------------------------------------------------------------ level 2
    async def deep_read(self, task: ReplyTask, cues: Cues, blocks: dict) -> dict:
        k = self.k
        self.stats["deep"] += 1
        name = k.selfm.name()
        chat = self.m.ctx.render_chat(task.history, task.new_lines, name, max_chars=3500)
        d = await k.llm.json("core.deep_read", prompts.DEEP_READ.format(
            state=blocks.get("state", ""), present=blocks.get("present") or prompts.PRESENT_NONE,
            focus=blocks.get("focus") or prompts.FOCUS_NONE, relationship=blocks.get("relationship", ""),
            memories=blocks.get("memories", ""), cues=cues.text, reasons="、".join(cues.reasons), chat=chat),
            tier="deliberate", system=self.m.ctx.system_prompt(contract=""), identity_critical=True,
            thread_id=task.thread_id, schema={"hypotheses": [], "perspective": "", "feeling": "", "need": "",
                                               "not_need": "", "approach": "", "pitfalls": []})
        return d

    @staticmethod
    def render_deep(d: dict) -> str:
        hs = []
        for h in (d.get("hypotheses") or [])[:3]:
            if isinstance(h, dict):
                hs.append(f"{h.get('h','')}（可能性约 {float(h.get('p') or 0):.0%}；依据：{h.get('evidence','')}）")
        lines = []
        if hs:
            lines.append("几种可能的理解：" + "；".join(hs))
        if d.get("perspective"):
            lines.append("换位想想：" + d["perspective"])
        if d.get("need"):
            lines.append(f"他此刻需要：{d['need']}；不需要：{d.get('not_need','')}")
        if d.get("approach"):
            lines.append("我打算：" + d["approach"])
        if d.get("pitfalls"):
            lines.append("要避免：" + "；".join(str(x) for x in d["pitfalls"][:3]))
        return "\n".join(lines)

    # ------------------------------------------------------------------ empathy (docs/17 §6)
    async def rekey_entity(self, old: str, new: str) -> None:
        """Entity merge: cached baselines / closeness / storylines / cross-space lines for either id are dropped
        and recomputed (storylines are cached per (person, space): drop every entry touching either id)."""
        for d in (self._baselines, self._lines):
            for key in (old, new):
                d.pop(key, None)
        for key in [x for x in self._story if isinstance(x, tuple) and (set(x) & {old, new})]:
            self._story.pop(key, None)
        self._closeness = (0.0, {})                 # closeness snapshot is recomputed on next use

    async def contagion(self, reading: dict, person_key: Optional[str], trust_low: bool) -> list:
        if not reading or not person_key:
            return []
        feeling = str(reading.get("feeling") or "")
        if not feeling and reading.get("feeling_valence") is None:
            return []
        close = 0.2 if trust_low else await self.closeness(person_key)
        a = n = 0.5
        try:
            bf = (self.k.affect.status() or {}).get("big_five") or {}
            a, n = float(bf.get("A", 0.5)), float(bf.get("N", 0.5))
        except Exception:
            pass
        gain = 0.6 + 0.4 * a + 0.2 * n
        base = float(reading.get("feeling_intensity") or 0.5)
        inten = min(0.35, base * close * gain)
        if inten < 0.05:
            return []
        neg = any(w in feeling for w in ("难过", "伤心", "低落", "失落", "委屈", "沮丧", "失望", "痛苦", "孤独", "寂寞", "累", "疲惫", "焦虑", "紧张",
                                     "害怕", "担心", "烦", "郁闷", "崩溃", "绝望", "不开心", "emo", "无助", "心酸", "难受", "压力"))
        pos = any(w in feeling for w in ("开心", "高兴", "兴奋", "激动", "满足", "骄傲", "期待", "轻松", "幸福", "快乐", "得意", "惊喜"))
        angry = any(w in feeling for w in ("生气", "愤怒", "火大", "恼火"))
        name = await self.k.social.person_name(person_key)
        val = reading.get("feeling_valence")
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None
        if val is not None:            # the reading's own valence is the primary signal; lexicon is a fallback
            neg, pos = val <= -0.2, val >= 0.2
            angry = angry and neg
        if angry:
            t = "fear"
        elif neg:
            t = "pity"
        elif pos:
            t = "happy_for"
        else:
            return []
        return [{"type": t, "intensity": round(inten, 3), "target": person_key, "target_name": name,
                 "cause": f"共情：{name}{feeling}"}]

    # ------------------------------------------------------------------ learning (docs/17 §9)
    async def record(self, person_key: Optional[str], thread_id: str, event_id: str, reading: dict, deep: bool) -> None:
        if not person_key or not reading:
            return
        hyps = reading.get("hypotheses") or []
        top = ""
        if hyps and isinstance(hyps[0], dict):
            top = str(max(hyps, key=lambda h: float(h.get("p") or 0)).get("h", ""))
        await self.k.db.execute("INSERT INTO core_readings (id,ts,person_key,thread_id,event_id,feeling,need,hypothesis,deep)"
                                " VALUES (?,?,?,?,?,?,?,?,?)",
                                (new_id("rd"), self.k.clock.now(), person_key, thread_id, event_id,
                                 str(reading.get("feeling", ""))[:200], str(reading.get("need", ""))[:50], top[:300],
                                 int(deep)))

    async def review(self, since: float, until: float, limit: int = 10) -> dict:
        """Check past readings against what happened afterwards; feed growth evidence + opinions."""
        k = self.k
        rows = await k.db.fetchall("SELECT * FROM core_readings WHERE ts>=? AND ts<? AND reviewed=0 "
                                   "ORDER BY deep DESC, ts DESC LIMIT ?", (since, until, limit))
        rep = {"reviewed": 0, "confirmed": 0, "refuted": 0}
        for r in rows:
            # evidence = what this PERSON said afterwards, in any space (docs/22), not the next lines of one chat
            after = await k.bus.query(types=["percept.message"], since=r["ts"], limit=300)
            theirs = [e for e in after if e.payload.get("person_key") == r["person_key"]][:12]
            later = [e.payload.get("text", "") for e in theirs]
            # the lesson is drawn from what they said where: it keeps those spaces as its origins (INV-34)
            spaces = list(dict.fromkeys([r["thread_id"]] + [e.thread_id for e in theirs[:10] if e.thread_id]))
            if not later:
                await k.db.execute("UPDATE core_readings SET reviewed=1, verdict='no_evidence' WHERE id=?", (r["id"],))
                continue
            name = await k.social.person_name(r["person_key"])
            d = await k.llm.json("core.review_reading", prompts.REVIEW_READING.format(
                name=name, feeling=r["feeling"], need=r["need"], hypothesis=r["hypothesis"],
                later="\n".join(f"- {t}" for t in later[:10])), tier="fast",
                schema={"verdict": "unclear", "lesson": ""})
            verdict = d.get("verdict", "unclear")
            await k.db.execute("UPDATE core_readings SET reviewed=1, verdict=? WHERE id=?", (verdict, r["id"]))
            rep["reviewed"] += 1
            if verdict in ("confirmed", "refuted"):
                rep[verdict] += 1
                try:
                    await k.growth.record_evidence(level="S3", polarity="helpful" if verdict == "confirmed" else "harmful",
                                                   about=f"interpretation:{r['person_key']}", subject=r["person_key"],
                                                   context_refs=[r["event_id"]], confidence=0.6,
                                                   note=(d.get("lesson") or "")[:300])
                except Exception:
                    pass
                if verdict == "refuted" and d.get("lesson"):
                    extra = {"meta": {"origins": [s for s in spaces if s]}} if len(spaces) > 1 else {}
                    await k.memory.note(kind="opinion", content=f"关于{name}：{d['lesson']}", source="reflected",
                                        importance=5, evidence=[r["event_id"]], person_keys=[r["person_key"]],
                                        thread_id=r["thread_id"], **extra)
        return rep
