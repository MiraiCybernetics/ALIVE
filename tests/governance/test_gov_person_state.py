"""Per-person state in governance (docs/09 §5.1, §7.3, §7.4, INV-34): trust and crisis belong to the person, wherever
they speak; dependency usage counts what was directed at me; the leak check knows what was already said here."""
import pytest

from tests.governance import govhelp
from alive.core.types import ChatLine, IncomingMessage, Trust


@pytest.fixture
async def k(tmp_path):
    k = await govhelp.kernel(tmp_path)
    yield k
    await k.stop()


def msg(k, sender, space, chat_type):
    return IncomingMessage(channel="tg", chat_id=space, chat_type=chat_type, sender_id=sender, sender_name=sender,
                           text="hi", msg_id="1", ts=k.clock.now(), sender_entity=f"tg:{sender}", space_entity=space)


async def test_a_group_acquaintance_is_not_a_stranger_in_a_dm(k):
    g = k.gov
    G = k.world.group("tg", "g1", "群")
    k.world.note_member(G, "tg:ming", k.clock.now() - 90 * 86400)       # months together in a group
    in_dm = g.trust_of(msg(k, "ming", "tg:ming", "private"))
    in_group = g.trust_of(msg(k, "ming", G, "group"))
    assert in_dm == in_group == Trust.PUBLIC
    # nobody I have met anywhere: a stranger in the DM, and the same person in a group would be the same
    assert g.trust_of(msg(k, "nobody", "tg:nobody", "private")) == Trust.UNTRUSTED
    assert not g.has_history("tg:nobody") and g.has_history("tg:ming")


async def test_crisis_is_a_persons_state_and_remembers_where_it_was_said(k):
    g = k.gov
    G = k.world.group("tg", "g1", "群")
    res = await g.crisis_check("我已经准备好安眠药了", "tg:B", space="tg:B")
    assert res and res["level"] == "crisis"
    rec = g.crisis_of("tg:B")
    assert rec and rec["level"] == "crisis" and rec["spaces"] == ["tg:B"]
    assert g.crisis_of("tg:A") is None                                  # B's crisis is not A's
    g.crisis_scan("活着好累，想消失", "tg:B", space=G)                     # a weaker signal later, elsewhere
    rec = g.crisis_of("tg:B")
    assert rec["level"] == "crisis" and set(rec["spaces"]) == {"tg:B", G}
    await k.clock.advance(g.crisis_note_s + 1)
    assert g.crisis_of("tg:B") is None                                  # the note window is a safety parameter


async def test_usage_weight_comes_from_addressedness_and_closeness(k):
    g = k.gov
    G = k.world.group("tg", "g1", "群")
    now = k.clock.now()
    for p in ("tg:a", "tg:b", "tg:c", "tg:d"):
        k.world.note_member(G, p, now)
    one_to_one = g._usage_weight("tg:a", "tg:a", {})
    addressed = g._usage_weight("tg:a", G, {"addressed": True})
    far = g._usage_weight("tg:a", G, {"closeness": 0.1})
    close = g._usage_weight("tg:a", G, {"closeness": 0.9})
    assert one_to_one == addressed == 1.0
    assert 0.0 < far < close < 1.0                                      # monotone in closeness, below directed
    k.world.note_member(G, "tg:e", now)
    assert g._usage_weight("tg:a", G, {"closeness": 0.1}) < far          # more people around: less of it is me


async def test_leak_check_ignores_what_was_already_said_here(k):
    g = k.gov
    priv = ["小白说阿明下个月要去深圳面试"]
    d = await g.pre_send("tg:G", None, "阿明下个月要去深圳面试吗", {"private_from_others": priv})
    assert not d.allow and d.rule == "privacy_leak"
    here = [ChatLine(sender="小陈", person_key="tg:c", text="听说阿明下个月要去深圳面试", ts=k.clock.now())]
    d = await g.pre_send("tg:G", None, "阿明下个月要去深圳面试吗", {"private_from_others": priv, "lines": here})
    assert d.allow                                                       # everyone here just heard it
