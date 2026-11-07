"""Self model (docs/08): identity files, self-state snapshot, outcomes digest, self notes, narrative updates."""
from __future__ import annotations

from alive.self_model.model import SelfModel


def build(kernel) -> SelfModel:
    return SelfModel(kernel)


__all__ = ["SelfModel", "build"]
