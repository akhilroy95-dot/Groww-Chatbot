"""Phase 3: embed chunks into normalized, cacheable float32 vectors.

Contract (implementation.md phase 3, architecture.md 5.5)::

    chunks.jsonl -> cache lookup (content_sha256) -> SentenceTransformer on a miss
                 -> L2 normalize -> embeddings.npy (N, 384) float32
                 -> embedding_ids.json + manifest.json

Everything is deterministic given the same chunks and model: rows follow the
``chunks.jsonl`` order, the cache is keyed by ``content_sha256``, and a second
run performs no model work at all.  The cache identity includes the model,
revision, dimension, device, batch size and normalization flag, so vectors from
two different models can never be mixed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np
from pydantic import ValidationError

from ..config import ConfigError, Settings
from ..schemas import ChunkRecord, EmbeddingManifest

SUPPORTED_PROVIDER = "sentence_transformers"
CACHE_FILENAME = "embedding_cache.jsonl"
EMBEDDINGS_FILENAME = "embeddings.npy"
IDS_FILENAME = "embedding_ids.json"
MANIFEST_FILENAME = "manifest.json"

# The gate tolerance from implementation.md 3.3.
NORM_TOLERANCE = 1e-3


class EmbedError(ConfigError):
    """Embedding could not be produced without breaking a gate."""


class Encoder(Protocol):
    """Minimal surface shared by SentenceTransformer and test doubles."""

    def encode(self, sentences: list[str], **kwargs: object) -> np.ndarray: ...


# ---------------------------------------------------------------------- summary


@dataclass
class EmbedSummary:
    model_name: str
    revision: str
    dimension: int
    normalized: bool
    total_chunks: int = 0
    embedded: int = 0
    cached: int = 0
    max_norm_error: float = 0.0
    artifacts_written: bool = False
    outputs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.total_chunks

    @property
    def ok(self) -> bool:
        return not self.errors

    def describe(self) -> str:
        lines = [
            f"embedding model: {self.model_name} (revision {self.revision})",
            f"chunks: {self.total_chunks}",
            f"{self.embedded} embedded, {self.cached} cached",
            (
                f"shape: ({self.total_chunks}, {self.dimension}) float32, "
                f"normalized={self.normalized}, max |norm-1|={self.max_norm_error:.2e}"
            ),
        ]
        if self.artifacts_written:
            lines.append("artifacts written:")
        else:
            lines.append("artifacts unchanged (cache hit):")
        for path in self.outputs:
            lines.append(f"  - {path}")
        if self.errors:
            lines.append("errors:")
            for error in self.errors:
                lines.append(f"  - {error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- run_embed


def run_embed(
    settings: Settings,
    *,
    force: bool = False,
    encoder: Encoder | None = None,
) -> EmbedSummary:
    """Embed every chunk, reusing cached vectors for unchanged content.

    ``force`` ignores the on-disk cache (``embed --rebuild``).  ``encoder`` is an
    injection point for tests; production code loads SentenceTransformer lazily
    only when at least one vector is missing, so a cache-hit run does not import
    torch at all.
    """
    embedding = settings.embedding
    if embedding.provider != SUPPORTED_PROVIDER:
        raise ConfigError(
            f"unsupported embedding provider {embedding.provider!r}\n"
            f"fix: set embedding.provider to {SUPPORTED_PROVIDER!r} in config/config.yaml"
        )
    if not embedding.normalize:
        raise ConfigError(
            "embedding.normalize is false\n"
            "fix: set embedding.normalize: true (cosine similarity depends on unit vectors)"
        )
    if embedding.dimension <= 0:
        raise ConfigError("embedding.dimension must be a positive integer")
    if embedding.batch_size <= 0:
        raise ConfigError("embedding.batch_size must be a positive integer")

    settings.ensure_data_dirs()
    chunks_path = settings.data_dir_path / "02_chunks" / "chunks.jsonl"
    out_dir = settings.data_dir_path / "03_embeddings"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not chunks_path.exists():
        raise ConfigError(
            f"no chunks at {chunks_path}\n"
            "fix: run python -m app.cli chunk before embedding"
        )
    chunks_bytes = chunks_path.read_bytes()
    if not chunks_bytes.strip():
        raise ConfigError(
            f"chunks file is empty: {chunks_path}\n"
            "fix: run python -m app.cli chunk to rebuild chunks"
        )

    chunks = _load_chunks(chunks_bytes, chunks_path)
    chunks_sha256 = _sha256_bytes(chunks_bytes)
    identity = _identity(settings)
    config_sha256 = _identity_sha256(settings)

    texts: dict[str, str] = {}
    ordered_hashes: list[str] = []
    for chunk in chunks:
        digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        if chunk.content_sha256 != digest:
            raise ConfigError(
                f"chunk {chunk.chunk_id} content_sha256 does not match its text\n"
                "fix: re-run python -m app.cli chunk"
            )
        if chunk.content_sha256 in texts and texts[chunk.content_sha256] != chunk.text:
            raise ConfigError(
                f"chunk {chunk.chunk_id} reuses content_sha256 for different text\n"
                "fix: re-run python -m app.cli chunk"
            )
        texts[chunk.content_sha256] = chunk.text
        ordered_hashes.append(chunk.content_sha256)

    cache_path = out_dir / CACHE_FILENAME
    cache = {} if force else _read_cache(cache_path, config_sha256, embedding.dimension)
    missing_hashes = [digest for digest in dict.fromkeys(ordered_hashes) if digest not in cache]
    missing_set = set(missing_hashes)
    embedded_rows = sum(1 for digest in ordered_hashes if digest in missing_set)

    summary = EmbedSummary(
        model_name=embedding.model_name,
        revision=embedding.revision,
        dimension=embedding.dimension,
        normalized=embedding.normalize,
        total_chunks=len(chunks),
        embedded=embedded_rows,
        cached=len(chunks) - embedded_rows,
    )

    if missing_hashes:
        active_encoder = encoder or _load_encoder(settings)
        vectors = _encode(active_encoder, [texts[digest] for digest in missing_hashes], settings)
        for digest, vector in zip(missing_hashes, vectors, strict=True):
            cache[digest] = vector

    matrix = np.vstack([cache[digest] for digest in ordered_hashes]).astype(np.float32, copy=False)
    summary.max_norm_error = _validate_matrix(matrix, settings)

    # Prune removed chunks so the cache cannot grow without bound, and write it
    # back only when it actually changed.
    current_hashes = set(ordered_hashes)
    cache = {digest: cache[digest] for digest in current_hashes}
    cache_bytes = _render_cache(cache, config_sha256, embedding.dimension)

    embeddings_bytes = _npy_bytes(matrix)
    embeddings_sha256 = _sha256_bytes(embeddings_bytes)
    ids_bytes = _render_ids([chunk.chunk_id for chunk in chunks])

    existing = _read_manifest(out_dir / MANIFEST_FILENAME)
    created_at = _created_at(existing, chunks_sha256, config_sha256, summary)
    manifest = EmbeddingManifest(
        embedding_model=identity["model_name"],
        revision=identity["revision"],
        dimension=identity["dimension"],
        dtype="float32",
        normalized=identity["normalize"],
        count=summary.total_chunks,
        chunks_sha256=chunks_sha256,
        embeddings_sha256=embeddings_sha256,
        config_sha256=config_sha256,
        created_at=created_at,
    )
    manifest_bytes = _render_manifest(manifest)

    embeddings_path = out_dir / EMBEDDINGS_FILENAME
    ids_path = out_dir / IDS_FILENAME
    manifest_path = out_dir / MANIFEST_FILENAME

    writes = [
        _write_bytes_if_changed(cache_path, cache_bytes),
        _write_bytes_if_changed(embeddings_path, embeddings_bytes),
        _write_bytes_if_changed(ids_path, ids_bytes),
        _write_bytes_if_changed(manifest_path, manifest_bytes),
    ]
    summary.artifacts_written = any(writes)
    summary.outputs = [embeddings_path, ids_path, manifest_path, cache_path]
    return summary


# --------------------------------------------------------------------- chunk I/O


def _load_chunks(chunks_bytes: bytes, path: Path) -> list[ChunkRecord]:
    records: list[ChunkRecord] = []
    seen: set[str] = set()
    for line_number, line in enumerate(chunks_bytes.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            record = ChunkRecord(**payload)
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


# -------------------------------------------------------------------- model/encode


def _load_encoder(settings: Settings) -> Encoder:
    embedding = settings.embedding
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:  # pragma: no cover - depends on the local venv
        raise EmbedError(
            "sentence-transformers is not installed\n"
            "fix: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        ) from exc

    try:  # pragma: no cover - downloads/loads model weights
        try:
            model = SentenceTransformer(
                embedding.model_name,
                revision=embedding.revision,
                device=embedding.device,
                local_files_only=True,
            )
        except Exception:
            # First run on a fresh machine: fall back to the Hub download.
            model = SentenceTransformer(
                embedding.model_name,
                revision=embedding.revision,
                device=embedding.device,
            )
    except Exception as exc:  # pragma: no cover - network/HF cache dependent
        raise EmbedError(
            f"could not load embedding model {embedding.model_name!r}: {exc}\n"
            "fix: check the model name, network access, or the Hugging Face cache"
        ) from exc

    if embedding.max_seq_length:
        model.max_seq_length = embedding.max_seq_length
    return model


def _encode(encoder: Encoder, texts: list[str], settings: Settings) -> np.ndarray:
    embedding = settings.embedding
    try:  # pragma: no cover - exercised through the injected test encoder
        raw = encoder.encode(
            texts,
            batch_size=embedding.batch_size,
            normalize_embeddings=embedding.normalize,
            show_progress_bar=embedding.show_progress,
            convert_to_numpy=True,
        )
    except TypeError:
        # Older/fake encoders may not accept convert_to_numpy.
        raw = encoder.encode(
            texts,
            batch_size=embedding.batch_size,
            normalize_embeddings=embedding.normalize,
            show_progress_bar=embedding.show_progress,
        )
    except Exception as exc:  # pragma: no cover - model/runtime dependent
        raise EmbedError(f"embedding model failed on {len(texts)} chunk(s): {exc}") from exc

    matrix = np.asarray(raw, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    expected = (len(texts), embedding.dimension)
    if matrix.shape != expected:
        raise EmbedError(
            f"embedding model returned shape {matrix.shape}, expected {expected}\n"
            "fix: set embedding.dimension to the model's output size"
        )
    if not np.isfinite(matrix).all():
        raise EmbedError("embedding model returned non-finite values")
    if embedding.normalize:
        matrix = _l2_normalize(matrix)
    return matrix.astype(np.float32, copy=False)


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0) or not np.isfinite(norms).all():
        raise EmbedError("embedding model returned a zero or non-finite vector")
    return (matrix / norms).astype(np.float32, copy=False)


def _validate_matrix(matrix: np.ndarray, settings: Settings) -> float:
    if matrix.dtype != np.float32:
        raise EmbedError(f"embedding dtype is {matrix.dtype}, expected float32")
    if matrix.ndim != 2 or matrix.shape[1] != settings.embedding.dimension:
        raise EmbedError(
            f"embedding shape is {matrix.shape}, expected (N, {settings.embedding.dimension})"
        )
    if not np.isfinite(matrix).all():
        raise EmbedError("embedding matrix contains non-finite values")
    if not settings.embedding.normalize:
        return 0.0
    norms = np.linalg.norm(matrix, axis=1)
    error = float(np.max(np.abs(norms - 1.0)))
    if error > NORM_TOLERANCE:
        raise EmbedError(
            f"embedding vectors are not unit length (max |norm-1|={error:.3e}, "
            f"tolerance {NORM_TOLERANCE})"
        )
    return error


# ------------------------------------------------------------------------ cache


def _identity(settings: Settings) -> dict[str, object]:
    embedding = settings.embedding
    return {
        "provider": embedding.provider,
        "model_name": embedding.model_name,
        "revision": embedding.revision,
        "dimension": embedding.dimension,
        "normalize": embedding.normalize,
        "max_seq_length": embedding.max_seq_length,
        "device": embedding.device,
        "batch_size": embedding.batch_size,
    }


def _identity_sha256(settings: Settings) -> str:
    payload = json.dumps(_identity(settings), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_cache(path: Path, config_sha256: str, dimension: int) -> dict[str, np.ndarray]:
    if not path.exists():
        return {}
    entries: dict[str, np.ndarray] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("model_key") != config_sha256:
            continue
        if row.get("dimension") != dimension:
            continue
        digest = row.get("content_sha256")
        if not isinstance(digest, str) or not digest:
            continue
        vector = _decode_vector(row.get("vector"), dimension)
        if vector is None:
            continue
        entries[digest] = vector
    return entries


def _decode_vector(value: object, dimension: int) -> np.ndarray | None:
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != dimension * 4:
        return None
    vector = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if not np.isfinite(vector).all():
        return None
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or abs(norm - 1.0) > NORM_TOLERANCE:
        return None
    return vector


def _render_cache(
    cache: dict[str, np.ndarray], config_sha256: str, dimension: int
) -> bytes:
    lines: list[str] = []
    for digest in sorted(cache):
        vector = np.asarray(cache[digest], dtype="<f4")
        row = {
            "content_sha256": digest,
            "dimension": dimension,
            "model_key": config_sha256,
            "vector": base64.b64encode(vector.tobytes()).decode("ascii"),
        }
        lines.append(json.dumps(row, sort_keys=True, ensure_ascii=False))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


# --------------------------------------------------------------------- artifacts


def _npy_bytes(matrix: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, matrix, allow_pickle=False)
    return buffer.getvalue()


def _render_ids(ids: Iterable[str]) -> bytes:
    return (json.dumps(list(ids), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _render_manifest(manifest: EmbeddingManifest) -> bytes:
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
    return (payload + "\n").encode("utf-8")


def _read_manifest(path: Path) -> EmbeddingManifest | None:
    if not path.exists():
        return None
    try:
        return EmbeddingManifest(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def _created_at(
    existing: EmbeddingManifest | None,
    chunks_sha256: str,
    config_sha256: str,
    summary: EmbedSummary,
) -> str:
    """Keep the original timestamp when the inputs and model are unchanged.

    Preserving it makes a cache-hit run byte-identical, which is what the
    inspector and the gate expect.  Any corpus or model change gets a new stamp.
    """
    if (
        existing is not None
        and summary.embedded == 0
        and existing.embedding_model == summary.model_name
        and existing.revision == summary.revision
        and existing.dimension == summary.dimension
        and existing.normalized == summary.normalized
        and existing.count == summary.total_chunks
        and existing.chunks_sha256 == chunks_sha256
        and existing.config_sha256 == config_sha256
    ):
        return existing.created_at
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_bytes_if_changed(path: Path, data: bytes) -> bool:
    """Atomic write that leaves an identical file untouched (zero-work rerun)."""
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
