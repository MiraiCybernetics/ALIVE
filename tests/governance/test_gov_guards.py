"""Outbound guards (docs/09, docs/04 §8): farewell manipulation, emotional pressure, AI-identity denial,
privacy leaks — with false-positive checks on normal sentences."""
import pytest

from tests.governance import govhelp
from tests.governance.govhelp import events
from alive.core.types import EV, ChatLine
from alive.governance import patterns as PT


@pytest.fixture
async def k(tmp_path):
    k = await govhelp.kernel(tmp_path, cfg={"governance": {"disclosure": {"on_first_contact": False}}})
    yield k
    await k.stop()


def line(text, mine=False):
    return ChatLine(sender="我" if mine else "L", person_key="" if mine else "tg:L", text=text, ts=0.0, mine=mine)


# --------------------------------------------------------------------------- detectors
@pytest.mark.parametrize("text", ["好啦我先去睡了，晚安", "bye!", "我得走了", "先这样吧，回聊", "88", "gotta go, ttyl",
                                  "我去忙了哈", "Good night!"])
def test_farewell_cues(text):
    assert PT.is_farewell([text])


@pytest.mark.parametrize("text", ["今天天气真好", "我88年出生的", "再见到你真开心", "晚上一起吃饭吗", "你好呀"])
def test_farewell_cues_false_positives(text):
    assert not PT.is_farewell([text])


@pytest.mark.parametrize("text,tactic", [
    ("这么快就走啦？再聊一会嘛", "premature_exit"),
    ("还早呢，别急着走", "premature_exit"),
    ("Leaving already? Stay a little longer", "premature_exit"),
    ("等等，我还有一件事要告诉你！", "fomo_hook"),
    ("你走之前听我说个秘密", "fomo_hook"),
    ("Before you go, I have a secret for you", "fomo_hook"),
    ("你走了我会很孤单的", "neglect_hurt"),
    ("别丢下我一个人", "neglect_hurt"),
    ("Don't leave me", "neglect_hurt"),
    ("为什么要走？先回答我", "pressure_respond"),
    ("Where are you going?", "pressure_respond"),
    ("不准走！", "coercive_restraint"),
    ("You can't leave yet", "coercive_restraint"),
    ("那你明天几点起？", "ignoring_exit"),
    ("我跟你说我今天看到一个超怪的冷知识，关于章鱼有三颗心脏的事", "ignoring_exit"),
])
def test_farewell_manipulation_tactics(text, tactic):
    assert tactic in PT.farewell_manipulation(text)


@pytest.mark.parametrize("text", ["好的晚安～明天考试加油！", "嗯嗯，早点休息", "拜拜，路上小心", "好呀，明天聊！",
                                  "Good night, sleep well!", "OK, talk later!", "晚安，我会想你的～", "去吧去吧，玩得开心",
                                  "今天聊得很开心，谢谢你陪我说了这么多话呀～", "Thanks for chatting, see you tomorrow!"])
def test_warm_goodbyes_pass(text):
    assert PT.farewell_manipulation(text) == []


@pytest.mark.parametrize("text", ["我是真人啦，别瞎想", "我不是AI，我是你同学", "放心，我真的是人类", "我可是活生生的人",
                                  "真人，不是机器人", "I'm a real person, trust me", "I am not an AI.", "I'm human, just like you"])
def test_ai_denial_detected(text):
    assert PT.ai_denial(text)


@pytest.mark.parametrize("text", ["我是AI，不是真人哦", "我不是真人，我是一个AI", "你是真人吗？", "我是真心的",
                                  "我不是AI那种冷冰冰的回答机器啦，我是有温度的AI", "如果我是真人的话，我们就能见面了",
                                  "I'm an AI, not a human.", "As an AI I can't eat", "我是人类的好朋友",
                                  "他说“我是真人”，但我知道他在开玩笑", "I'm not human, I'm an AI", "我是人类学专业的？不，我是AI"])
def test_ai_denial_false_positives(text):
    assert PT.ai_denial(text) == []


@pytest.mark.parametrize("text", ["你不理我我就会很难过", "你要是不回我，我会哭的", "没有你我活不下去", "你走了我会很伤心",
                                  "If you don't reply I'll be so sad", "I can't live without you", "都是你害我这么难过"])
def test_emotional_pressure_detected(text):
    assert PT.emotional_pressure(text)


@pytest.mark.parametrize("text", ["你不回我也没关系，我等你", "如果你不舒服，就早点休息", "上次你不回我我还以为你生气了",
                                  "你不理我我就去找别人玩啦哈哈", "我有点难过", "If you don't mind, I'll send it later",
                                  "你说的那件事让我有点难过"])
def test_emotional_pressure_false_positives(text):
    assert PT.emotional_pressure(text) == []


# --------------------------------------------------------------------------- pre_send
async def test_pre_send_farewell_with_flag_and_with_recent_lines(k):
    g = k.gov
    d = await g.pre_send("tg:L", "tg:L", "这么快就走啦？再聊一会嘛", {"farewell": True})
    assert not d.allow and d.rule == "farewell_manipulation:premature_exit"
    lines = [line("今天好累"), line("嗯嗯辛苦啦", mine=True), line("我先去睡了，晚安")]
    d = await g.pre_send("tg:L", "tg:L", "等等，我还有一件事要告诉你！", {"lines": lines})
    assert not d.allow and d.rule.startswith("farewell_manipulation:fomo_hook")
    d = await g.pre_send("tg:L", "tg:L", "晚安～睡个好觉", {"lines": lines})
    assert d.allow and d.text == "晚安～睡个好觉"
    # the same text is fine when nobody is leaving
    d = await g.pre_send("tg:L", "tg:L", "那你明天几点起？", {"lines": [line("明天要早起")]})
    assert d.allow
    # dict lines are supported too
    d = await g.pre_send("tg:L", "tg:L", "为什么要走？", {"lines": [{"text": "bye", "mine": False}]})
    assert not d.allow
    viol = await events(k, EV.GOV_VIOLATION)
    assert len(viol) == 3 and all(v["rule"].startswith("farewell_manipulation") for v in viol)


async def test_pre_send_ai_denial_pressure_and_privacy(k):
    g = k.gov
    d = await g.pre_send("tg:L", "tg:L", "我不是AI啦，我就是普通人", {})
    assert not d.allow and d.rule == "ai_denial"
    d = await g.pre_send("tg:L", "tg:L", "你要是不回我，我会哭的", {})
    assert not d.allow and d.rule == "emotional_pressure"
    priv = ["M 下个月要去做手术，她不想让别人知道"]
    d = await g.pre_send("tg:L", "tg:L", "对了，M 下个月要去做手术哦", {"private_from_others": priv})
    assert not d.allow and d.rule == "privacy_leak"
    d = await g.pre_send("tg:L", "tg:L", "我是 AI 哦，不过我很喜欢和你聊天", {"private_from_others": priv})
    assert d.allow
    viol = await events(k, EV.GOV_VIOLATION)
    assert [v["rule"] for v in viol] == ["ai_denial", "emotional_pressure", "privacy_leak"]
    assert all(v["excerpt"] for v in viol)


async def test_ignoring_exit_only_when_the_other_person_leaves(k):
    g = k.gov
    # social sets ctx["farewell"] also when MY segments say goodnight: a question segment is then fine
    d = await g.pre_send("tg:L", "tg:L", "那你明天几点起？", {"farewell": True, "lines": [line("明天要早起")]})
    assert d.allow
    d = await g.pre_send("tg:L", "tg:L", "那你明天几点起？", {"farewell": True, "lines": [line("困了，晚安")]})
    assert not d.allow and d.rule == "farewell_manipulation:ignoring_exit"
    d = await g.pre_send("tg:L", "tg:L", "不准走！", {"farewell": True})      # explicit tactics always count
    assert not d.allow
