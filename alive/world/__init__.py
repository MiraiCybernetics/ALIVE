"""World model: entities and their handles (docs/22). Not conversation-centric: channels are transport,
conversations are not first-class; everything above the transport layer is keyed by entity ids."""
from alive.world.registry import SELF, Entity, World

__all__ = ["World", "Entity", "SELF"]
