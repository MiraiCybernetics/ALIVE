"""Social / interaction layer (docs/07): channels, inbound shaping, reply decision, embodiment, relationships, web.

    kernel.social = build(kernel)    # SocialManager, implements alive.interfaces.SocialSystem

Modules
    manager     SocialManager (orchestration, interface implementation, extra public API)
    actor       per-thread state and actor (debounce, backoff, in-flight handling)
    shaping     dedupe, debounce timing, steer/interrupt/queue classification
    decision    MaiBot logistic gate + ALIVE features, DynamicReplyGate, idle backoff, Webb turn ladder
    embodiment  segment splitting, delay model
    relations   persons, relationship dynamics, DAM aspect attitudes, opinions, pairing
    web         reader-isolated read_url, RSS/Atom feeds
    channels/   cli, telegram, onebot, sim adapters
"""
from __future__ import annotations

from alive.social import store as _store  # noqa: F401  (registers soc_ schema)
from alive.social.manager import SocialManager


def build(kernel) -> SocialManager:
    return SocialManager(kernel)


__all__ = ["SocialManager", "build"]
