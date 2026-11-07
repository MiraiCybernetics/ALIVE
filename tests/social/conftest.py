import os

import pytest

from tests.social.soc_helpers import make_kernel


def pytest_collection_modifyitems(config, items):
    """Live tests (real LLM) in tests/social run only with ALIVE_LIVE=1 or an explicit ``-m live``."""
    if os.environ.get("ALIVE_LIVE") == "1" or "live" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="live test: set ALIVE_LIVE=1 or pass -m live")
    for it in items:
        p = str(getattr(it, "fspath", "")).replace("\\", "/")
        if "live" in it.keywords and "/tests/social/" in p:
            it.add_marker(skip)


@pytest.fixture
async def kern(tmp_path):
    k = await make_kernel(tmp_path)
    yield k
    await k.stop()
