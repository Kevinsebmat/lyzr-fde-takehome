"""Persistence: sqlite for records, numpy cosine for vectors.

Deliberately not Chroma/pgvector/Pinecone. The corpora here are hundreds of
chunks, not millions; a numpy dot product is exact where an ANN index is
approximate, and it means `git clone && make smoke` works with no service to
stand up. The interface is narrow enough to swap for a real index if the
corpus ever justifies one — which is the honest production answer, and the
one the P2 scoping note makes to the customer.

Used by P2 (document chunks), P5 (long-term memory), P6 (approval queue),
and P8 (event queue + dead letters).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_LOCAL = threading.local()

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-']*")

#: Function words carry no retrieval signal and, unremoved, let a long question
#: match a long passage on grammar alone.
_STOPWORDS = frozenset(
    """a an and any are as at be been but by can could did do does for from get
    had has have how i if in into is it its may me my no not of on or our out over
    should so than that the their them then there these they this those to under up
    was we were what when where which who why will with would you your""".split()
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    collection  TEXT NOT NULL,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    embedding   BLOB,
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);

CREATE TABLE IF NOT EXISTS embedding_cache (
    key        TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    vector     BLOB NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE TABLE IF NOT EXISTS kv (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
    PRIMARY KEY (namespace, key)
);
"""


def db_path() -> Path:
    return Path(os.environ.get("AGENTCORE_DB", "agentcore.db")).expanduser()


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Thread-local connection with WAL on.

    WAL matters for P8: the webhook handler writes while the worker reads, and
    the default rollback journal serialises them into lock contention.
    """
    p = path or db_path()
    key = str(p)
    conns = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = _LOCAL.conns = {}
    conn = conns.get(key)
    if conn is None:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conns[key] = conn
    yield conn


def reset(path: Path | None = None) -> None:
    """Drop and recreate. Used by tests and `make demo-reset`."""
    p = path or db_path()
    conns = getattr(_LOCAL, "conns", {})
    if (conn := conns.pop(str(p), None)) is not None:
        conn.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)


# ---------- embeddings ----------


def _pack(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class Embedder:
    """Embeddings with an on-disk cache.

    Re-embedding an unchanged corpus on every run is the single easiest way to
    waste money in a RAG demo, so the cache is keyed on (model, text) and lives
    in the same sqlite file as the chunks.

    Offline, vectors come from hashed bag-of-words (see `_lexical`). Not
    semantic, but it ranks by shared vocabulary, which is enough for retrieval
    *ordering* to be correct in a demo and for thresholds to mean something.
    """

    def __init__(self, model: str = "voyage-3", dim: int = 512):
        self.model = model
        self.dim = dim
        self._client = None

    @property
    def backend(self) -> str:
        """Which vector space we're in. Part of the cache key, because mock and
        real vectors are not interchangeable and must never share an entry."""
        from . import mock

        live = not mock.is_mock_mode() and bool(os.environ.get("VOYAGE_API_KEY"))
        return self.model if live else "lexical-v1"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors: list[np.ndarray | None] = [None] * len(texts)
        missing: list[int] = []

        with connect() as conn:
            for i, text in enumerate(texts):
                key = self._key(text)
                row = conn.execute(
                    "SELECT vector FROM embedding_cache WHERE key = ?", (key,)
                ).fetchone()
                if row is not None:
                    vectors[i] = _unpack(row["vector"])
                else:
                    missing.append(i)

            if missing:
                fresh = self._embed_uncached([texts[i] for i in missing])
                for slot, vec in zip(missing, fresh, strict=True):
                    vectors[slot] = vec
                    conn.execute(
                        "INSERT OR REPLACE INTO embedding_cache (key, model, vector) "
                        "VALUES (?, ?, ?)",
                        (self._key(texts[slot]), self.model, _pack(vec)),
                    )

        return np.vstack([v for v in vectors if v is not None])

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.backend}\x00{text}".encode()).hexdigest()

    def _embed_uncached(self, texts: list[str]) -> list[np.ndarray]:
        if self.backend != self.model:
            return [self._lexical(t) for t in texts]
        if self._client is None:
            import voyageai

            self._client = voyageai.Client()
        result = self._client.embed(texts, model=self.model, input_type="document")
        return [np.asarray(e, dtype=np.float32) for e in result.embeddings]

    def _lexical(self, text: str) -> np.ndarray:
        """Hashed bag-of-words: cosine here is vocabulary overlap.

        The earlier version summed a random gaussian per token, which encodes
        token identity but spreads every token across all dimensions with
        random signs — two passages sharing a few words got a small, noisy dot
        product, and offline retrieval ranked the wrong chunk first. Feature
        hashing puts each token in one bucket, so similarity tracks shared
        vocabulary directly and the demo retrieves what a reader would expect.

        Sublinear term weighting and stopword removal keep "how do I" from
        outweighing "refund window".
        """
        counts: dict[int, float] = {}
        for token in _WORD_RE.findall(text.lower()):
            if token in _STOPWORDS or len(token) < 2:
                continue
            bucket = (
                int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
                % self.dim
            )
            counts[bucket] = counts.get(bucket, 0.0) + 1.0

        vec = np.zeros(self.dim, dtype=np.float32)
        for bucket, count in counts.items():
            vec[bucket] = 1.0 + np.log(count)  # sublinear: the 5th mention adds little

        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec


# ---------- vector search ----------


@dataclass
class Hit:
    id: str
    text: str
    score: float
    title: str | None = None
    source: str | None = None
    metadata: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


def add_documents(collection: str, docs: Sequence[dict], embedder: Embedder) -> int:
    """Insert chunks with embeddings. `docs` need id/text, optionally
    title/source/metadata."""
    texts = [d["text"] for d in docs]
    vectors = embedder.embed(texts)
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO documents "
            "(id, collection, source, title, text, metadata, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    d["id"],
                    collection,
                    d.get("source"),
                    d.get("title"),
                    d["text"],
                    json.dumps(d.get("metadata", {})),
                    _pack(vectors[i]),
                )
                for i, d in enumerate(docs)
            ],
        )
    return len(docs)


def search(
    collection: str, query: str, embedder: Embedder, k: int = 5, min_score: float = 0.0
) -> list[Hit]:
    """Exact cosine search over a collection.

    Returns fewer than `k` when nothing clears `min_score` — an empty result is
    a valid, meaningful answer here, and P2 depends on being able to see it.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, source, text, metadata, embedding FROM documents "
            "WHERE collection = ? AND embedding IS NOT NULL",
            (collection,),
        ).fetchall()
    if not rows:
        return []

    matrix = np.vstack([_unpack(r["embedding"]) for r in rows])
    q = embedder.embed([query])[0]

    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(q)
    norms[norms == 0] = 1e-9
    scores = (matrix @ q) / norms

    order = np.argsort(-scores)[:k]
    return [
        Hit(
            id=rows[i]["id"],
            text=rows[i]["text"],
            score=float(scores[i]),
            title=rows[i]["title"],
            source=rows[i]["source"],
            metadata=json.loads(rows[i]["metadata"]),
        )
        for i in order
        if scores[i] >= min_score
    ]


def count(collection: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE collection = ?", (collection,)
        ).fetchone()["n"]


# ---------- key/value ----------


def kv_set(namespace: str, key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (namespace, key, value, updated_at) "
            "VALUES (?, ?, ?, unixepoch('subsec'))",
            (namespace, key, json.dumps(value, default=str)),
        )


def kv_get(namespace: str, key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM kv WHERE namespace = ? AND key = ?", (namespace, key)
        ).fetchone()
    return json.loads(row["value"]) if row else default


def kv_list(namespace: str) -> list[tuple[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM kv WHERE namespace = ? ORDER BY updated_at DESC",
            (namespace,),
        ).fetchall()
    return [(r["key"], json.loads(r["value"])) for r in rows]
