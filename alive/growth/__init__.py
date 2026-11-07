"""Growth subsystem (docs/06): evidence (S1–S4), L2 procedural artifacts (playbook, expressions, jargon)
with a gated, versioned lifecycle (INV-9), expression/jargon learners, skills registry, sleep pipeline
orchestration and the learning curriculum.

    from alive.growth import build
    growth = build(kernel)       # -> Growth (implements alive.interfaces.GrowthSystem)
"""
from __future__ import annotations

from alive.growth import schema as _schema  # noqa: F401  (registers tables at import)
from alive.growth.artifacts import Artifact
from alive.growth.sleep import STAGES, SleepContext
from alive.growth.system import Growth

__all__ = ["build", "Growth", "Artifact", "STAGES", "SleepContext"]


def build(kernel) -> Growth:
    return Growth(kernel)
