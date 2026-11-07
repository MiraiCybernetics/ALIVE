"""Small, dependency-free text heuristics used by the interaction layer.

Everything here is cheap (regex / set arithmetic) because it runs on every inbound message.
Chinese-first, with English fallbacks.
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence, Tuple

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\U0001F3FB-\U0001F3FF"
    "\uFE0E\uFE0F\u200D\u20E3\u3030\u303D\u3297\u3299]")
_PLACEHOLDER = re.compile(r"\[(?:图片|表情|动画表情|表情包|贴纸|语音|视频|文件|sticker|image|photo|gif|emoji|face)[^\]]{0,12}\]",
                          re.I)
_PUNCT_SPACE = re.compile(r"[\s\W_]+", re.U)
_KAOMOJI_CHARS = set("()（）^_^~～・ω°´`ﾟ∀≧≦▽ﾉ╯╰︶ ･ᐛ•ᴗ")

_QUESTION_WORDS = re.compile(
    r"(为什么|为啥|怎么|怎样|如何|什么|啥|哪|谁|几[点个时天号岁]|多少|是不是|能不能|可不可以|有没有|会不会|要不要|对不对|"
    r"好不好|行不行|吗|么$|呢$|why|how|what|when|where|who|which|could you|can you|do you|are you|is it)",
    re.I)

_CORRECTION = re.compile(
    r"^(\*|＊)|不对|不是不是|说错|打错|写错|更正|纠正|我是说|我的意思是|应该是|我刚才说的|搞错了|"
    r"\bactually\b|\bi meant\b|\bcorrection\b|\btypo\b|\bsorry,? i meant\b", re.I)
_CANCEL = re.compile(
    r"算了|不用了|别回|先别|取消|当我没说|没事了|撤回|收回|等等|等一下|等下|不要回|"
    r"\bnever ?mind\b|\bnvm\b|\bcancel\b|\bwait\b|\bforget it\b|\bignore that\b", re.I)
_TOPIC_SHIFT = re.compile(r"^(对了|话说|另外|换个话题|顺便|说起来|突然想到|btw\b|by the way|anyway|oh and)", re.I)
_FAREWELL = re.compile(r"(拜拜|再见|晚安|睡了|先走了|下了|回头聊|改天聊|byebye|\bbye\b|good ?night|\bgtg\b|\bttyl\b)", re.I)
_ACK = {"嗯", "嗯嗯", "哦", "噢", "喔", "好", "好的", "好滴", "行", "ok", "okk", "okay", "k", "收到", "了解", "明白",
        "哈", "哈哈", "哈哈哈", "嘿嘿", "hh", "hhh", "233", "666", "👍", "🙏", "对", "对对", "是", "是的", "yes", "yep",
        "lol", "草", "嗯呢", "好嘞", "知道了"}


def cjk_count(s: str) -> int:
    return len(_CJK.findall(s or ""))


def latin_words(s: str) -> int:
    return len(_LATIN_WORD.findall(_CJK.sub(" ", s or "")))


def has_question(s: str) -> bool:
    if not s:
        return False
    if "?" in s or "？" in s:
        return True
    t = s.strip().rstrip("~～。.!！ ")
    return bool(_QUESTION_WORDS.search(t[-12:] if len(t) > 12 else t)) or bool(
        re.search(r"(吗|么|呢|嘛)$", t))


def is_emoji_only(s: str) -> bool:
    """True if the text carries no words: only emoji, media placeholders, punctuation or kaomoji."""
    if s is None:
        return True
    t = _PLACEHOLDER.sub("", s)
    t = _EMOJI.sub("", t)
    t = "".join(ch for ch in t if ch not in _KAOMOJI_CHARS)
    t = _PUNCT_SPACE.sub("", t)
    return not t


def is_trivial_ack(s: str) -> bool:
    t = (s or "").strip().lower().rstrip("~～。.!！ ")
    return t in _ACK or (len(t) <= 4 and is_emoji_only(t)) or not t


def is_correction(s: str) -> bool:
    return bool(_CORRECTION.search((s or "").strip()))


def is_cancel(s: str) -> bool:
    return bool(_CANCEL.search((s or "").strip()))


def is_topic_shift_marker(s: str) -> bool:
    return bool(_TOPIC_SHIFT.search((s or "").strip()))


def is_farewell(s: str) -> bool:
    return bool(_FAREWELL.search(s or ""))


def tokens(s: str) -> set:
    """Character bigrams for CJK runs + lowercase latin words (>=2 chars). Used for cheap similarity."""
    s = s or ""
    out = set()
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", s):
        if len(run) == 1:
            out.add(run)
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    for w in _LATIN_WORD.findall(s.lower()):
        if len(w) >= 2:
            out.add(w)
    return out


def similarity(a: str, b: str) -> float:
    """Overlap coefficient |A∩B| / min(|A|,|B|) on bigram/word tokens. 0..1."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(min(len(ta), len(tb)))


def interest_similarity(text: str, interests: Iterable[set]) -> float:
    """Max over interest token sets of |T∩I| / |T|^0.5 / |I|^0.5-ish, clipped to 0..1.

    Interests are long descriptions; a message hitting 2-3 of their bigrams is already a strong signal,
    so we use a soft cosine (count / sqrt(|T|·|I|)) scaled up and clipped.
    """
    t = tokens(text)
    if not t:
        return 0.0
    best = 0.0
    for it in interests:
        if not it:
            continue
        inter = len(t & it)
        if not inter:
            continue
        score = inter / (len(t) * len(it)) ** 0.5
        best = max(best, min(1.0, score * 2.5))
    return best


def name_mention(text: str, names: Sequence[str]) -> Tuple[bool, bool]:
    """(mentioned, vocative). vocative = addressed by name: '@名字', '名字，…', '名字你…', '…吧名字'."""
    if not text:
        return False, False
    t = text.strip()
    tl = t.lower()
    mentioned = False
    vocative = False
    for n in names:
        if not n or len(n) < 2 and not _CJK.match(n or ""):
            continue
        nl = n.lower()
        if nl not in tl:
            continue
        mentioned = True
        if "@" + nl in tl:
            vocative = True
            break
        if tl.startswith(nl):
            rest = tl[len(nl):len(nl) + 1]
            if not rest or rest in "，,:：!！?？ 、~～啊呀呢你在快帮来哦吖。" or _EMOJI.match(rest):
                vocative = True
                break
        stripped = tl.rstrip("~～。.!！?？ ")
        if stripped.endswith(nl) and len(stripped) > len(nl):
            prev = stripped[-len(nl) - 1]
            if prev in "，, 吧呢啊呀吗么哦嘛" or not _CJK.match(prev):
                vocative = True
                break
    return mentioned, vocative


def snippet(s: str, n: int = 24) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def media_placeholder(media: Sequence[dict]) -> str:
    names = {"image": "图片", "photo": "图片", "sticker": "表情包", "face": "表情", "gif": "动图", "animation": "动图",
             "voice": "语音", "audio": "语音", "video": "视频", "file": "文件", "forward": "转发消息", "card": "卡片"}
    parts = []
    for m in media or []:
        parts.append(f"[{names.get(str(m.get('type', '')).lower(), '附件')}]")
    return "".join(parts)
