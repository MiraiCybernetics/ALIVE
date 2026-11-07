"""Throughput: 50 active threads at 20 msgs/s inbound without backlog (sim adapter + FakeMind).

Virtual time is driven by ManualClock; the numbers printed are real wall-clock costs of the whole pipeline
(dedupe, persistence via the DB writer thread, actors, decision, embodied send with delays, side effects).
"""
import time

from tests.social.soc_helpers import decisions, make_kernel


async def test_fifty_threads_twenty_msgs_per_second(tmp_path):
    k = await make_kernel(tmp_path, social={"talk_value": 1.0})
    try:
        sim = k.social.adapter("sim")
        n_threads, rate, seconds = 50, 20, 60
        dt = 1.0 / rate
        threads = [(f"u{i}", "private") if i < 35 else (f"g{i}", "group") for i in range(n_threads)]
        t0 = time.perf_counter()
        max_inbox = 0
        sent = 0
        for step in range(rate * seconds):
            chat, ctype = threads[step % n_threads]
            if ctype == "private":
                await sim.inject(f"消息{step}：今天怎么样？", sender=chat)
            else:
                await sim.inject(f"群消息{step} 有人吗", sender=f"m{step % 7}", chat_id=chat, chat_type="group",
                                 mentions_me=(step % 9 == 0))
            sent += 1
            await k.clock.advance(dt)
            if step % 50 == 0:
                max_inbox = max([max_inbox] + [len(a.inbox) for a in k.social._actors.values()])
        feed_wall = time.perf_counter() - t0
        await k.clock.advance(120)                          # let the last batches finish
        await k.social.flush()
        total_wall = time.perf_counter() - t0
        backlog = sum(len(a.inbox) for a in k.social._actors.values())
        busy = sum(1 for a in k.social._actors.values() if a.busy)
        dec = await decisions(k)
        decided_msgs = sum(d["n"] for d in dec)
        replies = len(sim.outputs)
        print(f"\nperf: {sent} msgs over {seconds}s virtual across {n_threads} threads; "
              f"wall {feed_wall:.2f}s feeding ({sent / feed_wall:.0f} msg/s real), {total_wall:.2f}s total; "
              f"decisions={len(dec)} covering {decided_msgs} msgs; replies={replies}; max_inbox={max_inbox}; "
              f"backlog_end={backlog}; busy_end={busy}; write_failed={k.social.writer.failed}")
        assert backlog == 0 and busy == 0
        assert decided_msgs == sent                         # every message reached a decision
        assert max_inbox <= 10                              # no thread accumulates a queue
        assert sent / feed_wall > rate                      # faster than real time -> keeps up with 20 msg/s
        assert k.social.writer.failed == 0
        assert await k.db.fetchval("SELECT COUNT(*) FROM soc_messages WHERE mine=0") == sent
    finally:
        await k.stop()


async def test_burst_submit_throughput(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        for i in range(50):                                  # warm the person/thread caches
            await sim.inject("hi", sender=f"u{i}")
        n = 2000
        t0 = time.perf_counter()
        for i in range(n):
            await sim.inject(f"burst {i}", sender=f"u{i % 50}")
        submit = time.perf_counter() - t0
        await k.clock.advance(600)
        await k.social.flush()
        print(f"\nburst: {n} submit_incoming in {submit:.3f}s = {n / submit:.0f} msg/s real")
        assert n / submit > 500
        assert sum(len(a.inbox) for a in k.social._actors.values()) == 0
    finally:
        await k.stop()
