"""Non-dialogue action model (docs/22): perceive without replying, act later, speak unprompted, group floor."""
import random

import pytest

from alive.core.types import ChatLine, OutreachPlan, ReplyTask, Trust
from alive.interfaces import Decision, Urge
from alive.testing import make_test_kernel
from alive.core.initiative import get_initiative


class _Low(random.Random):
    """An RNG whose draws always fall under the probability: the sampled acts happen (deterministic tests)."""
    def random(self):
        return 0.0

    def gauss(self, mu=0.0, sigma=1.0):
        return mu


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=["mind", "selfm"])
    yield kern
    await kern.stop()


def _lines(k, n=3, who="阿明", pk="sim:ming", text="今天在研究怎么做手冲咖啡"):
    now = k.clock.now()
    return [ChatLine(sender=who, person_key=pk, text=f"{text} {i}", ts=now - (n - i) * 30, msg_id=f"m{i}") for i in range(n)]


async def test_observe_files_a_space_item_without_llm(k):
    task = ReplyTask(thread_id="sim:g1", channel="sim", chat_type="group", chat_title="摸鱼", participants=["sim:ming"],
                     history=[], new_lines=_lines(k), trust=Trust.CONTACT)
    await k.mind.observe(task)
    assert "space:sim:g1" in k.mind.ws.items
    assert k.mock.calls == []


async def test_later_intent_becomes_an_unprompted_turn(k):
    hist = _lines(k, 2, text="我下午去面试")

    async def history(tid, limit=30):
        return hist
    k.social.history = history
    k.mock.on("s2.reply", {"reply": ["加油"], "later": {"in_minutes": 90, "intent": "问问他面试怎么样了"}})
    task = ReplyTask(thread_id="sim:ming", channel="sim", chat_type="private", chat_title="", participants=["sim:ming"],
                     history=[], new_lines=hist, trust=Trust.CONTACT)
    res = await k.mind.compose_reply(task)
    assert res.segments == ["加油"]
    hist.append(ChatLine(sender="小屿", person_key="", text="加油", ts=k.clock.now(), mine=True))
    k.mock.on("s2.reply", {"reply": ["面试咋样了"]})
    k.mind._autonomy_go = lambda *a, **kw: True       # the hazard to act on my own and the moment-to-speak draw
    get_initiative(k)._rng = lambda: _Low()          # both succeed (sampled in production)
    await k.clock.advance(95 * 60)
    for _ in range(6):
        await k.clock.advance(300)
    sent = [s for s in k.social.sent if s[0] == "sim:ming"]
    assert sent and sent[-1][1] == ["面试咋样了"]
    prompt = k.mock.calls_for("s2.reply")[-1].messages[0]["content"]
    assert "问问他面试怎么样了" in prompt and "没有人要求你此刻说话" in prompt


async def test_how_many_messages_is_sampled_from_state_not_fixed(k):
    """No fixed '1 message in a group': the count is 1 + Poisson(lambda) from chattiness, arousal, energy and the
    floor; fewer in a shared space unless addressed, fewer when I already hold the floor, never above the bound."""
    ini = get_initiative(k)
    import random as _r
    ini._rng = lambda: rng
    rng = _r.Random(5)

    def mean(**kw):
        return sum(ini.message_count(upper=4, **kw) for _ in range(800)) / 800
    private = mean(shared=False, mentioned=False)
    group = mean(shared=True, mentioned=False)
    addressed = mean(shared=True, mentioned=True)
    hogging = mean(shared=True, mentioned=False, floor_ratio=3.0)
    assert 1.0 <= hogging < group < addressed <= private <= 4.0
    assert private > 1.2 and group < 1.8                 # tendencies, with variation both ways
    counts = {ini.message_count(shared=True, mentioned=False, upper=4) for _ in range(300)}
    assert counts >= {1, 2}                              # sometimes more than one in a group too
    k.mock.on("s2.reply", {"reply": ["哈哈", "我也觉得", "真的", "对"]})
    res = await k.mind.compose_reply(ReplyTask(thread_id="sim:g1", channel="sim", chat_type="group", chat_title="摸鱼",
                                               participants=["sim:ming"], history=[], new_lines=_lines(k, 1),
                                               trust=Trust.CONTACT, max_segments=3))
    assert 1 <= len(res.segments) <= 3


async def test_yield_the_floor_when_hogging(k):
    now = k.clock.now()
    hist = []
    for i in range(10):
        hist.append(ChatLine(sender="小屿", person_key="", text=f"我说的 {i}", ts=now - 600 + i * 50, mine=True))
    hist.append(ChatLine(sender="阿明", person_key="sim:ming", text="嗯", ts=now - 20))
    k.mock.on("s2.reply", {"reply": ["还有一件事"]})
    task = ReplyTask(thread_id="sim:g1", channel="sim", chat_type="group", chat_title="摸鱼", participants=["sim:ming"],
                     history=hist, new_lines=[ChatLine(sender="小林", person_key="sim:lin", text="哈哈哈", ts=now)],
                     trust=Trust.CONTACT)
    get_initiative(k)._rng = lambda: _Low()          # yielding is sampled; force the draw
    res = await k.mind.compose_reply(task)
    assert res.silent and res.reason == "yield_floor"
    assert k.mock.calls_for("s2.reply") == []


async def test_blocked_outreach_is_not_retried_every_tick(k):
    plan = OutreachPlan(id="op1", person_key="sim:ming", thread_id="sim:ming", reason_type="care",
                        content_hint="问候", score=0.9, created_at=k.clock.now())
    checks = {"n": 0}

    async def propose(urges, now):
        return [plan]

    def allowed(pk, now):
        checks["n"] += 1
        return Decision(False, "min gap 4 h")
    k.motivation.propose_outreach = propose
    k.motivation.tick = lambda dt, now, a: [Urge(need="social", pressure=0.9, suggestion="想找阿明")]
    k.gov.initiative_allowed = allowed
    for _ in range(12):
        await k.clock.advance(300)
    assert checks["n"] <= 2


async def test_overload_counts_what_waits_for_me_and_wears_off(k):
    """Modes (ADR-0007): my own passing thoughts don't make me 'overwhelmed'; open demands do, and the state
    wears off the longer it lasts (habituation) -- nobody stays overwhelmed for hours over a few chats."""
    import random as _r
    from alive.core.types import Mode
    from alive.core.workspace import WorkspaceItem
    m = k.mind
    k.chance_rng = _r.Random(4)
    now = k.clock.now()
    m.ws.capacity = 4
    for i in range(14):                                     # many thoughts of my own, nothing waiting for me
        m.ws.offer(WorkspaceItem(key=f"thought:{i}", kind="thought", text=f"想到{i}", salience=0.5, ttl=7200), now)

    class Snap:
        energy, A, P = 0.8, 0.3, 0.0
    m.last_activity = now - 3600
    seen = set()
    for step in range(60):
        m._mode_next_at = None
        m._update_mode(now + step * 60, Snap())
        seen.add(m._mode)
    assert Mode.OVERWHELMED not in seen
    for i in range(12):                                     # now a pile of unanswered things waiting for me
        m.ws.offer(WorkspaceItem(key=f"space:sim:p{i}", kind="space", text=f"有人在等我回{i}", salience=0.6,
                                 ttl=7200, open=0.9), now)
    m.overwhelmed_since = None
    modes = []
    for step in range(600):                                 # ten hours, re-settling every minute
        m._mode_next_at = None
        m._update_mode(now + 3600 + step * 60, Snap())
        modes.append(m._mode)
    first_hour = sum(1 for x in modes[:60] if x == Mode.OVERWHELMED)
    assert first_hour > 20                                  # the load is felt
    longest, run = 0, 0
    for x in modes:
        run = run + 1 if x == Mode.OVERWHELMED else 0
        longest = max(longest, run)
    assert longest < 240                                    # ...but not for four hours straight
