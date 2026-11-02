"""Phase 5 gate: scheme resolution, filters, gates, and stored traces."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from app.config import (
    AppSettings,
    EmbeddingSettings,
    RetrievalSettings,
    Settings,
    VectorDBSettings,
)
from app.pipeline.embed import run_embed
from app.pipeline.index import run_index
from app.pipeline.retrieve import (
    Candidate,
    assemble_context,
    classify_intent,
    normalize_query,
    resolve_scheme,
    retrieve,
    trace_for,
)
from app.schemas import ChunkRecord
from app.sources import load_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LARGE_CAP = "hdfc-large-cap-direct-growth"
MID_CAP = "hdfc-mid-cap-direct-growth"
ELSS = "hdfc-elss-tax-saver-direct-growth"


class FakeEncoder:
    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        rows: list[np.ndarray] = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            positions = np.arange(1, self.dimension + 1, dtype=np.float64)
            rows.append(
                (np.sin(positions * (seed % 89 + 1) / 97.0) + 0.5).astype(np.float32)
            )
        return np.vstack(rows)


def settings_for(tmp_path: Path, *, min_similarity: float = -1.0) -> Settings:
    return Settings(
        app=AppSettings(data_dir=str(tmp_path / "data")),
        embedding=EmbeddingSettings(batch_size=2, show_progress=False, dimension=384),
        vector_db=VectorDBSettings(
            persist_directory=str(tmp_path / "data" / "04_vector_db" / "chroma"),
            collection="retrieve_test",
        ),
        retrieval=RetrievalSettings(min_similarity=min_similarity),
    )


def chunk(
    text: str,
    *,
    doc_id: str,
    fact_type: str,
    scheme_slug: str | None,
    scheme_name: str,
) -> ChunkRecord:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ChunkRecord(
        chunk_id=f"{doc_id}__{fact_type}__{digest[:8]}",
        doc_id=doc_id,
        source_url=f"https://groww.in/mutual-funds/{doc_id}",
        source_title=scheme_name,
        source_owner="Groww",
        source_priority=1,
        scraped_at="2026-11-01T07:00:00Z",
        source_date="2026-10-30",
        scheme_slug=scheme_slug,
        scheme_name=scheme_name,
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
    chunk(
        "Scheme: HDFC Large Cap Fund Direct Growth\nFact: Expense ratio\nValue: 1.02%",
        doc_id="groww_large_cap",
        fact_type="expense_ratio",
        scheme_slug=LARGE_CAP,
        scheme_name="HDFC Large Cap Fund Direct Growth",
    ),
    chunk(
        "Scheme: HDFC Mid Cap Fund Direct Growth\nFact: Exit load\nValue: 1% within 1 year",
        doc_id="groww_mid_cap",
        fact_type="exit_load",
        scheme_slug=MID_CAP,
        scheme_name="HDFC Mid Cap Fund Direct Growth",
    ),
    chunk(
        "Scheme: HDFC ELSS Tax Saver Fund Direct Growth\nFact: Minimum SIP\nValue: ₹500",
        doc_id="groww_elss",
        fact_type="minimum_sip",
        scheme_slug=ELSS,
        scheme_name="HDFC ELSS Tax Saver Fund Direct Growth",
    ),
]


def prepare(tmp_path: Path, *, min_similarity: float = -1.0) -> Settings:
    settings = settings_for(tmp_path, min_similarity=min_similarity)
    path = settings.data_dir_path / "02_chunks" / "chunks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
            for record in RECORDS
        )
        + "\n",
        encoding="utf-8",
    )
    run_embed(settings, encoder=FakeEncoder())
    summary = run_index(settings)
    assert summary.ok, summary.describe()
    return settings


@pytest.fixture()
def registry():
    return load_registry(PROJECT_ROOT / "config" / "sources.yaml")


# --------------------------------------------------------------- normalization


def test_normalization_expands_domain_abbreviations() -> None:
    assert "expense ratio" in normalize_query("What is the ER?")
    assert "systematic investment plan" in normalize_query("SIP details")
    assert "consolidated account statement" in normalize_query("CAS download")
    assert "equity linked savings scheme" in normalize_query("ELSS lock in")


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("expense ratio hdfc large cap", "expense_ratio"),
        ("exit load hdfc mid cap", "exit_load"),
        ("minimum sip hdfc elss", "minimum_sip"),
        ("elss lock in period", "lock_in"),
        ("riskometer hdfc mid cap", "riskometer"),
        ("benchmark hdfc large cap", "benchmark"),
        ("how to download capital gains statement", "statement_download"),
        ("hdfc amc sponsor", "amc_overview"),
        ("what is a riskometer", "riskometer"),
    ],
)
def test_classify_intent(query: str, intent: str) -> None:
    assert classify_intent(normalize_query(query)) == intent


# ------------------------------------------------------------------- resolution


def test_alias_resolution_prefers_the_longest_phrase(registry) -> None:
    result = resolve_scheme("expense ratio of HDFC Large Cap", registry)

    assert result.status == "resolved"
    assert result.slug == LARGE_CAP
    assert result.method == "alias"


def test_fuzzy_resolution_tolerates_a_typo(registry) -> None:
    result = resolve_scheme("hdfc larg cap expense ratio", registry)

    assert result.status == "resolved"
    assert result.slug == LARGE_CAP
    assert result.method == "fuzzy"


def test_session_inheritance_applies_to_scoped_facts(registry) -> None:
    result = resolve_scheme(
        "exit load of hdfc fund",
        registry,
        session_last_scheme=MID_CAP,
        intent="exit_load",
    )

    assert result.status == "resolved"
    assert result.slug == MID_CAP
    assert result.method == "session"


def test_missing_scheme_returns_a_clarification(registry) -> None:
    result = resolve_scheme("expense ratio?", registry)

    assert result.status == "missing"
    assert result.message


def test_other_amc_is_out_of_scope(registry) -> None:
    result = resolve_scheme("expense ratio sbi bluechip fund", registry)

    assert result.status == "out_of_scope"
    assert result.method == "other_amc"


def test_unsupported_hdfc_category_is_out_of_scope(registry) -> None:
    result = resolve_scheme("hdfc small cap expense ratio", registry)

    assert result.status == "out_of_scope"
    assert result.method == "unsupported_scheme"


# ------------------------------------------------------------------------ gates


def test_scheme_scoped_fact_passes_the_gate_and_writes_a_trace(tmp_path, registry) -> None:
    settings = prepare(tmp_path)
    request_id = "req_TESTRETRIEVE000000000001"

    result = retrieve(
        settings,
        query="expense ratio of HDFC Large Cap",
        registry=registry,
        encoder=FakeEncoder(),
        request_id=request_id,
    )

    assert result.status == "ok"
    assert result.gate_passed
    assert result.filters == {"scheme_slug": LARGE_CAP}
    assert result.context_chunks
    assert {c.scheme_slug for c in result.context_chunks} == {LARGE_CAP}
    assert result.trace_path is not None and result.trace_path.exists()

    trace = trace_for(settings, request_id)
    assert trace is not None
    assert trace.status == "ok"
    assert trace.gate_passed
    assert trace.selected == [c.chunk_id for c in result.context_chunks]


def test_low_similarity_is_not_found(tmp_path, registry) -> None:
    settings = prepare(tmp_path, min_similarity=0.99)

    result = retrieve(
        settings,
        query="expense ratio of HDFC Large Cap",
        registry=registry,
        encoder=FakeEncoder(),
    )

    assert result.status == "not_found"
    assert result.reason == "below_similarity"
    assert result.context_chunks == []
    assert result.trace_path is not None


def test_missing_scheme_clarifies_before_embedding(tmp_path, registry) -> None:
    settings = prepare(tmp_path)

    result = retrieve(
        settings,
        query="Expense ratio?",
        registry=registry,
        encoder=FakeEncoder(),
    )

    assert result.status == "clarify"
    assert result.reason == "missing_scheme"
    assert result.filters == {}
    assert result.trace_path is not None


def test_other_amc_never_reaches_chroma(tmp_path, registry) -> None:
    settings = prepare(tmp_path)

    result = retrieve(
        settings,
        query="Expense ratio of SBI Bluechip Fund",
        registry=registry,
        encoder=FakeEncoder(),
    )

    assert result.status == "out_of_scope"
    assert result.trace_path is not None


def test_context_assembly_dedupes_and_respects_the_budget() -> None:
    metadata = {
        "scheme_name": "HDFC Large Cap Fund Direct Growth",
        "fact_type": "expense_ratio",
        "section": "key_stats",
        "source_url": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
        "source_date": "2026-10-30",
        "content_sha256": "a" * 64,
    }
    first = Candidate("one", "Fact: Expense ratio\nValue: 1.02%", metadata, 0.8, 0.85)
    duplicate = Candidate("two", "Fact: Expense ratio\nValue: 1.02%", metadata, 0.7, 0.75)
    blocks, text, tokens = assemble_context([first, duplicate], max_tokens=1200)

    assert len(blocks) == 1
    assert "[S1]" in blocks[0]
    assert "1.02%" in text
    assert tokens > 0
