"""Phase 4: store normalized embeddings in a persistent Chroma collection.

Contract (implementation.md phase 4)::

    chunks.jsonl + embeddings.npy + embedding_ids.json + manifest.json
        -> model guard -> prune stale IDs -> upsert (batch 100)
        -> data/04_vector_db/chroma (persistent)
        -> data/04_vector_db/collection_stats.json

The collection is a projection of the chunk file, never a second source of
truth.  Every row carries the same flat scalar metadata as its chunk, so a
retrieval filter (``scheme_slug``, ``fact_type``, ``doc_id``, ...) is a plain
Chroma ``where`` clause.  ``--rebuild`` is the only way to change the model:
vectors from two embedding models can never share a collection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from ..config import PROJECT_ROOT, ConfigError, Settings
from ..schemas import ChunkRecord, EmbeddingManifest
from .embed import (
    EMBEDDINGS_FILENAME,
    IDS_FILENAME,
    MANIFEST_FILENAME,
    Encoder,
    _encode,
    _load_encoder,
)

SUPPORTED_PROVIDER = "chroma"
SUPPORTED_DISTANCE = "cosine"
STATS_FILENAME = "collection_stats.json"
STATS_VERSION = 1
BATCH_SIZE = 100
NORM_TOLERANCE = 1e-3


class IndexBuildError(ConfigError):
    """The vector DB could not be built without breaking a gate."""


# ------------------------------------------------------------------ smoke queries

# implementation.md 4.2: each query must return its expected card in the top 3.
@dataclass(frozen=True)
class SmokeQuery:
    query: str
    doc_ids: tuple[str, ...]
    fact_type: str | None = None


SMOKE_QUERIES: tuple[SmokeQuery, ...] = (
    SmokeQuery("expense ratio hdfc large cap", ("groww_large_cap",), "expense_ratio"),
    SmokeQuery("exit load hdfc mid cap", ("groww_mid_cap",), "exit_load"),
    SmokeQuery("minimum sip elss", ("groww_elss",), "minimum_sip"),
    SmokeQuery("elss lock in", ("groww_elss", "sebi_elss")),
    SmokeQuery(
        "download capital gains statement",
        ("groww_help_tax_proof",),
        "statement_download",
    ),
)


@dataclass
class SmokeResult:
    query: str
    passed: bool
    top_ids: list[str] = field(default_factory=list)
    top_similarities: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "passed": self.passed,
            "top_ids": self.top_ids,
            "top_similarities": self.top_similarities,
        }


@dataclass
class IndexSummary:
    collection: str
    model_name: str
    revision: str
    dimension: int
    distance: str
    total_chunks: int = 0
    upserted: int = 0
    pruned: int = 0
    count: int = 0
    rebuilt: bool = False
    smoke: list[SmokeResult] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def smoke_passed(self) -> int:
        return sum(1 for result in self.smoke if result.passed)

    def describe(self) -> str:
        lines = [
            f"vector db: {SUPPORTED_PROVIDER} collection {self.collection} ({self.distance})",
            f"embedding model: {self.model_name} (revision {self.revision}, {self.dimension} dims)",
            f"chunks: {self.total_chunks}",
            (
                f"upserted: {self.upserted}, pruned: {self.pruned}, "
                f"count: {self.count}"
                + (" (rebuilt)" if self.rebuilt else "")
            ),
        ]
        if self.smoke:
            lines.append(f"smoke queries: {self.smoke_passed}/{len(self.smoke)} passed")
        lines.append("outputs:")
        for path in self.outputs:
            lines.append(f"  - {path}")
        if self.errors:
            lines.append("errors:")
            for error in self.errors:
                lines.append(f"  - {error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- run_index


def run_index(
    settings: Settings,
    *,
    rebuild: bool = False,
    encoder: Encoder | None = None,
    verify_smoke: bool = False,
) -> IndexSummary:
    """Project the embedding artifacts into Chroma.

    ``verify_smoke`` runs the five gate queries and loads the embedding model
    only when needed.  Tests inject ``encoder`` and usually leave smoke off.
    """
    vector_db = settings.vector_db
    if vector_db.provider != SUPPORTED_PROVIDER:
        raise ConfigError(
            f"unsupported vector DB provider {vector_db.provider!r}\n"
            f"fix: set vector_db.provider to {SUPPORTED_PROVIDER!r} in config/config.yaml"
        )
    if vector_db.distance != SUPPORTED_DISTANCE:
        raise ConfigError(
            f"vector_db.distance is {vector_db.distance!r}, expected {SUPPORTED_DISTANCE!r}\n"
            "fix: set vector_db.distance: cosine (similarity = 1 - distance)"
        )

    settings.ensure_data_dirs()
    chunks_path = settings.data_dir_path / "02_chunks" / "chunks.jsonl"
    embedding_dir = settings.data_dir_path / "03_embeddings"
    if not chunks_path.exists():
        raise ConfigError(
            f"no chunks at {chunks_path}\n"
            "fix: run python -m app.cli chunk before indexing"
        )

    chunks_bytes = chunks_path.read_bytes()
    if not chunks_bytes.strip():
        raise ConfigError(
            f"chunks file is empty: {chunks_path}\n"
            "fix: run python -m app.cli chunk to rebuild chunks"
        )
    chunks = _load_chunks(chunks_bytes, chunks_path)
    chunks_sha256 = _sha256_bytes(chunks_bytes)

    manifest = _load_manifest(embedding_dir / MANIFEST_FILENAME, embedding_dir)
    _guard_embedding_artifacts(settings, manifest, chunks, chunks_sha256)

    matrix = _load_vectors(settings, embedding_dir, len(chunks))
    _guard_ids(embedding_dir / IDS_FILENAME, chunks, len(chunks))
    _guard_content_hashes(chunks)

    persist = _persist_path(settings)
    client = _client(persist)
    collection = _collection(
        client,
        settings,
        rebuild=rebuild,
        chunks_sha256=chunks_sha256,
    )

    current_ids = [chunk.chunk_id for chunk in chunks]
    current_set = set(current_ids)
    existing_ids = set(collection.get(include=[])["ids"])

    summary = IndexSummary(
        collection=settings.vector_db.collection,
        model_name=settings.embedding.model_name,
        revision=settings.embedding.revision,
        dimension=settings.embedding.dimension,
        distance=settings.vector_db.distance,
        total_chunks=len(chunks),
        rebuilt=rebuild,
    )

    stale = sorted(existing_ids - current_set)
    for start in range(0, len(stale), BATCH_SIZE):
        batch = stale[start : start + BATCH_SIZE]
        collection.delete(ids=batch)
        summary.pruned += len(batch)

    for start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[start : start + BATCH_SIZE]
        rows = matrix[start : start + len(batch)]
        collection.upsert(
            ids=[chunk.chunk_id for chunk in batch],
            documents=[chunk.text for chunk in batch],
            embeddings=rows.tolist(),
            metadatas=[_metadata(chunk) for chunk in batch],
        )
        summary.upserted += len(batch)

    summary.count = collection.count()
    if summary.count != len(chunks):
        summary.errors.append(
            f"collection count is {summary.count}, expected {len(chunks)}"
        )

    if verify_smoke:
        active_encoder = encoder or _load_encoder(settings)
        summary.smoke = _run_smoke(collection, settings, active_encoder)
        for result in summary.smoke:
            if not result.passed:
                summary.errors.append(
                    f"smoke query failed: {result.query!r} "
                    f"(top-3: {result.top_ids})"
                )

    stats_path = settings.data_dir_path / "04_vector_db" / STATS_FILENAME
    stats_bytes = _render_stats(summary, chunks, chunks_sha256, settings)
    _write_bytes_if_changed(stats_path, stats_bytes)
    summary.outputs = [persist, stats_path]
    return summary


# --------------------------------------------------------------------- guards


def _load_chunks(chunks_bytes: bytes, path: Path) -> list[ChunkRecord]:
    records: list[ChunkRecord] = []
    seen: set[str] = set()
    for line_number, line in enumerate(chunks_bytes.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = ChunkRecord(**json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ConfigError(
                f"{path}:{line_number}: invalid chunk: {exc}\n"
                "fix: re-run python -m app.cli chunk"
            ) from exc
        if record.chunk_id in seen:
            raise ConfigError(
                f"{path}:{line_number}: duplicate chunk_id {record.chunk_id!r}\n"
                "fix: re-run python -m app.cli chunk"
            )
        seen.add(record.chunk_id)
        records.append(record)
    if not records:
        raise ConfigError(
            f"chunks file has no rows: {path}\n"
            "fix: run python -m app.cli chunk to build chunks"
        )
    return records


def _load_manifest(path: Path, directory: Path) -> EmbeddingManifest:
    if not path.exists():
        raise ConfigError(
            f"no embedding manifest at {path}\n"
            "fix: run python -m app.cli embed before indexing"
        )
    try:
        return EmbeddingManifest(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ConfigError(
            f"invalid embedding manifest at {path}: {exc}\n"
            "fix: run python -m app.cli embed --rebuild"
        ) from exc


def _guard_embedding_artifacts(
    settings: Settings,
    manifest: EmbeddingManifest,
    chunks: list[ChunkRecord],
    chunks_sha256: str,
) -> None:
    embedding = settings.embedding
    if manifest.embedding_model != embedding.model_name:
        raise ConfigError(
            f"embedding manifest model is {manifest.embedding_model!r}, "
            f"config expects {embedding.model_name!r}\n"
            "fix: run python -m app.cli embed --rebuild"
        )
    if manifest.revision != embedding.revision:
        raise ConfigError(
            f"embedding manifest revision is {manifest.revision!r}, "
            f"config expects {embedding.revision!r}\n"
            "fix: run python -m app.cli embed --rebuild"
        )
    if manifest.dimension != embedding.dimension:
        raise ConfigError(
            f"embedding manifest dimension is {manifest.dimension}, "
            f"config expects {embedding.dimension}\n"
            "fix: run python -m app.cli embed --rebuild"
        )
    if not manifest.normalized:
        raise ConfigError("embedding manifest is not normalized; re-run python -m app.cli embed")
    if manifest.count != len(chunks):
        raise ConfigError(
            f"embedding manifest has {manifest.count} rows but chunks.jsonl has "
            f"{len(chunks)}\nfix: run python -m app.cli embed"
        )
    if manifest.chunks_sha256 != chunks_sha256:
        raise ConfigError(
            "embeddings are stale relative to chunks.jsonl\n"
            "fix: run python -m app.cli embed before indexing"
        )


def _load_vectors(settings: Settings, directory: Path, count: int) -> np.ndarray:
    path = directory / EMBEDDINGS_FILENAME
    if not path.exists():
        raise ConfigError(
            f"no embedding matrix at {path}\n"
            "fix: run python -m app.cli embed before indexing"
        )
    try:
        matrix = np.load(path, allow_pickle=False)
    except Exception as exc:  # pragma: no cover - corrupted .npy is environment-specific
        raise ConfigError(
            f"could not read {path}: {exc}\n"
            "fix: run python -m app.cli embed --rebuild"
        ) from exc

    dimension = settings.embedding.dimension
    if matrix.shape != (count, dimension):
        raise ConfigError(
            f"embedding matrix shape is {matrix.shape}, expected ({count}, {dimension})\n"
            "fix: run python -m app.cli embed"
        )
    if matrix.dtype != np.float32:
        raise ConfigError(
            f"embedding matrix dtype is {matrix.dtype}, expected float32\n"
            "fix: run python -m app.cli embed --rebuild"
        )
    if not np.isfinite(matrix).all():
        raise ConfigError("embedding matrix contains non-finite values; re-run python -m app.cli embed")
    norms = np.linalg.norm(matrix, axis=1)
    error = float(np.max(np.abs(norms - 1.0))) if count else 0.0
    if error > NORM_TOLERANCE:
        raise ConfigError(
            f"embedding vectors are not unit length (max |norm-1|={error:.3e})\n"
            "fix: run python -m app.cli embed --rebuild"
        )
    return matrix.astype(np.float32, copy=False)


def _guard_ids(path: Path, chunks: list[ChunkRecord], count: int) -> None:
    if not path.exists():
        raise ConfigError(
            f"no embedding ids at {path}\n"
            "fix: run python -m app.cli embed before indexing"
        )
    try:
        ids = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"invalid embedding ids at {path}: {exc}\n"
            "fix: run python -m app.cli embed --rebuild"
        ) from exc
    expected = [chunk.chunk_id for chunk in chunks]
    if not isinstance(ids, list) or len(ids) != count:
        raise ConfigError(
            f"embedding ids must be a list of {count} chunk IDs\n"
            "fix: run python -m app.cli embed"
        )
    if ids != expected:
        raise ConfigError(
            "embedding_ids.json is not in chunks.jsonl row order\n"
            "fix: run python -m app.cli embed"
        )


def _guard_content_hashes(chunks: list[ChunkRecord]) -> None:
    for chunk in chunks:
        digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        if chunk.content_sha256 != digest:
            raise ConfigError(
                f"chunk {chunk.chunk_id} content_sha256 does not match its text\n"
                "fix: re-run python -m app.cli chunk && python -m app.cli embed"
            )


# -------------------------------------------------------------------- Chroma I/O


def _persist_path(settings: Settings) -> Path:
    path = Path(settings.vector_db.persist_directory)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _client(persist: Path):
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings
    except Exception as exc:  # pragma: no cover - depends on the local venv
        raise IndexBuildError(
            "chromadb is not installed\n"
            "fix: .\\.venv\\Scripts\\python.exe -m pip install chromadb\n"
            "     (on Python 3.15 see requirements.txt for the --no-deps install)"
        ) from exc

    persist.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(persist),
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def _collection_names(client) -> set[str]:
    names: set[str] = set()
    for entry in client.list_collections():
        names.add(entry if isinstance(entry, str) else entry.name)
    return names


def _collection(client, settings: Settings, *, rebuild: bool, chunks_sha256: str):
    vector_db = settings.vector_db
    embedding = settings.embedding
    name = vector_db.collection

    if rebuild and name in _collection_names(client):
        client.delete_collection(name)

    if name in _collection_names(client):
        collection = client.get_collection(name)
        metadata = collection.metadata or {}
        stored_model = metadata.get("embedding_model")
        stored_revision = metadata.get("embedding_revision")
        stored_dimension = metadata.get("dimension")
        stored_distance = metadata.get("distance", metadata.get("hnsw:space"))
        if (
            stored_model != embedding.model_name
            or stored_revision != embedding.revision
            or stored_dimension != embedding.dimension
            or stored_distance != vector_db.distance
        ):
            raise ConfigError(
                f"collection {name!r} was built with "
                f"{stored_model!r}/{stored_revision!r}/"
                f"{stored_dimension} dims/{stored_distance!r}, but config now says "
                f"{embedding.model_name!r}/{embedding.revision!r}/"
                f"{embedding.dimension} dims/{vector_db.distance!r}\n"
                "fix: python -m app.cli index --rebuild"
            )
        return collection

    metadata = {
        "hnsw:space": vector_db.distance,
        "distance": vector_db.distance,
        "embedding_model": embedding.model_name,
        "embedding_revision": embedding.revision,
        "dimension": embedding.dimension,
        "chunks_sha256": chunks_sha256,
    }
    configuration = {
        "hnsw": {
            "space": vector_db.distance,
            "ef_construction": vector_db.hnsw.construction_ef,
            "max_neighbors": vector_db.hnsw.m,
            "ef_search": vector_db.hnsw.search_ef,
        }
    }
    return client.create_collection(
        name=name,
        metadata=metadata,
        configuration=configuration,
    )


def _metadata(chunk: ChunkRecord) -> dict[str, str | int | float | bool]:
    """Flat scalars only; Chroma rejects ``None`` and nested values."""
    values: dict[str, object] = {
        "doc_id": chunk.doc_id,
        "source_url": chunk.source_url,
        "source_title": chunk.source_title,
        "source_owner": chunk.source_owner,
        "source_priority": chunk.source_priority,
        "scraped_at": chunk.scraped_at,
        "source_date": chunk.source_date,
        "scheme_slug": chunk.scheme_slug,
        "scheme_name": chunk.scheme_name,
        "amc": chunk.amc,
        "category": chunk.category,
        "plan": chunk.plan,
        "option": chunk.option,
        "fact_type": chunk.fact_type,
        "section": chunk.section,
        "chunk_type": chunk.chunk_type,
        "token_count": chunk.token_count,
        "content_sha256": chunk.content_sha256,
        "chunker_version": chunk.chunker_version,
    }
    return {
        key: value
        for key, value in values.items()
        if isinstance(value, (str, int, float, bool)) and value != ""
    }


def _run_smoke(collection, settings: Settings, encoder: Encoder) -> list[SmokeResult]:
    vectors = _encode(encoder, [spec.query for spec in SMOKE_QUERIES], settings)
    response = collection.query(
        query_embeddings=vectors.tolist(),
        n_results=3,
        include=["metadatas", "distances"],
    )
    all_ids = response.get("ids") or [[] for _ in SMOKE_QUERIES]
    all_metadata = response.get("metadatas") or [[] for _ in SMOKE_QUERIES]
    all_distances = response.get("distances") or [[] for _ in SMOKE_QUERIES]

    results: list[SmokeResult] = []
    for spec, ids, metadatas, distances in zip(
        SMOKE_QUERIES, all_ids, all_metadata, all_distances, strict=True
    ):
        passed = any(
            _smoke_hit(metadata or {}, spec) for metadata in metadatas
        )
        results.append(
            SmokeResult(
                query=spec.query,
                passed=passed,
                top_ids=list(ids),
                top_similarities=[
                    round(1.0 - float(distance), 6) for distance in distances
                ],
            )
        )
    return results


def _smoke_hit(metadata: dict, spec: SmokeQuery) -> bool:
    if metadata.get("doc_id") not in spec.doc_ids:
        return False
    if spec.fact_type is not None and metadata.get("fact_type") != spec.fact_type:
        return False
    return True


# ---------------------------------------------------------------------- artifacts


def _render_stats(
    summary: IndexSummary,
    chunks: list[ChunkRecord],
    chunks_sha256: str,
    settings: Settings,
) -> bytes:
    payload = {
        "schema_version": STATS_VERSION,
        "collection": summary.collection,
        "provider": SUPPORTED_PROVIDER,
        "persist_directory": str(_persist_path(settings)),
        "distance": summary.distance,
        "embedding_model": summary.model_name,
        "embedding_revision": summary.revision,
        "dimension": summary.dimension,
        "count": summary.count,
        "upserted": summary.upserted,
        "pruned": summary.pruned,
        "rebuilt": summary.rebuilt,
        "chunks_sha256": chunks_sha256,
        "sample_ids": [chunk.chunk_id for chunk in chunks[:5]],
        "smoke": {
            "passed": summary.smoke_passed,
            "total": len(summary.smoke),
            "queries": [result.as_dict() for result in summary.smoke],
        },
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    }
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def _write_bytes_if_changed(path: Path, data: bytes) -> bool:
    if path.exists():
        try:
            if path.read_bytes() == data:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return True


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
