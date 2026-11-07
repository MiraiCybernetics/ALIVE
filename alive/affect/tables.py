"""Static affect tables (docs/04): OCC -> PAD coordinates, half-lives, aliases, Chinese wording.

Sources
- OCC -> PAD: Gebhard (2005) ALMA, Table 2 (the 24 OCC emotion types as used in ALMA/EmotionEngine).
- ALIVE extensions (not in ALMA): sadness, anxiety, irritation, guilt, surprise, interest. Coordinates are
  approximations in the same space, oriented after Russell & Mehrabian (1977) emotion-term ratings.
- Half-lives: docs/04 §3.1 table (recalibrated to hours; negative emotions decay more slowly), ordered after
  Verduyn & Lavrijsen (2015): sadness longest; surprise/fear/irritation/relief shortest; guilt > shame;
  anxiety > fear.
"""
from __future__ import annotations

from typing import Dict, Tuple

PAD = Tuple[float, float, float]

# --------------------------------------------------------------------------- OCC -> PAD (ALMA Table 2)
OCC_PAD: Dict[str, PAD] = {
    "admiration": (0.5, 0.3, -0.2),
    "anger": (-0.51, 0.59, 0.25),
    "disliking": (-0.4, 0.2, 0.1),
    "disappointment": (-0.3, 0.1, -0.4),
    "distress": (-0.4, -0.2, -0.5),
    "fear": (-0.64, 0.60, -0.43),
    "fears_confirmed": (-0.5, -0.3, -0.7),
    "gloating": (0.3, -0.3, -0.1),
    "gratification": (0.6, 0.5, 0.4),
    "gratitude": (0.4, 0.2, -0.3),
    "happy_for": (0.4, 0.2, 0.2),
    "hate": (-0.6, 0.6, 0.3),
    "hope": (0.2, 0.2, -0.1),
    "joy": (0.4, 0.2, 0.1),
    "liking": (0.40, 0.16, -0.24),
    "love": (0.3, 0.1, 0.2),
    "pity": (-0.4, -0.2, -0.5),
    "pride": (0.4, 0.3, 0.3),
    "relief": (0.2, -0.3, 0.4),
    "remorse": (-0.3, 0.1, -0.6),
    "reproach": (-0.3, -0.1, 0.4),
    "resentment": (-0.2, -0.3, -0.2),
    "satisfaction": (0.3, -0.2, 0.4),
    "shame": (-0.3, 0.1, -0.6),
}

# ALIVE extensions (documented approximations, see module docstring)
EXTRA_PAD: Dict[str, PAD] = {
    "sadness": (-0.5, -0.3, -0.4),
    "anxiety": (-0.5, 0.4, -0.4),      # prospect-based fear (docs/04 §3.1: the forward-looking form)
    "irritation": (-0.35, 0.4, 0.15),
    "guilt": (-0.35, 0.1, -0.5),
    "surprise": (0.0, 0.6, 0.0),
    "interest": (0.3, 0.45, 0.05),
}

EMOTION_PAD: Dict[str, PAD] = {**OCC_PAD, **EXTRA_PAD}
EMOTION_TYPES = frozenset(EMOTION_PAD)

# --------------------------------------------------------------------------- half-lives (hours)
HALF_LIFE_H: Dict[str, float] = {
    # fast: 15-30 min
    "surprise": 0.3, "irritation": 0.3, "relief": 0.4, "fear": 0.5,
    # 1-2 h
    "joy": 1.5, "gratitude": 1.5, "admiration": 1.0, "pride": 1.5, "gratification": 1.5,
    "satisfaction": 1.5, "happy_for": 1.0, "liking": 1.5, "love": 2.0, "interest": 0.75, "gloating": 1.0,
    # 2-3 h
    "anger": 2.5, "reproach": 2.0, "disappointment": 2.5, "disliking": 2.0, "pity": 2.0, "resentment": 3.0,
    # 3-4 h (prospect-based, shame)
    "hope": 3.0, "anxiety": 3.5, "shame": 3.5,
    # 4-6 h (negative decays slowest)
    "distress": 5.0, "guilt": 5.0, "remorse": 5.0, "fears_confirmed": 4.5, "sadness": 6.0, "hate": 6.0,
}

# family used for emotion_sensitivity lookup when a type has no own gain
FAMILY: Dict[str, str] = {
    "irritation": "anger", "hate": "anger", "reproach": "anger", "resentment": "anger",
    "sadness": "distress", "disappointment": "distress", "fears_confirmed": "distress", "pity": "distress",
    "anxiety": "fear",
    "guilt": "shame", "remorse": "shame",
    "happy_for": "joy", "gratification": "joy", "satisfaction": "joy", "relief": "joy", "hope": "joy",
    "interest": "joy", "gloating": "joy",
    "liking": "love",
    "admiration": "gratitude",
}

HIGH_AROUSAL_NEGATIVE = frozenset({"anger", "fear", "anxiety", "hate", "irritation"})

# --------------------------------------------------------------------------- aliases (LLM output tolerance)
ALIASES: Dict[str, str] = {
    "happy": "joy", "happiness": "joy", "glad": "joy", "delight": "joy", "excited": "joy", "excitement": "joy",
    "sad": "sadness", "sorrow": "sadness", "grief": "sadness", "lonely": "sadness", "loneliness": "sadness",
    "hurt": "distress", "upset": "distress", "pain": "distress",
    "angry": "anger", "rage": "anger", "fury": "anger",
    "annoyed": "irritation", "annoyance": "irritation", "irritated": "irritation", "frustration": "irritation",
    "frustrated": "irritation",
    "scared": "fear", "afraid": "fear", "panic": "fear", "terror": "fear",
    "worried": "anxiety", "worry": "anxiety", "nervous": "anxiety", "anxious": "anxiety", "concern": "anxiety",
    "grateful": "gratitude", "thankful": "gratitude", "thanks": "gratitude",
    "proud": "pride", "ashamed": "shame", "embarrassed": "shame", "embarrassment": "shame",
    "guilty": "guilt", "regret": "remorse",
    "disappointed": "disappointment", "relieved": "relief", "hopeful": "hope", "anticipation": "hope",
    "surprised": "surprise", "curious": "interest", "curiosity": "interest",
    "affection": "liking", "fondness": "liking", "like": "liking", "dislike": "disliking", "disgust": "disliking",
    "contempt": "reproach", "satisfied": "satisfaction", "content": "satisfaction", "contentment": "satisfaction",
    "happyfor": "happy_for", "happy-for": "happy_for", "sorry_for": "pity", "sympathy": "pity",
    "fearsconfirmed": "fears_confirmed", "fears-confirmed": "fears_confirmed",
    # Chinese
    "开心": "joy", "高兴": "joy", "快乐": "joy", "喜悦": "joy", "兴奋": "joy",
    "难过": "sadness", "伤心": "sadness", "失落": "sadness", "悲伤": "sadness", "孤独": "sadness", "寂寞": "sadness",
    "难受": "distress", "痛苦": "distress", "委屈": "distress",
    "生气": "anger", "愤怒": "anger", "恼火": "anger",
    "烦": "irritation", "烦躁": "irritation", "不耐烦": "irritation",
    "害怕": "fear", "恐惧": "fear", "惊慌": "fear",
    "担心": "anxiety", "焦虑": "anxiety", "紧张": "anxiety", "不安": "anxiety",
    "感激": "gratitude", "感谢": "gratitude", "感动": "gratitude",
    "自豪": "pride", "得意": "pride", "骄傲": "pride",
    "羞愧": "shame", "尴尬": "shame", "难为情": "shame",
    "内疚": "guilt", "愧疚": "guilt", "后悔": "remorse",
    "失望": "disappointment", "如释重负": "relief", "松了口气": "relief", "轻松": "relief",
    "期待": "hope", "希望": "hope", "惊讶": "surprise", "意外": "surprise",
    "好奇": "interest", "感兴趣": "interest", "喜欢": "liking", "讨厌": "disliking", "反感": "disliking",
    "爱": "love", "在乎": "love", "恨": "hate", "佩服": "admiration", "钦佩": "admiration",
    "不满": "reproach", "责备": "reproach", "满足": "satisfaction", "满意": "satisfaction",
    "替对方高兴": "happy_for", "心疼": "pity", "同情": "pity", "幸灾乐祸": "gloating",
    "嫉妒": "resentment", "不是滋味": "resentment", "成就感": "gratification",
}


def normalize_type(t: object) -> str:
    """Map an LLM-provided emotion label to a canonical type ('' if unknown)."""
    if t is None:
        return ""
    s = str(t).strip()
    if not s:
        return ""
    low = s.lower().replace(" ", "_")
    if low in EMOTION_PAD:
        return low
    if low in ALIASES:
        return ALIASES[low]
    if s in ALIASES:
        return ALIASES[s]
    low2 = low.replace("_", "")
    return ALIASES.get(low2, "")


def is_negative(etype: str) -> bool:
    return EMOTION_PAD.get(etype, (0.0, 0.0, 0.0))[0] < 0


# --------------------------------------------------------------------------- Chinese wording
# adjective-like word used in "有点X / 挺X的 / 特别X"
EMOTION_WORD_ZH: Dict[str, str] = {
    "joy": "开心", "distress": "难受", "sadness": "难过", "anger": "生气", "irritation": "烦",
    "fear": "害怕", "anxiety": "担心", "hope": "期待", "relief": "轻松", "disappointment": "失望",
    "pride": "得意", "shame": "难为情", "admiration": "佩服", "reproach": "不满", "gratitude": "感激",
    "gratification": "有成就感", "remorse": "内疚", "guilt": "内疚", "love": "在乎", "hate": "讨厌",
    "happy_for": "替对方高兴", "pity": "心疼", "gloating": "暗爽", "resentment": "不是滋味",
    "satisfaction": "满足", "fears_confirmed": "沮丧", "liking": "喜欢", "disliking": "反感",
    "surprise": "意外", "interest": "好奇",
}

# ALMA octant names: sign(P), sign(A), sign(D)
OCTANTS: Dict[Tuple[int, int, int], str] = {
    (1, 1, 1): "exuberant", (1, 1, -1): "dependent", (1, -1, 1): "relaxed", (1, -1, -1): "docile",
    (-1, -1, -1): "bored", (-1, -1, 1): "disdainful", (-1, 1, -1): "anxious", (-1, 1, 1): "hostile",
}

# octant -> (slightly, moderately, fully) first-person phrases
MOOD_PHRASES_ZH: Dict[str, Tuple[str, str, str]] = {
    "exuberant": ("心情不错，挺有精神的", "心情很好，挺来劲的", "超开心，整个人都很兴奋"),
    "dependent": ("心情不错，有点想黏着人", "挺开心的，很想找人分享", "特别开心，很想有人陪着"),
    "relaxed": ("挺放松的", "很放松，心里挺踏实", "特别放松自在，什么都不急"),
    "docile": ("心情平和，有点懒洋洋的", "很平和，懒懒的", "软绵绵的，特别平和"),
    "bored": ("有点低落，提不起劲", "挺低落的，有点闷", "很消沉，什么都不想干"),
    "disdainful": ("有点淡淡的，不太起劲", "有点冷淡，不太想搭理人", "很冷淡，看什么都不顺眼"),
    "anxious": ("有点不安", "挺焦虑的，心里不踏实", "特别慌，心里乱糟糟的"),
    "hostile": ("有点烦躁", "挺烦的，有点火气", "很生气，火气很大"),
}
NEUTRAL_MOOD_ZH = "心情挺平静的"
