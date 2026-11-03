"""Read-only access to pipeline artifacts for the Pipeline Inspector.

The inspector is evidence, not a second implementation: every helper here reads
a file (or the Chroma collection) that a pipeline stage already wrote.  Nothing
in this module imports the answer path and nothing recomputes an embedding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from ..config import Settings
from ..schemas import AnswerTrace, ChunkRecord, RetrievalTrace, SourceRegistry
from ..sources import load_registry


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return rows


@dataclass(frozen=True)
class ArtifactPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "01_raw" / "manifest.json"

    @property
    def raw_dir(self) -> Path:
        return self.root / "01_raw"

    @property
    def chunks(self) -> Path:
        return self.root / "02_chunks" / "chunks.jsonl"

    @property
    def embeddings_manifest(self) -> Path:
        return self.root / "03_embeddings" / "manifest.json"

    @property
    def embeddings(self) -> Path:
        return self.root / "03_embeddings" / "embeddings.npy"

    @property
    def embedding_ids(self) -> Path:
        return self.root / "03_embeddings" / "embedding_ids.json"

    @property
    def collection_stats(self) -> Path:
        return self.root / "04_vector_db" / "collection_stats.json"

    @property
    def retrieval_dir(self) -> Path:
        return self.root / "05_retrieval"

    @property
    def answers_dir(self) -> Path:
        return self.root / "06_answers"

    @property
    def logs(self) -> Path:
        return self.root / "logs" / "app.jsonl"


class ArtifactReader:
    """Typed, cached views over the artifacts written by phases 1-6."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.paths = ArtifactPaths(settings.data_dir_path)
        self._registry: SourceRegistry | None = None
        self._chunks: list[ChunkRecord] | None = None

    # -- registry / sources ------------------------------------------------

    @property
    def registry(self) -> SourceRegistry:
        if self._registry is None:
            self._registry = load_registry(self.settings.sources_path)
        return self._registry

    def manifest(self) -> dict[str, Any]:
        payload = _read_json(self.paths.manifest)
        return payload if isinstance(payload, dict) else {}

    def raw_sources(self) -> list[dict[str, Any]]:
        entries = self.manifest().get("sources")
        return entries if isinstance(entries, list) else []

    def raw_source(self, source_id: str) -> dict[str, Any] | None:
        return _read_json(self.paths.raw_dir / f"{source_id}.json")

    def raw_text(self, source_id: str, *, limit: int = 4000) -> str:
        path = self.paths.raw_dir / f"{source_id}.txt"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return ""

    # -- chunks ------------------------------------------------------------

    def chunks(self) -> list[ChunkRecord]:
        if self._chunks is None:
            records: list[ChunkRecord] = []
            for row in _read_jsonl(self.paths.chunks):
                try:
                    records.append(ChunkRecord(**row))
                except Exception:
                    continue
            self._chunks = records
        return self._chunks

    def chunk(self, chunk_id: str) -> ChunkRecord | None:
        return next((chunk for chunk in self.chunks() if chunk.chunk_id == chunk_id), None)

    # -- embeddings --------------------------------------------------------

    def embedding_manifest(self) -> dict[str, Any]:
        payload = _read_json(self.paths.embeddings_manifest)
        return payload if isinstance(payload, dict) else {}

    def embedding_ids(self) -> list[str]:
        payload = _read_json(self.paths.embedding_ids)
        return [str(value) for value in payload] if isinstance(payload, list) else []

    def embedding_row(self, chunk_id: str) -> tuple[np.ndarray | None, int | None]:
        ids = self.embedding_ids()
        if chunk_id not in ids or not self.paths.embeddings.exists():
            return None, None
        try:
            matrix = np.load(self.paths.embeddings, allow_pickle=False)
        except (OSError, ValueError):
            return None, None
        index = ids.index(chunk_id)
        if matrix.ndim != 2 or index >= matrix.shape[0]:
            return None, None
        return matrix[index].astype(np.float32, copy=False), index

    # -- vector DB ---------------------------------------------------------

    def collection_stats(self) -> dict[str, Any]:
        payload = _read_json(self.paths.collection_stats)
        return payload if isinstance(payload, dict) else {}

    def collection_sample(self, limit: int = 10) -> dict[str, Any]:
        """Read the collection without embedding a query or mutating it."""
        try:
            from ..pipeline.index import _client, _collection_names, _persist_path

            client = _client(_persist_path(self.settings), read_only=True)
            name = self.settings.vector_db.collection
            if name not in _collection_names(client):
                return {"error": f"collection {name!r} does not exist"}
            collection = client.get_collection(name)
            count = collection.count()
            sample = collection.get(limit=limit, include=["metadatas", "documents"])
            return {
                "name": name,
                "count": count,
                "metadata": dict(collection.metadata or {}),
                "ids": sample.get("ids") or [],
                "documents": sample.get("documents") or [],
                "metadatas": sample.get("metadatas") or [],
            }
        except Exception as exc:  # pragma: no cover - depends on local Chroma state
            return {"error": str(exc)}

    # -- request artifacts -------------------------------------------------

    def request_ids(self) -> list[str]:
        if not self.paths.answers_dir.exists():
            return []
        files = sorted(
            self.paths.answers_dir.glob("req_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [path.stem for path in files]

    def answer_trace(self, request_id: str) -> AnswerTrace | None:
        if not request_id:
            return None
        payload = _read_json(self.paths.answers_dir / f"{request_id}.json")
        if not isinstance(payload, dict):
            return None
        try:
            return AnswerTrace(**payload)
        except Exception:
            return None

    def retrieval_trace(self, request_id: str) -> RetrievalTrace | None:
        if not request_id:
            return None
        payload = _read_json(self.paths.retrieval_dir / f"{request_id}.json")
        if not isinstance(payload, dict):
            return None
        try:
            return RetrievalTrace(**payload)
        except Exception:
            return None

    def latest_request_id(self) -> str | None:
        ids = self.request_ids()
        return ids[0] if ids else None

    def answer_dates(self) -> list[str]:
        dates: list[str] = []
        for request_id in self.request_ids()[:200]:
            trace = self.answer_trace(request_id)
            if trace is not None:
                dates.append(trace.final.last_updated_from_sources)
        return dates

    def log_events(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = _read_jsonl(self.paths.logs)
        return list(reversed(rows))[:limit]

    # -- derived -----------------------------------------------------------

    def chunk_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for chunk in self.chunks():
            counts[chunk.doc_id] = counts.get(chunk.doc_id, 0) + 1
        return counts

    def fact_types(self) -> list[str]:
        return sorted({chunk.fact_type for chunk in self.chunks()})

    def doc_ids(self) -> list[str]:
        return sorted({chunk.doc_id for chunk in self.chunks()})

    def source_entries(self) -> Iterable:
        return self.registry.sources
