"""Workspace (docs/02 §2): softmax spotlight with a "nothing in particular" option, drawn lifetimes instead of fixed
TTLs (salience, focus and unanswered things keep an item on my mind longer)."""
import random

from alive.core.workspace import Workspace, WorkspaceItem, life_quantile
from alive.runtime.chance import MedianRandom


def _ws(seed=0):
    rng = random.Random(seed)
    ws = Workspace(capacity=7)
    ws.rng_fn = lambda: rng
    return ws, rng


def test_spotlight_frequencies_follow_salience():
    ws, rng = _ws(1)
    for key, s in (("a", 0.6), ("b", 0.45), ("c", 0.3)):
        ws.offer(WorkspaceItem(key=key, kind="thought", text=key, salience=s), 0.0)
    counts = {"a": 0, "b": 0, "c": 0, None: 0}
    for _ in range(3000):
        ws.spotlight = None                       # no inertia: independent draws
        f = ws.select_focus(0.35, temperature=0.08, rng=rng)
        counts[f.key if f else None] += 1
    assert counts["a"] > counts["b"] > counts["c"] > 0       # ordered by salience, nobody is excluded
    assert counts[None] > 0                                     # sometimes nothing in particular
    ws.spotlight = None
    w = ws.focus_weights(0.35, 0.08)
    assert abs(counts["a"] / 3000 - w["a"]) < 0.04             # frequencies match the softmax weights


def test_spotlight_inertia_and_temperature():
    ws, rng = _ws(2)
    for key, s in (("a", 0.5), ("b", 0.55)):
        ws.offer(WorkspaceItem(key=key, kind="thought", text=key, salience=s), 0.0)
    ws.spotlight = "a"
    w = ws.focus_weights(0.35, 0.05)
    assert w["a"] > w["b"]                       # what I am already on keeps an edge over a slightly stronger one
    ws.spotlight = None
    hot, cold = ws.focus_weights(0.35, 0.2), ws.focus_weights(0.35, 0.02)
    assert cold["b"] > hot["b"] and hot[None] > cold[None]    # high temperature (aroused, unfocused) = scattered


def test_weak_items_are_not_excluded_by_a_threshold():
    ws, rng = _ws(3)
    ws.offer(WorkspaceItem(key="weak", kind="space", text="x", salience=0.25), 0.0)
    picks = 0
    for _ in range(2000):
        ws.spotlight = None                      # independent draws (no inertia)
        picks += ws.select_focus(0.35, temperature=0.05, rng=rng) is not None
    assert 0 < picks < 400                       # rarely, but not never (the old hard threshold said never)


def test_lifetimes_are_drawn_once_around_the_old_ttl():
    lives = []
    for seed in range(400):
        ws, _ = _ws(seed)
        ws.offer(WorkspaceItem(key="t", kind="thread", text="t", salience=0.35, ttl=900), 0.0)
        it = ws.items["t"]
        lives.append(it.median_life() * it.life_q)
    lives.sort()
    med = lives[len(lives) // 2]
    assert 750 < med < 1080                      # the old 900 s TTL is the median
    assert lives[40] < 0.6 * med and lives[-40] > 1.5 * med   # but each item's lifetime differs


def test_median_rng_gives_median_lifetimes_and_items_do_expire():
    ws = Workspace(capacity=7)
    m = MedianRandom()
    ws.rng_fn = lambda: m
    ws.offer(WorkspaceItem(key="t", kind="thread", text="t", salience=0.35, ttl=900), 0.0)
    assert life_quantile(0.5) == 1.0 and ws.items["t"].life_q == 1.0
    for t in range(60, 880, 60):                 # many small checks never "roll" it away early ...
        ws.expire(float(t))
        assert "t" in ws.items
    ws.expire(960.0)                             # ... and it is gone after its (median) lifetime
    assert "t" not in ws.items


def test_salience_focus_and_unanswered_keep_items_longer():
    base = WorkspaceItem(key="x", kind="space", text="x", salience=0.3, ttl=2400)
    salient = WorkspaceItem(key="y", kind="space", text="y", salience=0.6, ttl=2400)
    unanswered = WorkspaceItem(key="z", kind="space", text="z", salience=0.3, ttl=2400, open=1.0)
    assert salient.median_life() > base.median_life()
    assert unanswered.median_life() > 1.8 * base.median_life()
    assert base.median_life(focus=1.3) > base.median_life(focus=0.8)
    assert 0.0 < base.drop_probability(2400.0) < unanswered.drop_probability(9000.0) < 1.0


def test_refresh_redraws_and_resets_the_life():
    ws, _ = _ws(5)
    ws.offer(WorkspaceItem(key="u", kind="urge", text="u", salience=0.5, ttl=1800), 0.0)
    q0 = ws.items["u"].life_q
    ws.offer(WorkspaceItem(key="u", kind="urge", text="u", salience=0.5, ttl=1800), 1000.0)
    it = ws.items["u"]
    assert it.entered_at == 1000.0 and it.life_q != q0
