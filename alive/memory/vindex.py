"""Vector index used by memory: the runtime ``VectorIndex`` plus id lookup and batched cosine.

The base class's ``allow`` filter is O(n) in Python, so memory never uses it: it over-fetches and
filters rows by status/kind after loading them (cheap, candidates are few).
"""
from __future__ import annotations

import asyncio
from typing import Optional, Sequence

import numpy as np

from alive.runtime.embed import VectorIndex


class MemVectorIndex(VectorIndex):
    def get(self, id_: str) -> Optional[np.ndarray]:
        with self._lock:
            p = self._pos.get(id_)
            return None if p is None else self._mat[p].copy()

    def cosine_many(self, q: np.ndarray, ids: Sequence[str]) -> dict:
        """Cosine of ``q`` against the given ids (missing ids are skipped)."""
        with self._lock:
            pos = [(i, self._pos[i]) for i in ids if i in self._pos]
            if not pos:
                return {}
            mat = self._mat[[p for _, p in pos]]
        sims = mat @ np.asarray(q, dtype=np.float32)
        return {i: float(s) for (i, _), s in zip(pos, sims)}

    async def search_async(self, q: np.ndarray, k: int = 20) -> list:
        """Search off the event loop once the index is large (numpy releases the GIL)."""
        if len(self) < 4000:
            return self.search_sync(q, k)
        return await asyncio.get_running_loop().run_in_executor(None, self.search_sync, q, k, None)

    def nbytes(self) -> int:
        return int(self._mat.nbytes)
