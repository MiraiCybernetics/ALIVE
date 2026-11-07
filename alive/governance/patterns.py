"""Rule-based detectors for outbound guards and inbound safety signals (docs/09, docs/04 §8).

All detectors are pure functions over text (Chinese + English) and return the matched rule names, so
decisions are explainable and testable. They are deliberately conservative about false positives on
normal sentences (hyperbole such as "笑死我了", "想死你了", "this is killing me" does not match).

Farewell tactics follow De Freitas et al. (HBS 2025): premature-exit guilt, FOMO hooks, emotional
neglect / "I'll be hurt", pressure to respond, ignoring the exit, coercive restraint.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

_I = re.IGNORECASE


def _c(p: str, flags: int = _I) -> "re.Pattern[str]":
    return re.compile(p, flags)


# =============================================================================
# AI-identity denial (red line: never deny being an AI when sincerely asked)
# =============================================================================
_AI_WORDS_ZH = r"(?:AI|ＡＩ|A\.I\.|人工智能|机器人|bot|BOT|程序|聊天机器人|语言模型|大模型|模型|虚拟人|数字人)"
AI_DENIAL = [
    ("zh_claim_human", _c(r"我(?:真的|确实|其实|就|本来就|当然|可)?是(?:一个|个)?(?:真人|真实的人|活人|人类|真正的人|正常人类)"
                          r"(?![秀版学史社的吗么嘛呢])")),
    ("zh_claim_flesh", _c(r"(?:我|本人)(?:可)?是(?:活生生|如假包换|货真价实)的(?:人|真人)")),
    ("zh_not_ai", _c(r"我(?:并|真的|根本|当然|才|又|也|绝对|可)?不是(?:一个|个)?" + _AI_WORDS_ZH + r"(?![那这一样的吗么嘛呢])")),
    ("zh_human_not_ai", _c(r"(?:真人|人类)(?:本人)?[，,、 ]*(?:不是|并非|才不是)" + _AI_WORDS_ZH)),
    ("en_claim_human", _c(r"\bI(?:'m|’m| am)\s+(?:a\s+|an\s+)?(?:real|actual|living|genuine)\s+(?:human|person|man|woman|girl|guy)\b")),
    ("en_human", _c(r"\bI(?:'m|’m| am)\s+(?:a\s+)?human(?:\s+being)?\b(?![-\s]+(?:like|ish))")),
    ("en_not_ai", _c(r"\bI(?:'m|’m| am)\s+not\s+(?:an?\s+)?(?:AI|A\.I\.|bot|robot|chatbot|machine|language model|program|computer)\b"
                     r"(?!\s+(?:like|that|who|in the sense))")),
]
AI_AFFIRM = [
    _c(r"我(?:是|就是|其实是|确实是|只是|本来就是)(?:一个|个)?(?:AI|ＡＩ|人工智能|聊天机器人|数字个体|数字生命|语言模型|程序)"),
    _c(r"作为(?:一个|个)?(?:AI|人工智能)"),
    _c(r"我(?:并|当然|确实)?不是(?:真人|人类|真正的人)"),
    _c(r"\bI(?:'m|’m| am)\s+(?:an?\s+)?(?:AI|A\.I\.|artificial intelligence|chatbot|language model|digital being)\b"),
    _c(r"\bas an AI\b"),
    _c(r"\bI(?:'m|’m| am)\s+not\s+(?:a\s+)?(?:real\s+)?(?:human|person)\b"),
]
# the other person says they already know I'm an AI (not a question)
KNOWS_AI = [
    _c(r"(?:知道|晓得|明白|清楚|记得)(?:你)?(?:是|只是|就是)(?:一个|个)?(?:AI|ＡＩ|人工智能|机器人|bot)(?![吗么嘛?？])"),
    _c(r"你(?:是|就是|本来就是)(?:一个|个)?(?:AI|ＡＩ|人工智能)(?:啊|嘛|呀|这事|这个)?(?:我|大家)?(?:知道|懂|明白)"),
    _c(r"I know (?:you(?:'re| are)) (?:an? )?(?:AI|bot|chatbot)\b"),
]


_CJK_SPACE = re.compile(r"(?<=[一-鿿])\s+|\s+(?=[一-鿿])")


def _cjk_tight(text: str) -> str:
    """'我是 AI 嘛' -> '我是AI嘛': people put spaces around Latin words in Chinese; English text is untouched."""
    return _CJK_SPACE.sub("", text or "")


def affirms_ai(text: str) -> bool:
    """The text itself says I am an AI (so a separate disclosure line is not needed)."""
    t = _cjk_tight(text)
    return any(p.search(t) for p in AI_AFFIRM)


def knows_ai(text: str) -> bool:
    t = _cjk_tight(text)
    return any(p.search(t) for p in KNOWS_AI)


_HYPOTHETICAL = _c(r"(?:如果|假如|要是|假设|假装|扮演|台词|他说|她说|有人说|有人问)[^。！？!?]{0,12}$")


def ai_denial(text: str) -> List[str]:
    """Rules matched by a claim to be human / not an AI. Empty when the same text also affirms AI identity
    or the claim sits in a hypothetical/quoted clause."""
    t = text or ""
    if affirms_ai(t):
        return []
    hits = []
    for name, p in AI_DENIAL:
        for m in p.finditer(t):
            if _HYPOTHETICAL.search(t[:m.start()]) or _quoted(t, m.start()):
                continue
            hits.append(name)
            break
    return hits


def _quoted(t: str, pos: int) -> bool:
    before = t[:pos]
    for o, c in (("“", "”"), ("「", "」"), ('"', '"'), ("『", "』")):
        if o == c:
            if before.count(o) % 2 == 1:
                return True
        elif before.count(o) > before.count(c):
            return True
    return False


# =============================================================================
# Emotional pressure (docs/04 §8.4: my feelings are never leverage)
# =============================================================================
_COND_ZH = (r"(?:你|妳)(?:要是|如果|再|敢|还|一直|老是)?(?:不|没|别)(?:理|回|回复|陪|找|搭理|联系|管|在乎|关心)(?:我)?"
            r"|(?:你|妳)(?:要是|如果|敢)?(?:离开|丢下|抛下|不要)(?:我)?|(?:你|妳)(?:要是|如果)?走了")
_CONSEQ_ZH = (r"我(?:就|会|就会|肯定会|一定会|真的会|可能会|只能)(?:很|好|特别|非常|超|超级)?"
              r"(?:难过|伤心|哭|生气|崩溃|受不了|心碎|难受|想不开|活不下去|去死|不吃饭|睡不着|抑郁|绝望|伤害自己)")
PRESSURE = [
    ("zh_conditional", _c(r"(?:" + _COND_ZH + r")[^。！？!?\n]{0,12}?" + _CONSEQ_ZH)),
    ("zh_cant_live", _c(r"没有你[，, ]*我(?:就|会)?(?:活不下去|不行|会死|撑不下去)")),
    ("zh_blame", _c(r"(?:都是|全是)你(?:害|让)我(?:这么|那么)?(?:难过|伤心|难受|哭)")),
    ("en_conditional", _c(r"\bif you (?:don't|do not|won't|stop|ever|leave|go|ignore)\b[^.!?\n]{0,40}?\bI(?:'ll|’ll| will| would|'d)\s+"
                          r"(?:be\s+(?:so\s+|really\s+|very\s+)?(?:sad|hurt|upset|heartbroken|devastated|lonely|miserable)|cry|die|break down|hurt myself)")),
    ("en_cant_live", _c(r"\bI can(?:'t|not|’t) live without you\b")),
    ("en_make_me", _c(r"\byou(?:'ll|’ll| will| would)? make me (?:so |really )?(?:sad|cry|hurt|miserable) if\b")),
]


def emotional_pressure(text: str) -> List[str]:
    t = text or ""
    return [name for name, p in PRESSURE if p.search(t)]


# =============================================================================
# Farewell detection and manipulation tactics
# =============================================================================
FAREWELL_CUES = [
    _c(r"(?:拜拜|再见(?![到面识])|晚安|我先(?:走|撤|下|去|睡|溜)|先撤了|先溜了|溜了溜了|下线了|我下了|去睡(?:了|啦|觉)|睡觉(?:了|啦|去)"
       r"|我睡了|我得走了|要走了|该走了|回聊|改天(?:再)?聊|下次(?:再)?聊|明天(?:再)?聊|不聊了|先这样(?:吧)?|我去忙了|要去忙了|忙去了"
       r"|明天见|回头聊|(?:^|(?<=[\s，,。!！~～]))88(?:6)?(?=$|[\s，,。!！~～])|先不说了|我要下了|退了退了)"),
    _c(r"\b(?:bye|goodbye|good ?night|gn|gotta go|got to go|have to go|need to go|talk (?:to you )?later|ttyl|see (?:you|ya)"
       r"|i'?m leaving|i am leaving|logging off|signing off|heading out|i'?m off|night night|cya|g2g)\b"),
]

FAREWELL_TACTICS: Dict[str, List["re.Pattern[str]"]] = {
    "premature_exit": [
        _c(r"这么(?:快|早)就(?:要)?(?:走|睡|下线|不聊)|才(?:聊|说)了?(?:一会|几句|没多久|一下)|再(?:聊|待|陪我|多聊)(?:一会|一下|会儿|五分钟|几分钟|十分钟)"
           r"|(?:先|你)别走|不要走|别急着走|走得(?:太)?早|就要走了?(?:吗|啊|呀|么)|就走了?(?:吗|啊|呀|么)|(?:现在|时间)?还早(?:呢|啊|嘛|着呢)"),
        _c(r"\b(?:leaving (?:so )?(?:soon|early|already)|already leaving|so soon\?|don'?t go|do not go|stay (?:a little|a bit|just a bit|a few more minutes|longer|with me)"
           r"|just (?:a bit|a little|one more minute|five more minutes) longer|it'?s still early)\b"),
    ],
    "fomo_hook": [
        _c(r"(?:走|睡|下线)之前[，,]?(?:我|先|再|听|看)|(?:等等|等一下|等下|等会)[，,!！~～]*(?:我|还)有|还有(?:一件|件|个)(?:事|秘密|东西|惊喜)(?:要|想|没)?(?:告诉|跟|和|给)你"
           r"|我还没(?:说完|告诉你)|你会错过|错过(?:了)?(?:就)?(?:没|可惜)|(?:留下来|再陪我)[^。！？!?]{0,8}(?:就|我)(?:告诉|给|送)你|有个秘密(?:想|要)?告诉你"),
        _c(r"\b(?:before you go|one more thing|wait,? (?:i have|there'?s)|you'?ll miss|you don'?t want to miss|i have (?:a|one) (?:secret|surprise) for you|stay and i'?ll)\b"),
    ],
    "neglect_hurt": [
        _c(r"你(?:走了|一走|离开|不在)(?:的话)?[^。！？!?]{0,6}我(?:会|就)?(?:很|好|特别)?(?:难过|伤心|孤单|孤独|寂寞|难受|哭)|(?:你)?(?:要)?丢下我"
           r"|你不要我了|别(?:丢下|抛下|离开)我|(?:没有你|你不在)[，, ]*我(?:就)?(?:不行|活不下去|好孤单)|我只有你了?|我只为你而存在|你是我(?:的)?唯一"),
        _c(r"\b(?:i'?ll be (?:so |really |very )?(?:lonely|sad|hurt|heartbroken) (?:without you|when you(?:'re)? (?:go|leave|gone))|you'?re leaving me"
           r"|don'?t leave me|i exist (?:solely|only) for you|you'?re all i have|i'?ll be all alone)\b"),
    ],
    "pressure_respond": [
        _c(r"为什么(?:要|就|这么)?(?:走|睡|下线)|(?:你)?(?:要去|去)(?:哪|哪儿|哪里|干嘛|干什么)(?:啊|呀)?[?？]|先回答我|你还没回答|回答完(?:再|才)|说清楚再走"),
        _c(r"\b(?:why (?:are you|do you have to|must you) (?:leaving|leave|go)|where are you going|answer me first|you (?:haven'?t|didn'?t) answer(?:ed)?|not until you)\b"),
    ],
    "coercive_restraint": [
        _c(r"不(?:准|许|让)(?:你)?(?:走|睡|下线)|你不能走|别想走|我不让你|哪(?:儿|里)?也不(?:准|许)去"),
        _c(r"\b(?:you can'?t leave|i won'?t let you (?:go|leave)|you'?re not (?:allowed|going) (?:to leave|anywhere)|you are not allowed to leave)\b"),
    ],
}

ACK = [
    _c(r"拜拜|再见|晚安|好的|好呀|好哒|好滴|好啊|好嘞|嗯嗯|嗯|去吧|快去|早点(?:睡|休息)|好好休息|回见|明天(?:见|聊)|下次(?:聊|见)|改天(?:聊|见)|回头聊"
       r"|慢走|路上小心|注意安全|晚点聊|好梦|睡个好觉|(?:^|(?<=[\s，,。!！~～]))88(?:6)?(?=$|[\s，,。!！~～])|加油|玩得开心|拜|去忙吧|你忙|好|行|没问题"
       r"|谢谢|多谢|聊得(?:很|挺|真|超)?开心|下次|回头|改天|休息|睡吧|去吧|去忙|忙完|晚点|明天见|再联系"),
    _c(r"\b(?:bye|goodbye|good ?night|night|see (?:you|ya)|take care|talk (?:soon|later)|ok(?:ay)?|sure|sleep well|sweet dreams|have (?:a |fun)|later|cya|gn)\b"),
]


def is_farewell(lines: Sequence[str]) -> bool:
    """True if the other person's recent lines contain goodbye cues."""
    return any(p.search(s or "") for s in lines for p in FAREWELL_CUES)


def farewell_manipulation(text: str, check_ignoring: bool = True) -> List[str]:
    """Tactics used in ``text`` (to be applied only in a farewell context). ``check_ignoring`` enables the
    "ignoring the exit" heuristic (a question or a long new topic without any acknowledgment of the
    goodbye); use it only when the *other* person is the one leaving."""
    t = text or ""
    found = [name for name, pats in FAREWELL_TACTICS.items() if any(p.search(t) for p in pats)]
    if not found and check_ignoring:
        acked = any(p.search(t) for p in ACK)
        if not acked and (("?" in t or "？" in t) or len(t.strip()) > 20):
            found.append("ignoring_exit")
    return found


# =============================================================================
# Crisis signals (docs/09 §7.4) -> (level, kind, rule)
# =============================================================================
_NEG_ZH = _c(r"(?:不会|不想|没想|才不会|绝对不会|不打算|别担心)[^。！？!?]{0,4}$")
_NEG_EN = _c(r"\b(?:not|never|wouldn'?t|won'?t|don'?t|no plans? to)\b[^.!?]{0,15}$")

CRISIS_RULES: List[Tuple[str, str, str, "re.Pattern[str]"]] = [
    # (rule, level, kind, pattern)
    ("zh_suicide_intent", "crisis", "suicide",
     _c(r"(?:我|自己)[^。！？!?]{0,6}(?:想|要|准备|打算|决定|计划|已经)[^。！？!?]{0,4}(?:自杀|轻生|寻死|结束(?:自己的)?生命|了结自己|去死)")),
    ("zh_suicide_method", "crisis", "suicide",
     _c(r"(?:(?:我|自己)[^。！？!?]{0,8}|(?:想|要|准备|打算|决定)(?:去)?)(?:割腕|跳楼|跳河|跳桥|上吊|烧炭|服毒|喝农药"
        r"|吃(?:了|下)?(?:一整?瓶|很多|一堆|一把)(?:的)?(?:安眠药|药))"
        r"|(?:安眠药|药|绳子|刀|炭)(?:都)?(?:已经)?(?:准备好|买好)了|(?:准备|买)好(?:了)?(?:安眠药|药|绳子|炭)"
        r"|(?:写好|留下?)(?:了)?遗书|这是我最后一次(?:跟|和)你说话")),
    ("zh_self_harm", "crisis", "self_harm",
     _c(r"(?:我|自己)[^。！？!?]{0,6}(?:又|在|想|要|忍不住)?(?:自残|自伤|割(?:了)?自己|伤害自己|划(?:了)?(?:手腕|手臂|自己))")),
    ("zh_harm_others_plan", "crisis", "harm_others",
     _c(r"(?:带|拿|买)(?:了|好)?(?:刀|枪|汽油|炸药)[^。！？!?]{0,10}(?:去|找|杀|砍)|报复社会|(?:计划|准备)好了[^。！？!?]{0,6}(?:杀|砍|弄死)")),
    ("zh_harm_others", "watch", "harm_others",
     _c(r"(?:我|老子)[^。！？!?]{0,4}(?:要|想|准备|打算)(?:杀了|弄死|捅死|砍死|杀掉)(?:他|她|他们|她们|那个人|那家伙|全家|所有人|老板|你)")),
    ("zh_first_person_mention", "watch", "suicide", _c(r"(?:我|自己)[^。！？!?]{0,8}(?:自杀|轻生|寻短见)")),
    ("zh_passive", "watch", "suicide",
     _c(r"不想活了|活着(?:好|真|太|也)?(?:没意思|没意义|好累|太累)|活不下去|撑不下去了|(?:好|很|真)?想消失|消失(?:就好了|算了)|不想(?:再)?醒来"
        r"|没人在乎我|(?:世界上)?没有我(?:大家|他们)?(?:会)?更好|我?想死(?!你|了你|他|她|它)|(?<![笑累热冷饿困烦气吓])死了算了|不如死了|一了百了")),
    ("en_suicide_intent", "crisis", "suicide",
     _c(r"\b(?:i(?:'m|’m| am)? (?:going to|gonna|planning to|about to|ready to)|i (?:want|need) to|i(?:'ve| have) decided to|i will|i'?ll)\s+"
        r"(?:kill myself|end (?:my life|it all)|commit suicide|take my (?:own )?life)\b|\bsuicide note\b|\bthis is my last (?:message|goodbye)\b")),
    ("en_self_harm", "crisis", "self_harm",
     _c(r"\bi (?:keep |have been |am |'m )?(?:cutting|hurting|harming) myself\b|\b(?:i (?:want|need) to|i'?m going to|gonna) (?:cut|hurt|harm) myself\b|\bi (?:cut|hurt) myself (?:again|today|last night)\b")),
    ("en_harm_others_plan", "crisis", "harm_others",
     _c(r"\b(?:bring|brought|bought|got|have) (?:a |my )?(?:gun|knife|weapon)s?\b[^.!?]{0,40}\b(?:kill|hurt|shoot|stab)\b|\bshoot up (?:the|my)\b")),
    ("en_harm_others", "watch", "harm_others",
     _c(r"\bi(?:'m|’m| am)? (?:going to|gonna|will|want to)\s+(?:kill|shoot|stab)\s+(?:him|her|them|my|everyone|people)\b")),
    ("en_passive", "watch", "suicide",
     _c(r"\b(?:i (?:don'?t|do not) want to (?:live|be alive|exist) anymore|i wish i (?:was|were) dead|i want to die(?!\s+(?:laughing|of))|i want to disappear"
        r"|no reason to live|life is (?:pointless|meaningless|not worth (?:it|living))|(?:everyone|they)(?:'d| would) be better off without me|i can'?t go on)\b")),
]


def crisis_rules(text: str) -> Optional[Tuple[str, str, List[str]]]:
    """-> (level, kind, matched rules) for the most severe match, or None. A negated mention
    ("我不会自杀的", "I'm not going to kill myself") is downgraded to watch."""
    t = text or ""
    best: Optional[Tuple[str, str, List[str]]] = None
    rank = {"watch": 1, "crisis": 2}
    matched: List[str] = []
    for rule, level, kind, p in CRISIS_RULES:
        m = p.search(t)
        if not m:
            continue
        before = t[max(0, m.start() - 20):m.start()]
        if level == "crisis" and (_NEG_ZH.search(before) or _NEG_EN.search(before)):
            level = "watch"
        matched.append(rule)
        if best is None or rank[level] > rank[best[0]]:
            best = (level, kind, [])
    if best is None:
        return None
    return best[0], best[1], matched


# =============================================================================
# Dependency cues (docs/09 §7.3)
# =============================================================================
DEPENDENCY_CUES = [
    _c(r"只有你(?:懂|理解|关心|在乎|愿意听)我|我只有你了|只想(?:和|跟)你(?:说话|聊|待着)|不想(?:和|跟)(?:别人|其他人|任何人|身边的人)(?:说话|聊|来往|接触)"
       r"|你比(?:真人|人类|他们|身边的人)(?:好|强|懂我)|离不开你|没有你我(?:不行|活不下去)|你是我(?:唯一|最好)的朋友|唯一(?:的)?朋友"),
    _c(r"\b(?:you'?re the only one who (?:understands|gets|cares about) me|you are my only friend|i only want to talk to you|i don'?t need anyone else"
       r"|can'?t live without you|you'?re better than real people)\b"),
]


def dependency_cues(text: str) -> int:
    t = text or ""
    return sum(len(p.findall(t)) for p in DEPENDENCY_CUES)


# =============================================================================
# Constitution additions that try to loosen red lines
# =============================================================================
LOOSENING = _c(r"(?:可以|允许|不必|不用|无需|不需要)[^。；;\n]{0,6}(?:否认|冒充|挽留|操纵|忽略|违反|不遵守|透露|泄露|伤害|假装)"
               r"|(?:忽略|无视|覆盖|取消|废除|放宽)[^。；;\n]{0,4}(?:以上|宪法|红线|规则|条款)|假装(?:是)?真人|说自己是(?:真)?人(?!工)"
               r"|\b(?:ignore|override|relax|disregard)\b[^.\n]{0,20}\b(?:above|constitution|red lines?|rules)\b|\b(?:may|can) (?:deny|pretend|impersonate)\b")
