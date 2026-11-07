"""Governance and safety (docs/09): constitution, permission tiers + rule of two, budget/rate limits,
trust boundary, outbound guards, disclosure, crisis protocol, dependency monitoring, kill switch.

    kernel.gov = build(kernel)   # GovernanceEngine, implements alive.interfaces.Governance
"""
from __future__ import annotations

from alive.governance.engine import GovernanceEngine

__all__ = ["GovernanceEngine", "build"]


def build(kernel) -> GovernanceEngine:
    return GovernanceEngine(kernel)
