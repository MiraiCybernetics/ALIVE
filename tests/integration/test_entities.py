"""Entity-centric world (docs/22): one person across channels, routes follow the latest activity."""
import pytest

from alive.core.types import Trust
from alive.kernel import FACTORIES
from alive.testing import make_test_kernel

CH = [{"kind": "sim", "name": "sim"}, {"kind": "sim", "name": "sim2"}]


@pytest.fixture
async def k(tmp_path):
    kern = await make_test_kernel(tmp_path, real=list(FACTORIES), cfg_overrides={
        "channels": CH, "world": {"identities": [{"name": "阿明", "handles": ["sim:ming", "sim2:ming_alt"]}]}})
    yield kern
    await kern.stop()


async def test_same_person_on_two_channels_is_one_entity(k):
    eid = k.world.person("sim", "ming")
    assert k.world.person("sim2", "ming_alt") == eid
    k.gov.set_trust(eid, Trust.CONTACT)
    k.mock.on("s2.reply", {"reply": ["收到"], "moves": ["react"]})
    a, b = k.social.adapter("sim"), k.social.adapter("sim2")
    await a.inject("我在 A 平台", sender="ming", sender_name="阿明", chat_id="ming", chat_type="private")
    for _ in range(12):
        await k.clock.advance(5)
    await b.inject("现在换到 B 平台了", sender="ming_alt", sender_name="明", chat_id="ming_alt", chat_type="private")
    for _ in range(12):
        await k.clock.advance(5)
    # one space for the person, history merged across channels
    hist = await k.social.history(eid, 20)
    texts = [l.text for l in hist if not l.mine]
    assert "我在 A 平台" in texts and "现在换到 B 平台了" in texts
    # the latest reply went out on the channel he used last
    assert b.outputs and b.outputs[-1].text == "收到"
    assert k.world.route(eid)[0] == "sim2"
    # governance trust and relationship are per entity, not per channel
    assert k.gov.get_trust(eid) == Trust.CONTACT
    assert "明" in (k.world.entity(eid).aliases + [k.world.entity(eid).name])


async def test_group_membership_is_recorded(k):
    k.mock.on("s2.reply", {"reply": []})
    a = k.social.adapter("sim")
    await a.inject("大家好", sender="x", sender_name="小X", chat_id="g7", chat_type="group")
    await k.clock.advance(10)
    gid = k.world.group("sim", "g7")
    assert k.world.kind(gid) == "group"
    assert k.world.person("sim", "x") in k.world.members_of(gid)
