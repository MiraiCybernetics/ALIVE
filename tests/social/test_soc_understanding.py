"""Level-0 understanding cues (docs/17): person baselines, deviation cues, group context."""
from alive.core.types import ChatLine
from alive.social.understanding import PersonBaseline, deviation_cues, group_context, group_cues

from tests.social.soc_helpers import decisions, make_kernel


def _baseline(n=20, text="哈哈今天下班路上看到一只超可爱的猫"):
    b = PersonBaseline()
    for i in range(n):
        b.observe(text, float(i))
        b.observe_burst(1)
        b.observe_latency(300.0)
    return b


def test_baseline_summary():
    b = _baseline()
    s = b.summary()
    assert s["n_messages"] == 20 and s["reliable"] and s["len_median"] == len("哈哈今天下班路上看到一只超可爱的猫")
    assert s["laugh_rate"] > 0.9 and s["punct"]["period_end"] < 0.01 and s["reply_latency_median"] == 300
    varied = PersonBaseline()
    for t in ("哈哈好呀", "绝了哈哈", "明天见哈哈", "哈哈哈真的假的", "我也想去", "哈哈对", "下雨了", "哈哈哈哈"):
        varied.observe(t, 0.0)
    assert varied.summary()["catchphrases"][0] == "哈哈"
    b2 = PersonBaseline(b.to_dict())             # round-trips through JSON-able dict
    assert b2.summary()["len_median"] == s["len_median"]


def test_deviation_cues():
    b = _baseline()
    cues = deviation_cues(b, ["嗯。"], name="阿明", latency=3600 * 2, hour_share=0.01, gap_since_contact=10 * 86400)
    text = "\n".join(cues)
    for part in ("短很多", "句号", "隔了很久", "很少在线", "很久没联系"):
        assert part in text, part
    assert "弱参考" not in text
    cues = deviation_cues(b, ["今天好累", "不想说话", "随便吧", "算了"], name="阿明")
    assert any("连发了 4 条" in c for c in cues) and any("哈哈" in c for c in cues)
    assert deviation_cues(b, ["哈哈今天下班路上看到一只超可爱的狗"], name="阿明") == []
    weak = deviation_cues(_baseline(6), ["嗯"], name="小红")
    assert weak and "弱参考" in weak[-1]


def test_group_context_frame_and_share():
    now = 10_000.0
    lines = [ChatLine("A", "a", "哈哈哈笑死", now - 100), ChatLine("B", "b", "哈哈哈哈", now - 90),
             ChatLine("我", "", "哈哈", now - 80, mine=True), ChatLine("我", "", "太好笑了", now - 70, mine=True),
             ChatLine("我", "", "笑死我了", now - 60, mine=True), ChatLine("A", "a", "233", now - 50),
             ChatLine("我", "", "哈哈哈", now - 40, mine=True)]
    ctx = group_context(lines, now, names={"a": "阿明", "b": "小红"})
    assert ctx["frame"] == "joking" and ctx["active_members"] == ["阿明", "小红"]
    assert ctx["over_share"] and ctx["fair_share"] == round(1 / 3, 3)
    assert any("说得比别人多" in c for c in group_cues(ctx))         # no rng: the typical case (4/7 vs 1/3: noticed)
    assert not any("收着点" in c for c in group_cues(ctx))            # a fact, not an order
    # noticing it is a draw that grows with how far over my share I am (and with sensitivity)
    import random
    from alive.social.understanding import group_cues as gc
    rng = random.Random(3)
    hi = sum(any("说得比别人多" in c for c in gc(ctx, rng=rng, sensitivity=1.5)) for _ in range(400))
    lo = sum(any("说得比别人多" in c for c in gc(ctx, rng=rng, sensitivity=0.3)) for _ in range(400))
    mild = dict(ctx, my_share=0.4)                                    # just over 1x fair share
    mild_n = sum(any("说得比别人多" in c for c in gc(mild, rng=rng, sensitivity=1.0)) for _ in range(400))
    assert hi > lo > 0 and mild_n < lo
    assert group_context(lines, now + 3600, names={})["frame"] == "quiet"


async def test_cues_flow_into_decisions(tmp_path):
    k = await make_kernel(tmp_path)
    try:
        sim = k.social.adapter("sim")
        for i in range(16):
            await sim.inject(f"哈哈我跟你说今天又发生了一件好玩的事{i}", sender="u1", sender_name="阿明")
            await k.clock.advance(40)
        await sim.inject("嗯。", sender="u1")
        await k.clock.advance(40)
        d = (await decisions(k, "sim:u1"))[-1]
        assert any("短很多" in c for c in d["cues"]) and any("句号" in c for c in d["cues"])
        assert k.social.reading_cues("sim:u1") == d["cues"]
        assert k.social.person_baseline("sim:u1")["n_messages"] == 17
        # group context is available for groups
        await sim.inject("哈哈哈", sender="a", chat_id="g1", chat_type="group")
        await sim.inject("@小屿 笑死", sender="b", chat_id="g1", chat_type="group")
        await k.clock.advance(30)
        assert k.social.group_context("sim:g1")["active_members"]
        dg = (await decisions(k, "sim:g1"))[-1]
        assert any(c.startswith("群里的气氛") for c in dg["cues"])
    finally:
        await k.stop()
    # baselines survive restarts (persisted in the person record)
    k = await make_kernel(tmp_path)
    try:
        await k.social.relations.get("sim:u1")
        assert k.social.person_baseline("sim:u1")["n_messages"] == 17
    finally:
        await k.stop()
