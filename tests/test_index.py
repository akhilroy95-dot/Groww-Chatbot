"""Phase 4 gate: persistent Chroma collection matches the chunk file exactly."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from app.config import AppSettings, ConfigError, EmbeddingSettings, Settings, VectorDBSettings
from app.pipeline.embed import run_embed
from app.pipeline.index import (
    STATS_FILENAME,
    SMOKE_QUERIES,
    SmokeQuery,
    _metadata,
    _smoke_hit,
    run_index,
)
from app.schemas import ChunkRecord

LARGE_CAP_URL = "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"
MID_CAP_URL = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
ELSS_URL = "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"


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


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    vector_db = overrides.pop(
        "vector_db",
        VectorDBSettings(
            persist_directory=str(tmp_path / "data" / "04_vector_db" / "chroma"),
            collection="test_collection",
        ),
    )
    return Settings(
        app=AppSettings(data_dir=str(tmp_path / "data")),
        embedding=EmbeddingSettings(batch_size=2, show_progress=False, dimension=384),
        vector_db=vector_db,
        **overrides,
    )


def chunk_record(
    text: str,
    *,
    doc_id: str = "groww_large_cap",
    fact_type: str = "expense_ratio",
    scheme_slug: str | None = "hdfc-large-cap-direct-growth",
    source_url: str = LARGE_CAP_URL,
) -> ChunkRecord:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ChunkRecord(
        chunk_id=f"{doc_id}__{fact_type}__{digest[:8]}",
        doc_id=doc_id,
        source_url=source_url,
        source_title="HDFC test scheme",
        source_owner="Groww",
        source_priority=1,
        scraped_at="2026-11-01T07:00:00Z",
        source_date="2026-10-30",
        scheme_slug=scheme_slug,
        scheme_name="HDFC test scheme",
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


RECORDS = [
    chunk_record(
        "Scheme: HDFC Large Cap Fund\nFact: Expense ratio\nValue: 1.02%",
        doc_id="groww_large_cap",
        fact_type="expense_ratio",
    ),
    chunk_record(
        "Scheme: HDFC Mid Cap Fund\nFact: Exit load\nValue: 1% within 1 year",
        doc_id="groww_mid_cap",
        fact_type="exit_load",
        scheme_slug="hdfc-mid-cap-direct-growth",
        source_url=MID_CAP_URL,
    ),
    chunk_record(
        "Scheme: HDFC ELSS\nFact: Minimum SIP\nValue: ₹500",
        doc_id="groww_elss",
        fact_type="minimum_sip",
        scheme_slug="hdfc-elss-tax-saver-direct-growth",
        source_url=ELSS_URL,
    ),
]


def write_chunks(tmp_path: Path, records: list[ChunkRecord]) -> Path:
    path = tmp_path / "data" / "02_chunks" / "chunks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        for record in records
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def prepare(tmp_path: Path, records: list[ChunkRecord] | None = None) -> Settings:
    settings = settings_for(tmp_path)
    write_chunks(tmp_path, records or RECORDS)
    run_embed(settings, encoder=FakeEncoder())
    return settings


def open_collection(settings: Settings):
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    client = chromadb.PersistentClient(
        path=settings.vector_db.persist_directory,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    return client, client.get_collection(settings.vector_db.collection)


def stats(tmp_path: Path) -> dict:
    path = tmp_path / "data" / "04_vector_db" / STATS_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------- tests


def test_index_writes_collection_and_stats(tmp_path) -> None:
    settings = prepare(tmp_path)

    summary = run_index(settings)

    assert summary.ok, summary.describe()
    assert summary.total_chunks == 3
    assert summary.upserted == 3
    assert summary.pruned == 0
    assert summary.count == 3
    assert not summary.rebuilt

    client, rows = open_collection(settings)
    try:
        assert rows.count() == 3
        assert set(rows.get(include=[])["ids"]) == {
            record.chunk_id for record in RECORDS
        }

        stored = rows.get(include=["documents", "metadatas"])
        by_id = {
            chunk_id: (document, metadata)
            for chunk_id, document, metadata in zip(
                stored["ids"], stored["documents"], stored["metadatas"], strict=True
            )
        }
        for record in RECORDS:
            document, metadata = by_id[record.chunk_id]
            assert document == record.text
            assert metadata["doc_id"] == record.doc_id
            assert metadata["fact_type"] == record.fact_type
            assert metadata["scheme_slug"] == record.scheme_slug
            assert metadata["source_priority"] == record.source_priority
            assert metadata["source_date"] == record.source_date
            assert all(
                isinstance(value, (str, int, float, bool))
                for value in metadata.values()
            )
    finally:
        client.close()

    payload = stats(tmp_path)
    assert payload["count"] == 3
    assert payload["collection"] == "test_collection"
    assert payload["provider"] == "chroma"
    assert payload["distance"] == "cosine"
    assert payload["dimension"] == 384
    assert payload["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert payload["sample_ids"] == [record.chunk_id for record in RECORDS[:3]]
    assert payload["smoke"]["total"] == 0


def test_reindex_is_idempotent_and_prunes_stale_ids(tmp_path) -> None:
    settings = prepare(tmp_path)
    first = run_index(settings)
    assert first.ok

    second = run_index(settings)
    assert second.ok
    assert second.upserted == 3
    assert second.pruned == 0
    assert second.count == 3

    write_chunks(tmp_path, RECORDS[:2])
    run_embed(settings, encoder=FakeEncoder())
    third = run_index(settings)

    assert third.ok, third.describe()
    assert third.total_chunks == 2
    assert third.pruned == 1
    assert third.count == 2
    client, rows = open_collection(settings)
    try:
        assert RECORDS[2].chunk_id not in set(rows.get(include=[])["ids"])
    finally:
        client.close()


def test_scheme_filter_narrows_results(tmp_path) -> None:
    settings = prepare(tmp_path)
    run_index(settings)

    matrix = np.load(
        tmp_path / "data" / "03_embeddings" / "embeddings.npy", allow_pickle=False
    )
    client, rows = open_collection(settings)
    try:
        response = rows.query(
            query_embeddings=matrix[:1].tolist(),
            n_results=10,
            where={"scheme_slug": RECORDS[1].scheme_slug},
        )
    finally:
        client.close()

    assert response["ids"][0] == [RECORDS[1].chunk_id]


def test_model_guard_refuses_and_rebuild_recovers(tmp_path) -> None:
    settings = prepare(tmp_path)
    run_index(settings)
    client, rows = open_collection(settings)
    try:
        rows.modify(
            metadata={
                key: value
                for key, value in rows.metadata.items()
                if key != "hnsw:space"
            }
            | {"embedding_model": "sentence-transformers/other-model"}
        )
    finally:
        client.close()

    with pytest.raises(ConfigError, match="index --rebuild"):
        run_index(settings)

    rebuilt = run_index(settings, rebuild=True)
    assert rebuilt.ok, rebuilt.describe()
    assert rebuilt.rebuilt
    assert rebuilt.count == 3
    client, rows = open_collection(settings)
    try:
        assert rows.metadata["embedding_model"] == settings.embedding.model_name
    finally:
        client.close()


def test_rebuild_removes_unknown_rows(tmp_path) -> None:
    settings = prepare(tmp_path)
    run_index(settings)
    matrix = np.load(
        tmp_path / "data" / "03_embeddings" / "embeddings.npy", allow_pickle=False
    )
    client, rows = open_collection(settings)
    try:
        rows.upsert(
            ids=["manual_stale_row"],
            documents=["stale"],
            embeddings=matrix[:1].tolist(),
            metadatas=[{"doc_id": "stale"}],
        )
        assert rows.count() == 4
    finally:
        client.close()

    summary = run_index(settings, rebuild=True)

    assert summary.ok, summary.describe()
    assert summary.count == 3
    client, rows = open_collection(settings)
    try:
        assert "manual_stale_row" not in set(rows.get(include=[])["ids"])
    finally:
        client.close()


def test_stale_embeddings_are_rejected(tmp_path) -> None:
    settings = prepare(tmp_path)
    run_index(settings)

    changed = RECORDS + [
        chunk_record(
            "Scheme: HDFC ELSS\nFact: Lock-in\nValue: 3 years",
            doc_id="groww_elss",
            fact_type="lock_in",
            scheme_slug="hdfc-elss-tax-saver-direct-growth",
            source_url=ELSS_URL,
        )
    ]
    write_chunks(tmp_path, changed)

    with pytest.raises(ConfigError, match="embed"):
        run_index(settings)


def test_missing_embeddings_names_the_fix(tmp_path) -> None:
    settings = settings_for(tmp_path)
    write_chunks(tmp_path, RECORDS)

    with pytest.raises(ConfigError, match="run python -m app.cli embed"):
        run_index(settings)


def test_non_cosine_distance_is_rejected(tmp_path) -> None:
    settings = settings_for(
        tmp_path,
        vector_db=VectorDBSettings(
            persist_directory=str(tmp_path / "data" / "04_vector_db" / "chroma"),
            collection="test_collection",
            distance="l2",
        ),
    )
    write_chunks(tmp_path, RECORDS)
    run_embed(settings, encoder=FakeEncoder())

    with pytest.raises(ConfigError, match="cosine"):
        run_index(settings)


def test_metadata_is_flat_and_drops_none_values() -> None:
    record = chunk_record(
        "Fact: Definition\nValue: text",
        doc_id="sebi_elss",
        fact_type="definition",
        scheme_slug=None,
        source_url="https://investor.sebi.gov.in/elss.html",
    )
    metadata = _metadata(record)

    assert "scheme_slug" not in metadata
    assert metadata["doc_id"] == "sebi_elss"
    assert metadata["amc"] == "HDFC Mutual Fund"
    assert all(isinstance(value, (str, int, float, bool)) for value in metadata.values())


def test_smoke_hit_requires_both_doc_and_fact_type() -> None:
    expected = SmokeQuery("exit load hdfc mid cap", ("groww_mid_cap",), "exit_load")

    assert _smoke_hit(
        {"doc_id": "groww_mid_cap", "fact_type": "exit_load"}, expected
    )
    assert not _smoke_hit(
        {"doc_id": "groww_large_cap", "fact_type": "exit_load"}, expected
    )
    assert not _smoke_hit(
        {"doc_id": "groww_mid_cap", "fact_type": "scheme_basics"}, expected
    )


def test_smoke_queries_match_the_gate_table() -> None:
    assert [spec.query for spec in SMOKE_QUERIES] == [
        "expense ratio hdfc large cap",
        "exit load hdfc mid cap",
        "minimum sip elss",
        "elss lock in",
        "download capital gains statement",
    ]
    assert SMOKE_QUERIES[3].doc_ids == ("groww_elss", "sebi_elss")
