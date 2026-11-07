"""End-to-end smoke tests through the sim adapter."""
from alive.core.types import EV

from tests.social.soc_helpers import events


async def test_private_message_gets_reply(kern):
    k = kern
    sim = k.social.adapter("sim")
    await sim.inject("你好呀，今天过得怎么样？", sender="u1", sender_name="小明")
    assert sim.outputs == []
    await k.clock.advance(60)
    assert sim.texts("u1") == ["好的"]
    assert any(c[0] == "compose_reply" for c in k.mind.calls)
    hist = await k.social.history("sim:u1")
    assert [l.mine for l in hist] == [False, True]
    decs = await events(k, EV.DECISION, "sim:u1")
    assert decs and decs[-1].payload["outcome"] == "respond"
    msgs = await events(k, EV.MESSAGE_IN, "sim:u1")
    assert len(msgs) == 1 and msgs[0].payload["person_key"] == "sim:u1"
    assert await k.social.person_name("sim:u1") == "小明"
    assert k.selfm.outcomes and "小明" in k.selfm.outcomes[-1]
