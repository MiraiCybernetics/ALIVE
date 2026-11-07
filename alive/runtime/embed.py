"""Embeddings and an in-memory vector index (docs/03 §5).

DeepSeek has no embeddings endpoint, so the default is a zero-dependency *hashing* embedder
(character n-grams for CJK, words + char trigrams for Latin scripts, signed feature hashing).
It is weaker than neural embeddings but fast (~20k short texts/s), deterministic, and combined
with SQLite FTS5 trigram search it gives decent hybrid retrieval. Optional providers:
- ``fastembed`` (ONNX, CPU) e.g. BAAI/bge-small-zh-v1.5, if installed (pip install alive[embed]).
- any OpenAI-compatible /embeddings endpoint.

``VectorIndex`` keeps L2-normalised float32 vectors in a numpy matrix; search is a single
matrix-vector product run in a worker thread (numpy releases the GIL), so 100k x 512 searches
take a few ms and never block the event loop.
"""
from __future__ import annotations

import asyncio
import re
import threading
import unicodedata
import zlib
from typing import Iterable, Optional, Sequence

import numpy as np

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")
_WORD = re.compile(r"[A-Za-z0-9_]+|[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]+")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def tokens_for_hash(text: str) -> list:
    t = _norm(text)
    feats: list = []
    for m in _WORD.finditer(t):
        w = m.group(0)
        if _CJK.match(w):
            feats.extend(w)                                  # unigrams
            feats.extend(w[i:i + 2] for i in range(len(w) - 1))  # bigrams (weighted more below)
            feats.extend("§" + w[i:i + 3] for i in range(len(w) - 2))
        else:
            feats.append("w:" + w)
            if len(w) > 4:
                pw = f"<{w}>"
                feats.extend("t:" + pw[i:i + 3] for i in range(len(pw) - 2))
    return feats


class Embedder:
    dim: int = 0
    name: str = "embedder"

    async def embed(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    async def embed_one(self, text: str) -> np.ndarray:
        return (await self.embed([text]))[0]


class HashingEmbedder(Embedder):
    name = "hashing"

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        counts: dict = {}
        for f in tokens_for_hash(text):
            counts[f] = counts.get(f, 0) + 1
        for f, c in counts.items():
            h = zlib.crc32(f.encode("utf-8"))
            idx = h % self.dim
            sign = 1.0 if (h >> 31) & 1 else -1.0
            w = 1.0 + np.log(c)
            if len(f) == 2 and not f.startswith(("w:", "t:")):
                w *= 1.5  # CJK bigrams carry more meaning than unigrams
            v[idx] += sign * w
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    def embed_sync(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        if len(texts) <= 16:
            return self.embed_sync(texts)
        return await asyncio.get_running_loop().run_in_executor(None, self.embed_sync, list(texts))


class FastEmbedEmbedder(Embedder):  # pragma: no cover - optional dependency
    name = "fastembed"

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5"):
        from fastembed import TextEmbedding  # type: ignore
        self._m = TextEmbedding(model_name=model)
        self.dim = len(next(iter(self._m.embed(["probe"]))))
        self._lock = threading.Lock()

    def _run(self, texts):
        with self._lock:
            arr = np.array(list(self._m.embed(list(texts))), dtype=np.float32)
        n = np.linalg.norm(arr, axis=1, keepdims=True)
        n[n == 0] = 1
        return arr / n

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        return await asyncio.get_running_loop().run_in_executor(None, self._run, list(texts))


class OpenAIEmbedder(Embedder):  # pragma: no cover - needs a server
    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        self.base_url, self.api_key, self.model, self.dim = base_url.rstrip("/"), api_key, model, dim

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        import aiohttp
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": list(texts)},
                              headers={"Authorization": f"Bearer {self.api_key}"}) as r:
                d = await r.json()
        arr = np.array([x["embedding"] for x in d["data"]], dtype=np.float32)
        n = np.linalg.norm(arr, axis=1, keepdims=True)
        n[n == 0] = 1
        return arr / n


def make_embedder(cfg: Optional[dict] = None) -> Embedder:
    cfg = cfg or {}
    kind = cfg.get("kind", "hashing")
    if kind == "hashing":
        return HashingEmbedder(int(cfg.get("dim", 512)))
    if kind == "fastembed":
        return FastEmbedEmbedder(cfg.get("model", "BAAI/bge-small-zh-v1.5"))
    if kind == "openai":
        return OpenAIEmbedder(cfg["base_url"], cfg["api_key"], cfg["model"], int(cfg["dim"]))
    raise ValueError(f"unknown embedder {kind}")


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


class VectorIndex:
    """Thread-safe in-memory cosine index with amortised O(1) append and lazy compaction."""

    def __init__(self, dim: int, capacity: int = 1024):
        self.dim = dim
        self._mat = np.zeros((capacity, dim), dtype=np.float32)
        self._ids: list = []
        self._pos: dict = {}
        self._alive = np.zeros(capacity, dtype=bool)
        self._n = 0
        self._lock = threading.RLock()

    def __len__(self) -> int:
        return len(self._pos)

    def add(self, id_: str, vec: np.ndarray) -> None:
        with self._lock:
            if id_ in self._pos:
                self._mat[self._pos[id_]] = vec
                return
            if self._n >= self._mat.shape[0]:
                self._grow()
            self._mat[self._n] = vec
            self._alive[self._n] = True
            self._ids.append(id_)
            self._pos[id_] = self._n
            self._n += 1

    def add_many(self, ids: Iterable[str], vecs: np.ndarray) -> None:
        for i, v in zip(ids, vecs):
            self.add(i, v)

    def remove(self, id_: str) -> None:
        with self._lock:
            p = self._pos.pop(id_, None)
            if p is not None:
                self._alive[p] = False
            if self._n > 1024 and len(self._pos) < self._n // 2:
                self._compact()

    def _grow(self) -> None:
        cap = self._mat.shape[0] * 2
        m = np.zeros((cap, self.dim), dtype=np.float32)
        m[: self._n] = self._mat[: self._n]
        a = np.zeros(cap, dtype=bool)
        a[: self._n] = self._alive[: self._n]
        self._mat, self._alive = m, a

    def _compact(self) -> None:
        keep = [i for i in range(self._n) if self._alive[i]]
        ids = [self._ids[i] for i in keep]
        mat = self._mat[keep].copy()
        cap = max(1024, len(keep) * 2)
        self._mat = np.zeros((cap, self.dim), dtype=np.float32)
        self._mat[: len(keep)] = mat
        self._alive = np.zeros(cap, dtype=bool)
        self._alive[: len(keep)] = True
        self._ids = ids
        self._pos = {i: k for k, i in enumerate(ids)}
        self._n = len(keep)

    def search_sync(self, q: np.ndarray, k: int = 20, allow: Optional[set] = None) -> list:
        with self._lock:
            if self._n == 0:
                return []
            sims = self._mat[: self._n] @ np.asarray(q, dtype=np.float32)
            sims = np.where(self._alive[: self._n], sims, -2.0)
            if allow is not None:
                mask = np.array([self._ids[i] in allow for i in range(self._n)])
                sims = np.where(mask, sims, -2.0)
            kk = min(k, self._n)
            idx = np.argpartition(-sims, kk - 1)[:kk]
            idx = idx[np.argsort(-sims[idx])]
            return [(self._ids[i], float(sims[i])) for i in idx if sims[i] > -1.5]

    async def search(self, q: np.ndarray, k: int = 20, allow: Optional[set] = None) -> list:
        if self._n < 5000:
            return self.search_sync(q, k, allow)
        return await asyncio.get_running_loop().run_in_executor(None, self.search_sync, q, k, allow)
