"""Persistence across restarts: vectors reload into the index; a changed embedder triggers re-embedding."""
from alive.runtime.embed import HashingEmbedder
from alive.runtime.clock import ManualClock
from alive.testing import make_test_kernel
from tests.memory.memhelp import MONDAY, kernel


async def test_index_reloads_and_reembeds(tmp_path):
    k = await kernel(tmp_path)
    ids = await k.memory.note_many([{"kind": "semantic", "content": f"第{i}条：小林喜欢爬山和摄影"} for i in range(300)])
    await k.stop()
    k2 = await kernel(tmp_path)
    try:
        assert len(k2.memory.index) == 300 and k2.memory.index.get(ids[0]) is not None
        assert (await k2.memory.retrieve("小林 爬山 摄影", tier="fast"))
    finally:
        await k2.stop()
    # a different embedder (dim 256): stored vectors are stale -> re-embedded in the background
    k3 = await make_test_kernel(tmp_path, real=["memory"], clock=ManualClock(start=MONDAY), start=False)
    k3.embedder = HashingEmbedder(256)
    await k3.start()
    try:
        await k3.memory.drain()
        assert len(k3.memory.index) == 300 and k3.memory.index.dim == 256
        assert await k3.db.fetchval("SELECT COUNT(*) FROM mem_vec WHERE dim=256") == 300
        assert (await k3.memory.retrieve("小林 爬山"))
    finally:
        await k3.stop()
