"""Person-level facts in the interaction layer (docs/09 §5.1, docs/22, INV-34): someone I know from a shared
space is not a stranger one-to-one; everything I say reaches memory, prompted or not; what was left out of a
composition is passed to the outbound leak check."""
import asyncio

from alive.core.types import ComposeResult
from alive.testing import make_test_kernel
from tests.social.soc_helpers import decisions, flush, make_kernel


async def test_a_group_acquaintance_is_not_a_stranger_in_a_dm(tmp_path):
    k = await make_kernel(tmp_path, channels=[{"kind": "sim", "name": "sim", "dm_policy": "pairing"}])
    try:
        sim = k.social.adapter("sim")
        await sim.inject("大家周末去爬山吗", sender="u1", sender_name="小明", chat_id="g1", chat_type="group")
        await k.clock.advance(60)
        await sim.inject("在吗，私聊一下", sender="u1", sender_name="小明")          # same person, one-to-one
        await k.clock.advance(60)
        assert k.social.list_pairings() == []
        assert (await decisions(k, "sim:u1"))[-1]["reason"] != "stranger"
        await sim.inject("你好", sender="s9", sender_name="路人")                    # no history anywhere
        await k.clock.advance(60)
        assert [p["person_key"] for p in k.social.list_pairings()] == ["sim:s9"]
    finally:
        await k.stop()


async def test_what_i_say_unprompted_reaches_the_spaces_memory(tmp_path):
    k = await make_test_kernel(tmp_path, real=["social", "memory"],
                               cfg_overrides={"channels": [{"kind": "sim", "name": "sim"}], "social": {"seed": 7}})
    try:
        sim = k.social.adapter("sim")
        await sim.inject("我明天面试", sender="u1", sender_name="小明")
        await k.clock.advance(120)
        await flush(k)
        P = "sim:u1"
        t = asyncio.ensure_future(k.social.send(P, ["面试加油，晚上跟我说说咋样"], kind="initiative", person_key=P))
        for _ in range(1200):                                       # typing takes (virtual) time
            if t.done():
                break
            await k.clock.advance(0.1)
        out = await t
        await k.clock.advance(5)
        await flush(k)
        assert out.get("sent")
        buf = k.memory._buffers[P]
        mine = [l for l in buf.lines + buf.context if l.mine]
        assert [l.text for l in mine].count("面试加油，晚上跟我说说咋样") == 1
        # replies recorded both as I send them and by the exchange bookkeeping are kept once, in order
        ts = [l.ts for l in buf.lines]
        assert ts == sorted(ts)
        texts = [l.text for l in buf.lines + buf.context]
        assert len(texts) == len(set((l.msg_id or l.text) for l in buf.lines + buf.context))
    finally:
        await k.stop()


async def test_withheld_contents_reach_the_leak_check(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        seen = []
        orig = k.gov.pre_send

        async def pre_send(thread_id, person_key, text, ctx):
            seen.append(list(ctx.get("private_from_others") or []))
            return await orig(thread_id, person_key, text, ctx)
        k.gov.pre_send = pre_send

        async def compose(task):
            return ComposeResult(segments=["好呀"], private_used=False, withheld=["小白说阿明下个月要去深圳面试"])
        k.mind.compose_reply = compose
        sim = k.social.adapter("sim")
        await sim.inject("在吗", sender="u1", sender_name="小明")
        for _ in range(6):
            await k.clock.advance(30)
        await flush(k)
        assert seen and seen[-1] == ["小白说阿明下个月要去深圳面试"]
        # the rule of two sees what the context actually held, not the kind of space
        acts = [c[1][0] for c in k.gov.calls if c[0] == "check_action" and c[1][0].kind == "send_message"]
        assert acts and "private" not in acts[-1].session_caps
    finally:
        await k.stop()
