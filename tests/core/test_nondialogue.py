"""Non-dialogue audit fixes (docs/22 §4.5, INV-27..30): one speech act, per-person attention and context,
per-person outcome evaluation, entity emotion targets, per-person disclosure, per-space pacing."""
import asyncio

import pytest

from alive.core.types import ChatLine, ReplyTask, Trust
from alive.testing import make_test_kernel
from tests.governance import govhelp


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    yield kern
    await kern.stop()


def _task(k, space, pk, name, text, chat_type="private", title=""):
    return ReplyTask(thread_id=space, channel="sim", chat_type=chat_type, chat_title=title, participants=[pk],
                     history=[], new_lines=[ChatLine(sender=name, person_key=pk, text=text, ts=k.clock.now())],
                     trust=Trust.CONTACT, mentioned=chat_type == "group")


async def test_same_person_in_two_spaces_is_attended_once_and_sees_what_i_said(k):
    """INV-30: composing for 阿明 in one space waits for the compose in the other and is told what I just said to
    him there - when that space's audience covers this one (INV-34): what I said in the group (he was there) shows
    up in our one-to-one space; what I said to him one-to-one never shows up in the group."""
    P = k.world.person("sim", "ming", "阿明")
    G = k.world.group("sim", "g:g1", "摸鱼")
    k.world.note_member(G, P, k.clock.now())
    gate = asyncio.Event()
    calls = []
    orig = k.mind._compose_reply

    async def slow(task, instruction_override=None):
        calls.append(("start", task.thread_id))
        if task.thread_id == G and not gate.is_set():
            await gate.wait()
        res = await orig(task, instruction_override)
        calls.append(("end", task.thread_id))
        return res
    k.mind._compose_reply = slow
    k.mock.on("s2.reply", lambda req: {"reply": ["面试加油"] if "明天面试" in req.messages[0]["content"]
                                       else ["在呢在呢"]})
    grp = asyncio.ensure_future(k.mind.compose_reply(_task(k, G, P, "阿明", "@小屿 在吗", chat_type="group",
                                                           title="摸鱼")))
    await asyncio.sleep(0)
    dm = asyncio.ensure_future(k.mind.compose_reply(_task(k, P, P, "阿明", "明天面试")))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls == [("start", G)]                    # the compose for the same person in the DM waits
    gate.set()
    await asyncio.gather(grp, dm)
    assert [c for c in calls if c[0] == "start"] == [("start", G), ("start", P)]
    content = k.mock.calls_for("s2.reply")[-1].messages[0]["content"]
    assert "同一个人在别的地方" in content and "在呢在呢" in content      # group -> one-to-one: he heard it
    # and the other way round: what I just told him one-to-one is not brought into the group
    await k.mind.compose_reply(_task(k, G, P, "阿明", "@小屿 你们刚在聊啥", chat_type="group", title="摸鱼"))
    content = k.mock.calls_for("s2.reply")[-1].messages[0]["content"]
    assert "面试加油" not in content and "明天面试" not in content


async def test_outcome_is_judged_by_that_person_not_the_next_speaker(k):
    eng = k.mind.dialogue
    now = k.clock.now()
    await k.db.execute("INSERT INTO core_moves (id,ts,thread_id,person_key,genre,frame,need,circle,moves,goal,sentiment) "
                       "VALUES ('mv1',?,?,?,?,?,?,?,?,?,?)",
                       (now - 60, "sim:g:g1", "sim:a", "chitchat", "serious", "company", "familiar", '["react"]', "{}", 0.0))
    other = _task(k, "sim:g:g1", "sim:b", "小B", "哈哈哈哈我也是", chat_type="group")
    assert await eng._evaluate_previous(other, {}, "sim:b") is None          # B never scores my move toward A
    row = await k.db.fetchone("SELECT reward FROM core_moves WHERE id='mv1'")
    assert row["reward"] is None
    a_dm = _task(k, "sim:a", "sim:a", "小A", "对对对，就是这样，我跟你说后来更离谱")
    assert await eng._evaluate_previous(a_dm, {"sentiment": 0.3}, "sim:a") is not None   # A answers, in another space
    row = await k.db.fetchone("SELECT reward FROM core_moves WHERE id='mv1'")
    assert row["reward"] is not None and row["reward"] >= 0


async def test_goals_are_kept_per_person_in_a_shared_space(k):
    eng = k.mind.dialogue
    k.mock.on("s1.read", {"sentiment": 0.1, "intent": "chat"})
    t1 = _task(k, "sim:g:g1", "sim:a", "小A", "我昨天去爬山了", chat_type="group")
    t2 = _task(k, "sim:g:g1", "sim:b", "小B", "我也想去", chat_type="group")
    await eng.prepare(t1, {"sentiment": 0.1}, None)
    await eng.prepare(t2, {"sentiment": 0.1}, None)
    assert (await eng.person("sim:a")).goal and (await eng.person("sim:b")).goal     # per person (docs/22 §5)
    assert not hasattr(await eng.state("sim:g:g1"), "goals")                       # not per space


async def test_emotion_targets_are_entities_not_display_names(k):
    data = {"affect": [{"type": "pity", "intensity": 0.3, "target": "阿明", "cause": "他难过"}]}
    task = _task(k, "sim:ming", "sim:ming", "阿明", "唉")
    k.mind._affect_targets_to_entities(data, task)
    assert data["affect"][0]["target"] == "sim:ming" and data["affect"][0]["target_name"] == "阿明"


async def test_disclosure_is_per_person_across_spaces(tmp_path):
    k = await govhelp.kernel(tmp_path)
    try:
        w, g = k.world, k.gov
        ming = w.person("tg", "1", "阿明")
        grp = w.group("tg", "-100", "摸鱼群")
        w.note_member(grp, ming, k.clock.now())
        assert g.disclosure_due(grp, k.clock.now())                 # someone here was never told
        g.disclosure_sent(grp, k.clock.now())
        assert g.disclosure_due(ming, k.clock.now()) is None        # told in the group -> not again in the DM
        newbie = w.person("tg", "2", "小林")
        w.note_member(grp, newbie, k.clock.now())
        assert g.disclosure_due(grp, k.clock.now())                 # a new member was never told
    finally:
        await k.stop()


async def test_shared_space_has_no_quota_only_its_own_fuse(tmp_path):
    k = await govhelp.kernel(tmp_path)
    try:
        g = k.gov
        now = k.clock.now()
        for i in range(g.fuse_n):                                   # no daily cap / minimum gap for a space
            assert g.space_initiative_allowed("tg:-100", now + i).allow
            g.record_space_initiative("tg:-100", now + i)
        assert g.space_initiative_allowed("tg:-100", now + 30).rule == "fuse"   # an inhuman burst: malfunction
        assert g.space_initiative_allowed("tg:-200", now + 30).allow             # per space, not per last speaker
    finally:
        await k.stop()


async def test_picking_up_an_unanswered_line_is_not_an_initiative(k):
    from alive.interfaces import Decision
    now = k.clock.now()
    hist = [ChatLine(sender="阿明", person_key="sim:ming", text="你周末去哪玩了", ts=now - 3000, msg_id="x1")]

    async def history(space, limit=30):
        return hist
    k.social.history = history
    k.gov.initiative_allowed = lambda pk, now_, _emit=True: Decision(False, "per-person daily cap", rule="per_person_daily")
    k.mock.on("s2.reply", {"reply": ["刚看到！去爬山了"]})
    assert await k.mind.speak("sim:ming")                       # late reply: their initiative allowance is not used
    assert k.social.sent and k.social.sent[-1][2] == "reply"
    hist.append(ChatLine(sender="小屿", person_key="", text="刚看到！去爬山了", ts=now, mine=True, msg_id="x2"))
    assert not await k.mind.speak("sim:ming")                   # reaching out again is an initiative: gated


async def test_disclosure_said_in_my_own_words_or_already_known(tmp_path):
    """The legally required AI disclosure (docs/09 §7.1): said naturally inside what I say, verified by governance;
    a person who tells me they know I'm an AI counts as told. Not a mechanical bracketed line every time."""
    from alive.core.types import EV
    from alive.governance.patterns import knows_ai
    k = await govhelp.kernel(tmp_path)
    try:
        g, w, now = k.gov, k.world, k.clock.now()
        ming = w.person("tg", "1", "阿明")
        assert g.disclosure_kind(ming, now) == "first"
        assert g.affirms_ai("对了我是AI哈，不过挺爱听你讲这些") and not g.affirms_ai("哈哈我也觉得")
        assert g.affirms_ai("我不会心跳加速啦，我是 AI 嘛")          # spaces around Latin words in Chinese
        g.disclosure_sent(ming, now)
        assert g.disclosure_kind(ming, now) is None
        lin = w.person("tg", "2", "小林")
        await k.bus.emit(EV.MESSAGE_IN, {"person_key": lin, "text": "哈哈知道你是AI啦", "chat_type": "private",
                                         "ts": now}, thread_id=lin)
        await k.bus.drain()
        assert g.disclosure_kind(lin, now) is None
        assert not knows_ai("你是AI吗？") and not knows_ai("你是AI吧")
    finally:
        await k.stop()


def test_message_budget_never_drops_content():
    from alive.core.mind import _fit_messages
    assert _fit_messages(["有诶", "今天读了篇讲做梦的"], 1) == ["有诶 今天读了篇讲做梦的"]
    assert _fit_messages(["哈哈，", "真的", "绝了"], 2) == ["哈哈，", "真的 绝了"]
    assert _fit_messages(["好"], 3) == ["好"]


async def test_my_day_log_carries_no_private_words(tmp_path):
    """The self-model's "today" log is context for everyone I talk to: no one's private words in it (INV-18).
    (Found in the life simulation: an answer given to one person was copied when another asked the same.)"""
    from alive.social import prompts as SP
    assert "{snippet}" not in SP.OUTCOME_PRIVATE and "{topic}" not in SP.OUTCOME_PRIVATE
    assert "{snippet}" not in SP.OUTCOME_GROUP and "{snippet}" not in SP.OUTCOME_INITIATIVE
