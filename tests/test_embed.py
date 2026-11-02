"""Phase 3 gate: normalized vectors, deterministic artifacts, and a real cache."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from app.config import AppSettings, ConfigError, EmbeddingSettings, Settings
from app.pipeline.embed import (
    CACHE_FILENAME,
    EMBEDDINGS_FILENAME,
    IDS_FILENAME,
    MANIFEST_FILENAME,
    EmbedError,
    run_embed,
)
from app.schemas import ChunkRecord

TEXTS = [
    (
        "Scheme: HDFC Large Cap Fund Direct Growth\n"
        "Fact: Expense ratio\nValue: 1.02%"
    ),
    (
        "Scheme: HDFC Large Cap Fund Direct Growth\n"
        "Fact: Exit load\nValue: 1% if redeemed within 1 year"
    ),
    (
        "Scheme: HDFC Large Cap Fund Direct Growth\n"
        "Fact: Minimum SIP\nValue: ₹100"
    ),
]


# ----------------------------------------------------------------------- helpers


class FakeEncoder:
    """Deterministic, unnormalized vectors so the pipeline must normalize them."""

    def __init__(self, dimension: int = 384, scale: float = 20.0) -> None:
        self.dimension = dimension
        self.scale = scale
        self.batches: list[list[str]] = []

    @property
    def calls(self) -> int:
        return len(self.batches)

    @property
    def texts(self) -> list[str]:
        return [text for batch in self.batches for text in batch]

    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        self.batches.append(list(texts))
        rows: list[np.ndarray] = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            positions = np.arange(1, self.dimension + 1, dtype=np.float64)
            vector = np.sin(positions * (seed % 89 + 1) / 97.0) + 0.5
            rows.append((vector * self.scale).astype(np.float32))
        return np.vstack(rows)


class ExplodingEncoder:
    def encode(self, sentences: list[str], **kwargs: object) -> np.ndarray:
        raise AssertionError("encoder must not be called on a full cache hit")


def settings_for(tmp_path: Path, **embedding: object) -> Settings:
    return Settings(
        app=AppSettings(data_dir=str(tmp_path / "data")),
        embedding=EmbeddingSettings(
            batch_size=2,
            show_progress=False,
            dimension=384,
            **embedding,
        ),
    )


def chunk_record(
    text: str,
    *,
    doc_id: str = "groww_large_cap",
    fact_type: str = "expense_ratio",
) -> ChunkRecord:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ChunkRecord(
        chunk_id=f"{doc_id}__{fact_type}__{digest[:8]}",
        doc_id=doc_id,
        source_url="https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
        source_title="HDFC Large Cap Fund Direct Growth",
        source_owner="Groww",
        source_priority=1,
        scraped_at="2026-11-01T07:00:00Z",
        source_date="2026-10-30",
        scheme_slug="hdfc-large-cap-direct-growth",
        scheme_name="HDFC Large Cap Fund Direct Growth",
        amc="HDFC Mutual Fund",
        category="Large Cap",
        plan="Direct",
        option="Growth",
        fact_type=fact_type,
        section="key_stats",
        chunk_type="fact_card",
        text=text,
        token_count=max(1, math.ceil(len(text) / 4)),
        content_sha256=digest,
        chunker_version="test",
    )


def write_chunks(tmp_path: Path, texts: list[str]) -> Path:
    path = tmp_path / "data" / "02_chunks" / "chunks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps(chunk_record(text).model_dump(mode="json"), ensure_ascii=False)
        for text in texts
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "03_embeddings"


def load_matrix(tmp_path: Path) -> np.ndarray:
    return np.load(output_dir(tmp_path) / EMBEDDINGS_FILENAME, allow_pickle=False)


def load_manifest(tmp_path: Path) -> dict:
    return json.loads((output_dir(tmp_path) / MANIFEST_FILENAME).read_text(encoding="utf-8"))


def load_ids(tmp_path: Path) -> list[str]:
    return json.loads((output_dir(tmp_path) / IDS_FILENAME).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------- tests


def test_embed_writes_shape_ids_manifest_and_cache(tmp_path) -> None:
    chunks_path = write_chunks(tmp_path, TEXTS)
    encoder = FakeEncoder()

    summary = run_embed(settings_for(tmp_path), encoder=encoder)

    assert summary.ok
    assert summary.total_chunks == 3
    assert summary.embedded == 3
    assert summary.cached == 0
    assert summary.artifacts_written
    assert encoder.calls == 1
    assert sorted(encoder.texts) == sorted(TEXTS)

    matrix = load_matrix(tmp_path)
    assert matrix.shape == (3, 384)
    assert matrix.dtype == np.float32
    assert np.isfinite(matrix).all()
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-3)
    assert summary.max_norm_error <= 1e-3

    expected_ids = [chunk_record(text).chunk_id for text in TEXTS]
    assert load_ids(tmp_path) == expected_ids

    manifest = load_manifest(tmp_path)
    assert manifest["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert manifest["revision"] == "main"
    assert manifest["dimension"] == 384
    assert manifest["dtype"] == "float32"
    assert manifest["normalized"] is True
    assert manifest["count"] == 3
    assert manifest["chunks_sha256"] == hashlib.sha256(chunks_path.read_bytes()).hexdigest()
    embeddings_path = output_dir(tmp_path) / EMBEDDINGS_FILENAME
    assert manifest["embeddings_sha256"] == hashlib.sha256(
        embeddings_path.read_bytes()
    ).hexdigest()
    assert len(manifest["config_sha256"]) == 64
    assert manifest["created_at"].endswith("Z")

    cache_lines = (
        output_dir(tmp_path) / CACHE_FILENAME
    ).read_text(encoding="utf-8").splitlines()
    assert len(cache_lines) == 3
    cache_rows = [json.loads(line) for line in cache_lines]
    assert all(row["model_key"] == manifest["config_sha256"] for row in cache_rows)
    assert all(row["dimension"] == 384 for row in cache_rows)
    assert {row["content_sha256"] for row in cache_rows} == {
        chunk_record(text).content_sha256 for text in TEXTS
    }


def test_second_run_is_zero_work_and_byte_identical(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)
    first = run_embed(settings_for(tmp_path), encoder=FakeEncoder())
    assert first.ok

    before = {
        path.name: path.read_bytes()
        for path in output_dir(tmp_path).iterdir()
        if path.is_file()
    }

    second = run_embed(settings_for(tmp_path), encoder=ExplodingEncoder())

    assert second.ok
    assert second.embedded == 0
    assert second.cached == 3
    assert not second.artifacts_written
    after = {
        path.name: path.read_bytes()
        for path in output_dir(tmp_path).iterdir()
        if path.is_file()
    }
    assert before == after


def test_only_changed_chunks_are_reembedded(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)
    run_embed(settings_for(tmp_path), encoder=FakeEncoder())

    changed = list(TEXTS)
    changed[1] = (
        "Scheme: HDFC Large Cap Fund Direct Growth\n"
        "Fact: Exit load\nValue: Nil"
    )
    write_chunks(tmp_path, changed)

    encoder = FakeEncoder()
    summary = run_embed(settings_for(tmp_path), encoder=encoder)

    assert summary.embedded == 1
    assert summary.cached == 2
    assert encoder.texts == [changed[1]]
    assert load_matrix(tmp_path).shape == (3, 384)


def test_duplicate_content_is_embedded_once_but_keeps_every_row(tmp_path) -> None:
    duplicated = [TEXTS[0], TEXTS[0], TEXTS[1]]
    path = tmp_path / "data" / "02_chunks" / "chunks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        chunk_record(text, fact_type=f"fact_{index}").model_dump(mode="json")
        for index, text in enumerate(duplicated)
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    encoder = FakeEncoder()
    summary = run_embed(settings_for(tmp_path), encoder=encoder)

    assert summary.total_chunks == 3
    assert summary.embedded == 3  # rows, not unique texts
    assert encoder.texts == [TEXTS[0], TEXTS[1]]  # unique texts only
    matrix = load_matrix(tmp_path)
    assert matrix.shape == (3, 384)
    assert np.allclose(matrix[0], matrix[1])


def test_model_change_invalidates_cache_and_manifest(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)
    first = run_embed(settings_for(tmp_path), encoder=FakeEncoder())
    assert first.ok
    old_manifest = load_manifest(tmp_path)

    changed_settings = settings_for(
        tmp_path, model_name="sentence-transformers/paraphrase-MiniLM-L3-v2"
    )
    encoder = FakeEncoder()
    second = run_embed(changed_settings, encoder=encoder)

    assert second.embedded == 3
    assert second.cached == 0
    assert encoder.calls == 1

    manifest = load_manifest(tmp_path)
    assert manifest["embedding_model"] == "sentence-transformers/paraphrase-MiniLM-L3-v2"
    assert manifest["config_sha256"] != old_manifest["config_sha256"]
    cache_rows = [
        json.loads(line)
        for line in (output_dir(tmp_path) / CACHE_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {row["model_key"] for row in cache_rows} == {manifest["config_sha256"]}


def test_rebuild_ignores_the_cache(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)
    run_embed(settings_for(tmp_path), encoder=FakeEncoder())

    encoder = FakeEncoder()
    summary = run_embed(settings_for(tmp_path), encoder=encoder, force=True)

    assert summary.embedded == 3
    assert summary.cached == 0
    assert encoder.calls == 1


def test_dimension_mismatch_is_rejected_without_writing_vectors(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)

    with pytest.raises(EmbedError, match="expected"):
        run_embed(settings_for(tmp_path), encoder=FakeEncoder(dimension=8))

    assert not (output_dir(tmp_path) / EMBEDDINGS_FILENAME).exists()


def test_zero_vector_is_rejected(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)

    class ZeroEncoder:
        def encode(self, sentences: list[str], **kwargs: object) -> np.ndarray:
            return np.zeros((len(sentences), 384), dtype=np.float32)

    with pytest.raises(EmbedError, match="zero"):
        run_embed(settings_for(tmp_path), encoder=ZeroEncoder())


def test_corrupt_cache_is_ignored_and_rebuilt(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)
    run_embed(settings_for(tmp_path), encoder=FakeEncoder())

    cache_path = output_dir(tmp_path) / CACHE_FILENAME
    cache_path.write_text("{not json}\n", encoding="utf-8")

    encoder = FakeEncoder()
    summary = run_embed(settings_for(tmp_path), encoder=encoder)

    assert summary.embedded == 3
    assert summary.cached == 0
    assert encoder.calls == 1
    rows = [
        json.loads(line)
        for line in cache_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 3


def test_non_normalized_config_is_rejected(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)

    with pytest.raises(ConfigError, match="normalize"):
        run_embed(settings_for(tmp_path, normalize=False), encoder=FakeEncoder())


def test_unknown_provider_is_rejected(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)

    with pytest.raises(ConfigError, match="provider"):
        run_embed(settings_for(tmp_path, provider="openai"), encoder=FakeEncoder())


def test_missing_chunks_file_names_the_fix(tmp_path) -> None:
    with pytest.raises(Exception) as caught:
        run_embed(settings_for(tmp_path), encoder=FakeEncoder())

    assert "run python -m app.cli chunk" in str(caught.value)


def test_cache_rows_are_pruned_to_current_chunks(tmp_path) -> None:
    write_chunks(tmp_path, TEXTS)
    run_embed(settings_for(tmp_path), encoder=FakeEncoder())

    write_chunks(tmp_path, TEXTS[:2])
    summary = run_embed(settings_for(tmp_path), encoder=FakeEncoder())

    assert summary.total_chunks == 2
    rows = [
        json.loads(line)
        for line in (output_dir(tmp_path) / CACHE_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(rows) == 2


def test_embedding_order_follows_the_chunks_file(tmp_path) -> None:
    reordered = [TEXTS[2], TEXTS[0], TEXTS[1]]
    write_chunks(tmp_path, reordered)

    run_embed(settings_for(tmp_path), encoder=FakeEncoder())

    matrix = load_matrix(tmp_path)
    assert load_ids(tmp_path) == [chunk_record(text).chunk_id for text in reordered]
    for row, text in zip(matrix, reordered, strict=True):
        expected = FakeEncoder().encode([text])[0]
        expected = expected / np.linalg.norm(expected)
        assert np.allclose(row, expected, atol=1e-6)
