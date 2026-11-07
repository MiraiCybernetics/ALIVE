"""Retrieval latency at 20k records (docs/03 §9: fast < 50 ms, standard < 500 ms; FTS+vector well under 200 ms).

Bounds asserted here are deliberately generous (CI machines vary); the measured numbers are printed
(run with -s to see them)."""
import os
import random
import statistics
import time

from tests.memory.memhelp import MONDAY, kernel

N = int(os.environ.get("ALIVE_PERF_N", "20000"))
SUBJ = ["阿杰", "小林", "老王", "Mia", "小周", "阿青", "大雄", "糖糖", "Leo", "七七"]
VERB = ["喜欢", "讨厌", "最近在学", "打算去", "聊到了", "想买", "推荐了", "吐槽了", "梦想是", "正在准备"]
OBJ = ["火锅", "吉他", "日本旅行", "考研", "猫咖", "新出的游戏", "马拉松", "摄影", "Python 编程", "面试", "搬家",
       "爬山", "咖啡", "做饭", "看展", "相亲", "健身", "钢琴", "科幻小说", "露营"]
TAIL = ["，说下周还要继续", "，感觉挺开心的", "，但有点累", "，还发了照片", "，问我要不要一起", "", "，好像很认真",
        "，说明天再说", "，结果下雨了", "，打算周末去"]


def make_items(n, rnd):
    items = []
    for i in range(n):
        s, v, o = rnd.choice(SUBJ), rnd.choice(VERB), rnd.choice(OBJ)
        items.append({"kind": rnd.choice(["episodic", "semantic", "semantic", "opinion"]),
                      "content": f"{s}{v}{o}{rnd.choice(TAIL)}（记录{i}）",
                      "importance": rnd.randint(1, 9), "entities": [o], "person_keys": [f"tg:{SUBJ.index(s)}"],
                      "source": "experienced", "observed_at": MONDAY - rnd.random() * 365 * 86400,
                      "refers_to": None})
    return items


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def test_retrieval_latency_20k(tmp_path):
    rnd = random.Random(7)
    k = await kernel(tmp_path)
    try:
        t0 = time.perf_counter()
        ids = await k.memory.note_many(make_items(N, rnd), emit=False)
        t_ins = time.perf_counter() - t0
        assert len(ids) == N and len(k.memory.index) == N
        cues = [f"{rnd.choice(SUBJ)}{rnd.choice(VERB)}{rnd.choice(OBJ)}" for _ in range(40)] + \
               ["面试", "猫", "上周聊了什么", "Python", "最近怎么样"]
        for c in cues[:5]:  # warm-up (reader connections, caches)
            await k.memory.retrieve(c, tier="fast", touch=False)
        lat: dict = {"fast": [], "standard": []}
        for tier in ("fast", "standard"):
            for c in cues:
                s = time.perf_counter()
                hits = await k.memory.retrieve(c, tier=tier, person_key="tg:1", touch=False)
                lat[tier].append((time.perf_counter() - s) * 1000)
                assert hits
        s = time.perf_counter()
        for c in cues[:20]:
            await k.memory.recall_cues([c])
        recall_ms = (time.perf_counter() - s) * 1000 / 20
        s = time.perf_counter()
        for i in range(50):
            await k.memory.note(kind="episodic", content=f"单条写入测试{i}：和小林去爬山", importance=3)
        write_ms = (time.perf_counter() - s) * 1000 / 50
        summary = {t: {"p50": round(statistics.median(v), 2), "p95": round(pct(v, 0.95), 2), "max": round(max(v), 2)}
                   for t, v in lat.items()}
        print(f"\n[mem perf] N={N} bulk insert {t_ins:.2f}s ({N / t_ins:.0f}/s); retrieval ms {summary}; "
              f"recall_cues {recall_ms:.2f} ms; single note() {write_ms:.2f} ms; "
              f"index {k.memory.index.nbytes() / 1e6:.0f} MB")
        scale = max(1.0, N / 20_000)
        assert summary["fast"]["p95"] < 150 * scale, summary
        assert summary["standard"]["p95"] < 400 * scale, summary
    finally:
        await k.stop()
