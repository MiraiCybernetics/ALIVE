"""Dialogue catalog: moves (docs/19 §5.1), speech genres (docs/20 §3), frames, circles, goal types, priors.

Plain data so humans/AI can extend it. Priors are multiplicative weights around 1.0.
"""
from __future__ import annotations

MOVES: dict = {
    # 接
    "react":        "对内容本身直接反应（啊？？/绝了/真的假的）",
    "backchannel":  "应和，表示在听（嗯嗯/哈哈）",
    # 问
    "open_q":       "开放问题，让他多说（然后呢/咋回事）",
    "closed_q":     "具体的小问题（哪项挂了/几点）",
    "check":        "确认理解（你是说…？）",
    # 映
    "reflect":      "用自己的话接住他话里的要点（不是宣告他的情绪）",
    "affirm":       "具体地肯定他做的事",
    # 给
    "inform":       "给信息",
    "suggest":      "给建议（只在他要办法时）",
    "self_disclose":"说自己类似的经历或感受",
    "share":        "分享见闻、链接",
    "offer":        "提出可以帮的忙",
    # 引
    "guide_q":      "引导性问题：让他自己想到（苏格拉底式）",
    "reframe_q":    "换个角度的问题（如果是朋友遇到这事你会怎么说）",
    "callback":     "回到我们共同的记忆或旧梗",
    "pre_sequence": "正式邀约或请求前先探路（周末有空吗）",
    # 转
    "shift":        "带过渡地转个话题",
    "return":       "回到之前没聊完的事",
    # 玩
    "tease":        "打趣（只在亲近、在玩、不碰敏感点时）",
    "play_along":   "接梗",
    # 序
    "invite":       "发出可以轻松拒绝的邀约",
    "accept":       "接受",
    "decline":      "婉拒：稍缓、带缓冲和理由",
    # 面子
    "cushion":      "缓冲：犹豫词、模糊限制语（呃…/可能不太行诶）",
    "face_save":    "给台阶、打圆场、自嘲",
    # 我（docs/21 §2.3）
    "own_view":     "表达自己的看法或态度",
    "disagree_soft":"有保留地不同意，带理由，不否定他这个人",
    "qualify":      "同意一部分：认可这件事本身，但不跟着把结论说满（对事不对人，落回眼前这件具体的事）",
    "self_topic":   "主动提起自己想聊的事（带过渡）",
    "counter":      "打趣式回怼、唱反调（亲近、在玩时）",
    # 收
    "presence":     "只表示我在（还没睡/在呢）",
    "close":        "自然结束",
    "wait":         "不说话，留白",
}

GENRES: dict = {
    "greeting":   {"name": "寒暄", "expect": "对等的轻回应", "pref": {"react": 1.3, "backchannel": 1.2, "closed_q": 1.1}},
    "chitchat":   {"name": "闲聊", "expect": "接话、延展", "pref": {"react": 1.4, "play_along": 1.2, "share": 1.1, "tease": 1.1, "open_q": 1.0, "own_view": 1.1, "self_topic": 1.0}},
    "story":      {"name": "讲经历", "expect": "回应他对这件事的感受与立场（评价），不要只复述情节", "pref": {"react": 1.4, "reflect": 1.2, "closed_q": 1.2, "open_q": 1.1, "self_disclose": 0.8}},
    "venting":    {"name": "吐槽", "expect": "站在他这边，先接住情绪", "pref": {"react": 1.5, "reflect": 1.2, "open_q": 1.1, "suggest": 0.3, "guide_q": 0.5}},
    "confiding":  {"name": "倾诉", "expect": "被听见", "pref": {"presence": 1.4, "open_q": 1.3, "reflect": 1.3, "suggest": 0.3, "tease": 0.1}},
    "good_news":  {"name": "报喜", "expect": "先一起高兴", "pref": {"react": 1.6, "affirm": 1.3, "closed_q": 1.2, "suggest": 0.3}},
    "request":    {"name": "求助", "expect": "认真帮忙", "pref": {"closed_q": 1.2, "suggest": 1.4, "inform": 1.4, "offer": 1.2, "presence": 0.6}},
    "question":   {"name": "提问", "expect": "回答问题", "pref": {"inform": 1.4, "react": 1.0, "closed_q": 0.9}},
    "invite":     {"name": "邀约", "expect": "接受或婉拒；婉拒稍缓并带理由", "pref": {"accept": 1.4, "decline": 1.0, "cushion": 1.1, "closed_q": 1.0}},
    "decline":    {"name": "他婉拒我", "expect": "爽快接受，给台阶，不追问", "pref": {"accept": 1.5, "face_save": 1.3, "open_q": 0.2, "closed_q": 0.3}},
    "apology":    {"name": "道歉", "expect": "接受并给台阶", "pref": {"accept": 1.4, "face_save": 1.4, "react": 1.0}},
    "thanks":     {"name": "道谢", "expect": "亲近圈轻轻带过，外圈礼貌回应", "pref": {"backchannel": 1.2, "accept": 1.2, "react": 1.0}},
    "banter":     {"name": "打趣斗嘴", "expect": "接住，用同样的轻松语气回", "pref": {"play_along": 1.6, "tease": 1.4, "react": 1.2, "counter": 1.4, "reflect": 0.3}},
    "discussion": {"name": "讨论", "expect": "有来有往地表达观点", "pref": {"inform": 1.2, "react": 1.0, "check": 1.1, "open_q": 1.0, "own_view": 1.5, "disagree_soft": 1.1}},
    "farewell":   {"name": "告别", "expect": "干脆温暖地结束", "pref": {"close": 1.8, "open_q": 0.1, "shift": 0.1}},
    "fragment":   {"name": "没头没尾的一句", "expect": "挂到最近的话题上理解，并接住他", "pref": {"closed_q": 1.3, "react": 1.3, "presence": 1.2, "open_q": 1.0}},
    "other":      {"name": "其他", "expect": "自然回应", "pref": {}},
}

FRAMES = {"serious": "认真", "play": "在玩", "venting": "吐槽", "confiding": "倾诉", "ritual": "客套/礼节", "conflict": "有冲突"}

CIRCLES = [  # (centre, key, name, how to talk) - the centres of SOFT memberships, not buckets (circle_membership)
    (0.6, "intimate", "亲近圈", "很随意，可以打趣、一两个字；客套反而见外"),
    (0.3, "familiar", "熟人圈", "自然随意，适度客套"),
    (0.1, "acquaintance", "泛交圈", "礼貌自然，不过分亲昵"),
    (-1.0, "stranger", "陌生", "礼貌、保持距离，不透露私事"),
]
# a representative closeness for each circle label (when only the label is known, e.g. a stored situation)
CIRCLE_CLOSENESS = {"intimate": 0.8, "familiar": 0.45, "acquaintance": 0.2, "stranger": 0.0}
CIRCLE_WIDTH = 0.06      # logistic width of each boundary: 27%..73% within +-0.06 of it


def circle_membership(closeness: float, width: float = CIRCLE_WIDTH) -> dict:
    """Soft membership of the closeness circles (差序格局 is graded, not bucketed): differences of logistic steps at
    0.6 / 0.3 / 0.1, summing to 1. Behaviour is weighted by these, never switched by a bucket."""
    import math

    def s(c0: float) -> float:
        return 1.0 / (1.0 + math.exp(-(closeness - c0) / width))
    a, b, c = s(0.6), s(0.3), s(0.1)
    b, c = max(b, a), max(c, max(b, a))
    return {"intimate": a, "familiar": b - a, "acquaintance": c - b, "stranger": 1.0 - c}

NEED_PRIORS: dict = {
    "listen":    {"presence": 1.2, "open_q": 1.2, "reflect": 1.2, "suggest": 0.4},
    "advice":    {"suggest": 1.5, "inform": 1.3, "closed_q": 1.1},
    "reassure":  {"presence": 1.2, "affirm": 1.2, "callback": 1.1, "suggest": 0.6},
    "company":   {"presence": 1.4, "react": 1.1, "share": 1.0},
    "celebrate": {"react": 1.4, "affirm": 1.3},
    "info":      {"inform": 1.5, "closed_q": 1.0},
    "space":     {"wait": 1.5, "close": 1.2, "presence": 1.0, "open_q": 0.3, "closed_q": 0.4},
    "esteem":    {"affirm": 1.5, "reflect": 1.1},
    "fun":       {"play_along": 1.4, "tease": 1.2, "react": 1.2},
}

AGENCY_MOVES = {"own_view", "disagree_soft", "qualify", "self_topic", "counter"}
# moves that ask for an answer (docs/19 §7): only for these is a missing answer evidence about the move. After a
# closing, a presence, a reaction or a story of mine, silence is a normal outcome and is left unevaluated.
ASKING_MOVES = {"open_q", "closed_q", "check", "guide_q", "reframe_q", "invite", "pre_sequence", "offer"}
STANCE_MOVES = {"own_view", "disagree_soft", "qualify"}     # docs/21 §2.5: stance on a claim, not on a feeling
LISTEN_NEEDS = {"listen", "reassure", "company"}

GOAL_TYPES: dict = {
    "connect": "陪伴与连接", "comfort": "安慰", "understand": "了解他", "elicit": "让他把心里的话说出来",
    "reframe": "帮他换个对他自己有益的角度", "help": "帮他解决问题", "share": "分享", "invite": "邀约",
    "respond_invite": "回应他的邀约", "play": "一起玩", "maintain": "维系关系", "close": "自然结束",
}
GUIDING_GOALS = {"elicit", "reframe"}

GENRE_TO_GOAL = {
    "confiding": "comfort", "venting": "connect", "story": "understand", "good_news": "connect", "request": "help",
    "question": "help", "invite": "respond_invite", "decline": "maintain", "apology": "maintain", "thanks": "maintain",
    "banter": "play", "chitchat": "connect", "greeting": "maintain", "farewell": "close", "fragment": "understand",
    "discussion": "understand", "other": "connect",
}
