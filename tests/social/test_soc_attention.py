"""Attention (alive/social/air.py, ADR-0007): when I actually look at a space -- sampled, never a fixed delay."""
import random
import statistics

from alive.core.types import EV

from tests.social.soc_helpers import events, make_kernel


class _Item:
    def __init__(self, pk="sim:u1", text="你好", addressed=False):
        from types import SimpleNamespace
        self.msg = SimpleNamespace(person_key=pk, text=text)
        self.addressed = addressed


async def _kernel(tmp_path, seed=1):
    k = await make_kernel(tmp_path, social={"attention": {"enabled": True}})
    k.chance_rng = random.Random(seed)
    return k


def _med(xs):
    return statistics.median(xs)


async def test_attending_means_no_delay_and_lingering_after_speaking(tmp_path):
    k = await _kernel(tmp_path)
    try:
        air = k.social.air
        st = k.social._thread_by_id("sim:u1")
        now = k.clock.now()
        assert air.notice_delay(st, _Item(), now) > 0               # not looking at it yet
        d = air.linger(st, now, spoke=True, person_key="sim:u1")
        assert d > 0 and air.attending(st, now + 1)
        assert air.notice_delay(st, _Item(), now + 1) == 0.0          # I'm in the chat: seen at once
        spoke = [air.linger(st, now, spoke=True) for _ in range(300)]
        looked = [air.linger(st, now, spoke=False) for _ in range(300)]
        assert _med(spoke) > 2 * _med(looked)                         # after speaking I wait for the answer
    finally:
        await k.stop()


async def test_notice_delay_is_heavy_tailed_and_follows_the_tie(tmp_path):
    k = await _kernel(tmp_path, seed=2)
    try:
        air = k.social.air
        st = k.social._thread_by_id("sim:u1")
        now = k.clock.now()
        closeness = {"sim:near": 0.9, "sim:far": 0.1}
        air._closeness = lambda pk, now_: closeness.get(pk, 0.3)       # isolate the effect of the tie
        near = [air.notice_delay(st, _Item("sim:near"), now) for _ in range(1500)]
        far = [air.notice_delay(st, _Item("sim:far"), now) for _ in range(1500)]
        assert _med(near) < _med(far) / 5
        assert 60 < _med(near) < 600                                   # minutes, not seconds or hours
        q = sorted(near)
        assert q[int(0.95 * len(q))] > 8 * _med(near)                  # heavy right tail
        assert min(near) < 30                                          # sometimes the phone is in my hand
    finally:
        await k.stop()


async def test_ambient_lines_in_a_crowd_wait_for_my_next_look(tmp_path):
    k = await _kernel(tmp_path, seed=3)
    try:
        air = k.social.air
        g = k.social._thread_by_id("sim:g1")
        g.chat_type = "group"
        now = k.clock.now()
        air._closeness = lambda pk, now_: 0.5
        ambient = [air.notice_delay(g, _Item("sim:a", "今天好热"), now) for _ in range(1500)]
        to_me = [air.notice_delay(g, _Item("sim:a", "@小屿 你觉得呢", addressed=True), now) for _ in range(1500)]
        assert _med(to_me) < _med(ambient)
        # notifications compete: an urgent line to me pulls me in far more often than an ambient one
        rng_hits = lambda it: sum(air.catches(g, it, now) is not None for _ in range(2000))  # noqa: E731
        assert rng_hits(_Item("sim:a", "急！！在吗", addressed=True)) > 3 * rng_hits(_Item("sim:a", "今天好热"))
    finally:
        await k.stop()


async def test_mode_and_mirroring_shift_the_delay(tmp_path):
    k = await _kernel(tmp_path, seed=4)
    try:
        air = k.social.air
        st = k.social._thread_by_id("sim:u1")
        now = k.clock.now()
        air._closeness = lambda pk, now_: 0.5
        k.social.mind_mode = lambda: "active"
        active = [air.notice_delay(st, _Item(), now) for _ in range(1500)]
        k.social.mind_mode = lambda: "focus"
        focus = [air.notice_delay(st, _Item(), now) for _ in range(1500)]
        assert _med(focus) > 2 * _med(active)
        k.social.mind_mode = lambda: "idle"
        base = [air.notice_delay(st, _Item(), now) for _ in range(1500)]
        air._their_latency = lambda pk: 20.0                            # they answer me within seconds
        quick = [air.notice_delay(st, _Item(), now) for _ in range(1500)]
        air._their_latency = lambda pk: 6 * 3600.0                      # they take hours
        slow = [air.notice_delay(st, _Item(), now) for _ in range(1500)]
        assert _med(quick) < _med(base) < _med(slow)
    finally:
        await k.stop()


async def test_end_to_end_a_message_waits_until_i_look_then_quick_back_and_forth(tmp_path):
    k = await _kernel(tmp_path, seed=5)
    try:
        sim = k.social.adapter("sim")
        k.social.air._closeness = lambda pk, now_: 0.4
        await sim.inject("在吗，问你个事", sender="u1")
        await k.clock.advance(5)
        assert await events(k, EV.DECISION, "sim:u1") == []            # not looked at within seconds (typically)
        for _ in range(600):                                          # ... but within hours
            await k.clock.advance(60)
            if sim.outputs:
                break
        assert sim.outputs
        t_reply = sim.outputs[-1].ts
        await sim.inject("就是那个报名的事", sender="u1")                   # they answer while I'm still there
        await k.clock.advance(60)
        dec = await events(k, EV.DECISION, "sim:u1")
        assert len(dec) == 2 and dec[-1].ts - t_reply < 60              # seen at once: only the debounce
    finally:
        await k.stop()
