"""Helpers for tests/social (imported as a top-level module; file names are prefixed to stay unique)."""
from alive.config import deep_merge
from alive.testing import make_test_kernel

SIM = [{"kind": "sim", "name": "sim"}]


async def make_kernel(tmp_path, social=None, channels=None, cfg_overrides=None, **kw):
    ov = {"channels": channels if channels is not None else SIM, "social": {"seed": 7, **(social or {})}}
    if cfg_overrides:
        ov = deep_merge(ov, cfg_overrides)
    return await make_test_kernel(tmp_path, real=["social"], cfg_overrides=ov, **kw)


async def flush(k):
    await k.social.flush()
    await k.bus.drain(2.0)


async def events(k, etype, thread_id=None):
    await flush(k)
    return await k.bus.query(types=[etype], thread_id=thread_id, limit=10000)


async def decisions(k, thread_id=None):
    return [e.payload for e in await events(k, "core.decision", thread_id)]
