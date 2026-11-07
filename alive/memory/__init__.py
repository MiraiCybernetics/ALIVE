"""Memory subsystem (docs/03): episodic / semantic / opinion / relational / self records with
write-time time normalisation, provenance rules (INV-11/14), hybrid retrieval, truth maintenance,
forgetting and sleep consolidation.

    from alive.memory import build
    mem = build(kernel)          # -> Memory (implements alive.interfaces.MemorySystem)
"""
from __future__ import annotations

from alive.memory import schema as _schema  # noqa: F401  (registers tables at import)
from alive.memory.records import Hit
from alive.memory.system import Memory

__all__ = ["build", "Memory", "Hit"]


def build(kernel) -> Memory:
    return Memory(kernel)
