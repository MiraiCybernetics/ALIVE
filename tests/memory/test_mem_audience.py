"""Privacy follows the information (INV-34, docs/03 §8): a record's audience comes from where it was said; what
reaches a space is only what everyone there may hear. Derived records keep their sources' origins."""
import pytest

from alive.core.types import ChatLine, Trust
from alive.world.audience import Scope, origins_of
from tests.memory.memhelp import MONDAY, kernel

DAY = 86400.0


@pytest.fixture
async def k(tmp_path):
    kern = await kernel(tmp_path)
    yield kern
    await kern.stop()


def people(k):
    """A, B, C are people; G is a group with all three; H is a group with A and C only."""
    A, B, C = (k.world.person("tg", x, n) for x, n in (("a", "阿明"), ("b", "小白"), ("c", "小陈")))
    G, H = k.world.group("tg", "g1", "大群"), k.world.group("tg", "g2", "小群")
    for p in (A, B, C):
        k.world.note_member(G, p, MONDAY)
    for p in (A, C):
        k.world.note_member(H, p, MONDAY)
    return A, B, C, G, H


async def secret(k, A, B):
    """B tells me one-to-one something private about A."""
    return await k.memory.note(kind="semantic", content="阿明最近在偷偷准备跳槽去深圳", source="told_by", told_by=B,
                               trust=Trust.CONTACT, person_keys=[A], thread_id=B, observed_at=MONDAY - DAY)


async def test_a_private_disclosure_about_A_never_reaches_A_or_a_group(k):
    A, B, C, G, H = people(k)
    rid = await secret(k, A, B)
    cue = "阿明跳槽深圳"
    for space in (A, G, H, C):
        withheld: list = []
        hits = await k.memory.retrieve(cue, thread_id=space, withheld=withheld)
        assert rid not in [h.id for h in hits], space
        assert any("跳槽" in w for w in withheld), space          # the outbound check gets it
        facts = await k.memory.about_person(A, thread_id=space)
        assert rid not in [h.id for h in facts], space
    # with the person who told me, one-to-one, it is usable; and my own thinking (no space) sees everything
    assert rid in [h.id for h in await k.memory.retrieve(cue, thread_id=B)]
    assert rid in [h.id for h in await k.memory.about_person(A, thread_id=B)]
    assert rid in [h.id for h in await k.memory.retrieve(cue)]


async def test_what_was_said_in_a_group_is_semi_public_within_it(k):
    A, B, C, G, H = people(k)
    rid = await k.memory.note(kind="semantic", content="小陈说他养了一只叫豆豆的柯基", source="told_by", told_by=C,
                              trust=Trust.PUBLIC, person_keys=[C], thread_id=H, observed_at=MONDAY - DAY)
    got = {s: [h.id for h in await k.memory.retrieve("小陈的柯基豆豆", thread_id=s)] for s in (H, A, C, B, G)}
    assert rid in got[H]                        # where it was said
    assert rid in got[A] and rid in got[C]      # one-to-one with someone who was there
    assert rid not in got[B]                    # B was not in that group
    assert rid not in got[G]                    # a space where B (not a member of H) would hear it


async def test_derived_records_keep_the_most_restrictive_audience(k):
    A, B, C, G, H = people(k)
    rid = await secret(k, A, B)
    pub = await k.memory.note(kind="semantic", content="阿明在大群里说过他喜欢深圳的天气", thread_id=G, person_keys=[A],
                              observed_at=MONDAY - DAY)
    # a nightly reflection drawing on both: inherits both origins -> only where both may be heard
    ref = await k.memory.note(kind="opinion", content="阿明可能想去深圳生活", source="reflected",
                              derived_from=[rid, pub], person_keys=[A])
    rec = await k.memory.get(ref)
    assert set(origins_of(rec)) == {B, G}
    for space in (A, G, C):
        assert ref not in [h.id for h in await k.memory.retrieve("阿明 深圳", thread_id=space)], space
    # B told me one part and was in G for the other: B may hear the whole
    assert ref in [h.id for h in await k.memory.retrieve("阿明 深圳", thread_id=B)]
    # a new version of the secret (reconciliation UPDATE) is as private as the old one
    v2 = await k.memory.note(kind="semantic", content="阿明下个月跳槽去深圳", source="told_by", told_by=C,
                             trust=Trust.CONTACT, person_keys=[A], thread_id=C, parent_version=rid, version=2)
    assert set(origins_of(await k.memory.get(v2))) == {C, B}
    # my own thought with no private source stays usable everywhere
    mine = await k.memory.note(kind="opinion", content="深圳的冬天应该挺暖和", source="reflected")
    for space in (A, B, G, H):
        assert mine in [h.id for h in await k.memory.retrieve("深圳的冬天", thread_id=space)], space


def test_scope_rule_unit():
    class W:
        def __init__(self):
            self.kinds = {"A": "person", "B": "person", "G": "group", "H": "group"}
            self.mem = {"G": ["A", "B"], "H": ["A"]}

        def entity(self, sid):
            return type("E", (), {"kind": self.kinds[sid]})() if sid in self.kinds else None

        def members_of(self, gid, active_within=None):
            return list(self.mem.get(gid, []))
    w = W()
    assert Scope(w, "A").covers(["A"]) and not Scope(w, "A").covers(["B"])     # one-to-one stays one-to-one
    assert not Scope(w, "G").covers(["A"])                                      # never into a shared space
    assert Scope(w, "A").covers(["G"]) and Scope(w, "A").covers(["H"])          # A heard both groups
    assert Scope(w, "B").covers(["G"]) and not Scope(w, "B").covers(["H"])
    assert Scope(w, "H").covers(["G"]) and not Scope(w, "G").covers(["H"])      # semi-public: members' subset
    assert Scope(w, "G").covers([]) and Scope(w, "A").covers([])                # nobody's private info
    assert not Scope(w, "A").covers(["legacy:123"])                             # unplaceable: only where it was
    assert Scope(w, "A").private_in(["A"]) and not Scope(w, "A").private_in(["G"])


async def test_buffer_keeps_lines_in_the_order_they_were_said(k):
    A, B, C, G, H = people(k)
    t = k.clock.now()
    mine = ChatLine(sender="我", person_key="", text="哈哈我也是", ts=t + 30, mine=True, msg_id="me1")
    theirs = ChatLine(sender="阿明", person_key=A, text="我今天好累", ts=t, msg_id="a1")
    await k.memory.observe_conversation(A, [mine], [A])          # my line reaches memory as I send it
    await k.memory.observe_conversation(A, [theirs, mine], [A])  # the exchange record arrives later: no double
    buf = k.memory._buffers[A]
    assert [l.msg_id for l in buf.lines] == ["a1", "me1"]
