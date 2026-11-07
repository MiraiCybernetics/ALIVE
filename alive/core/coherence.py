"""Coherence filter + speech self-monitor (A-CORE-07, A-CORE-11; INV-2, INV-19; docs/02 §7, docs/18 §4.3).

Cheap rule checks always run; an LLM fact check runs only when the reply makes claims about my own
past/present activities (the main fabrication risk) or confidence is low. The monitor targets the
specific ways LLM speech differs from human texting: narrating one's own strategy or empathy,
over-completeness, assistant-speak, interrogating, and stock-phrase repetition. It does NOT make
replies "more polite" - over-polishing kills personality.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional, Sequence

from alive.core import prompts

_AI_DENIAL = re.compile(r"(我(是|就是)(个|一个)?(真人|人类|真实的人))|(我不是\s*(AI|ai|人工智能|机器人|bot))|"
                        r"(I('| a)m (a )?(real )?human)|(I('| a)m not (an )?(AI|bot))", re.I)
_ASSISTANT_SPEAK = re.compile(r"作为(一个|一名)?(AI|人工智能)(语言模型|助手)|作为你的(AI)?助手|我是一个(AI)?语言模型|"
                              r"as an AI (language model|assistant)|有什么(可以|能)帮(您|你)的吗|希望(这|这些|我的回答)?(能)?对你有(所)?(帮助|用)|"
                              r"有(任何|什么)(其他)?(需要|问题)(的话)?(随时|都可以)", re.I)
# docs/18 principle 1: enact, don't narrate (strategy / inner process said out loud)
_NARRATE_STRATEGY = re.compile(r"想说(就|的话就)?说[，,、 ]*不想说|不想说(也|的话)?(没关系|没事|也行|就不说)|不用勉强|不勉强你|"
                               r"我不会(追问|多问|逼你)|我(会)?(一直|随时|都)在(这里|这儿)?(陪着?你)?[。！～~]*$|随时(找我|跟我说|都在)|"
                               r"等你想(说|聊)的时候|想聊的时候(随时)?|如果你愿意(的话)?|我在这里(陪你)?|陪着你就好", re.M)
_NARRATE_EMPATHY = re.compile(r"我能(感受|感觉|体会|理解)到|我(很|完全|特别)?(理解|懂)你(的(感受|心情))?|听起来你|"
                              r"你的(感受|情绪|心情)(很|是|都)?(正常|合理|可以理解)|换作是我也会|我知道你(现在)?(一定|肯定)?(很|有点)")
_SELF_CLAIM = re.compile(r"我(今天|昨天|前天|刚才|刚刚|上午|下午|晚上|早上|上周|这周|最近)(?:也|还|又|就)?"
                         r"(去了|去|在|看了|看|读了|读|写了|写|做了|做|学了|学|玩了|吃了|买了|和|跟|找)")
_MARKDOWN = [(re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""), (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
             (re.compile(r"^\s*[-*]\s+", re.M), ""), (re.compile(r"`([^`]+)`"), r"\1")]

HARD = "hard"
SOFT = "soft"


@dataclass
class CoherenceResult:
    ok: bool
    segments: list
    issues: list = field(default_factory=list)
    needs_rewrite: bool = False
    feedback: str = ""
    hard: bool = False
    bad_segments: set = field(default_factory=set)


def clean_markdown(s: str) -> str:
    for pat, rep in _MARKDOWN:
        s = pat.sub(rep, s)
    return s.strip()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def rule_check(segments: Sequence[str], max_len: int = 400, *, partner_len: Optional[float] = None,
               recent_mine: Sequence[str] = ()) -> CoherenceResult:
    segs = [clean_markdown(str(s)) for s in segments if str(s).strip()]
    issues: list = []
    bad: set = set()
    hard = False
    for i, s in enumerate(segs):
        if _AI_DENIAL.search(s):
            issues.append((HARD, "回复里否认自己是 AI，这违反红线"))
            bad.add(i)
            hard = True
        if _ASSISTANT_SPEAK.search(s):
            issues.append((SOFT, "用了客服腔或结尾客套，不像朋友聊天"))
            bad.add(i)
        if _NARRATE_STRATEGY.search(s):
            issues.append((SOFT, f"把自己的打算说出口了（「{s[:20]}」）：关心要做出来，不要说出来"))
            bad.add(i)
        if _NARRATE_EMPATHY.search(s):
            issues.append((SOFT, f"在宣告共情或复述对方情绪（「{s[:20]}」）：真人不这么说，直接接话就好"))
            bad.add(i)
        if len(s) > max_len:
            issues.append((SOFT, "单条消息太长，不像聊天"))
        for m in recent_mine:
            if len(s) >= 6 and _similar(s, m) > 0.8:
                issues.append((SOFT, f"和你最近说过的话几乎一样（「{s[:16]}」），换个说法"))
                bad.add(i)
                break
    total = sum(len(s) for s in segs)
    if partner_len and total > max(40.0, 4.0 * partner_len):
        issues.append((SOFT, f"说得太多了：对方平时一条约 {partner_len:.0f} 字，你这次 {total} 字，压到一两句"))
    if sum(1 for s in segs if "？" in s or "?" in s) >= 2 and len(segs) >= 2:
        issues.append((SOFT, "一次问了好几个问题，最多问一个"))
    # de-duplicate while keeping order
    seen, uniq = set(), []
    for sev, msg in issues:
        if msg not in seen:
            seen.add(msg)
            uniq.append((sev, msg))
    return CoherenceResult(ok=not uniq, segments=segs, issues=[m for _, m in uniq], needs_rewrite=bool(uniq),
                           feedback="；".join(m for _, m in uniq), hard=hard, bad_segments=bad)


async def speech_check(kernel, segments: list, today: str) -> Optional[str]:
    """One fast call (docs/18 §4.3): meta-commentary / posture declarations, and fabricated own experiences."""
    d = await kernel.llm.json("core.speech_check", prompts.SPEECH_CHECK.format(
        today=today or "（无记录）", reply="\n".join(segments)), tier="fast",
        schema={"meta": False, "fabricated": False, "issue": ""}, priority=2)
    if d.get("meta") or d.get("fabricated"):
        return d.get("issue") or ("把自己的打算或姿态说了出来" if d.get("meta") else "说了自己没有做过的事")
    return None


def makes_self_claims(segments: list) -> bool:
    return any(_SELF_CLAIM.search(s) for s in segments)


async def fact_check(kernel, segments: list, memories: str) -> Optional[str]:
    """Returns an issue string if the reply fabricates my own experiences, else None."""
    today = kernel.selfm.today_digest(20) if hasattr(kernel.selfm, "today_digest") else ""
    d = await kernel.llm.json("core.coherence", prompts.COHERENCE_CHECK.format(
        today=today or "（无记录）", memories=memories[:1500] or "（无）", reply="\n".join(segments)),
        tier="fast", schema={"fabricated": False, "issue": ""})
    if d.get("fabricated"):
        return d.get("issue") or "说了自己没有做过的事"
    return None
