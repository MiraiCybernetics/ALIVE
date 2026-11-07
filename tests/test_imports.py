"""Every module in the package must import (catches syntax errors in code paths no other test touches)."""
import importlib
import pkgutil

import alive


def test_all_modules_import():
    failures = []
    for m in pkgutil.walk_packages(alive.__path__, "alive."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:  # pragma: no cover - reported below
            failures.append((m.name, repr(e)))
    assert not failures, failures
