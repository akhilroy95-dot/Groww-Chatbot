"""Phase 1 gate: typed extraction and narrative filtering."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.extract import (
    ExtractionError,
    covered_facts,
    extract_document,
    policy_violations,
    render_plain_text,
)
from app.sources import find_source, load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phase1"


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def extract(registry, fixture: str, source_id: str) -> dict:
    entry = find_source(registry, source_id)
    assert entry is not None, source_id
    return extract_document(
        entry,
        (FIXTURES / fixture).read_bytes(),
        fetched_at="2026-11-01T00:00:00Z",
    )


def test_scheme_extraction_reads_only_allowlisted_fields(registry) -> None:
    document = extract(registry, "scheme_large_cap.html", "groww_large_cap")
    facts = document["facts"]

    assert facts["expense_ratio"]["value"] == "1.02"
    assert facts["minimum_sip"]["value"] == "100"
    assert facts["minimum_lumpsum"]["value"] == "100"
    assert facts["exit_load"]["value"] == "1% if redeemed within 1 year"
    assert facts["riskometer"]["value"] == "Very High"
    assert facts["benchmark"]["value"] == "NIFTY 100 Total Return Index"

    covered = covered_facts(document)
    entry = find_source(registry, "groww_large_cap")
    assert set(entry.expected_facts) <= covered
    assert policy_violations(document) == []


def test_scheme_extraction_keeps_elss_lock_in(registry) -> None:
    document = extract(registry, "scheme_elss.html", "groww_elss")

    assert document["facts"]["lock_in"]["value"] == "3 years"
    assert document["facts"]["exit_load"]["value"] == "Nil"
    assert document["facts"]["expense_ratio"]["value"] == "1.2"
    assert "returns" not in render_plain_text(document).lower()


def test_help_page_extracts_ordered_download_steps(registry) -> None:
    document = extract(registry, "help_steps.html", "groww_help_tax_proof")

    names = [section["name"] for section in document["sections"]]
    assert "statement_steps" in names
    steps = next(s for s in document["sections"] if s["name"] == "statement_steps")
    assert len(steps["items"]) == 4
    assert steps["items"][0] == "Go to your profile."
    assert any(section["name"] == "note" for section in document["sections"])
    assert "statement_download" in covered_facts(document)
    assert policy_violations(document) == []


def test_help_definition_page_keeps_bare_div_intro_and_drops_advice(registry) -> None:
    document = extract(registry, "help_definition.html", "groww_help_cas_what")

    assert document["facts"]["definition"]["value"].startswith("CAS stands for")
    section = next(s for s in document["sections"] if s["name"] == "definition")
    assert any("Consolidated Account Statement" in p for p in section["paragraphs"])
    assert any("Why is CAS important?" in p for p in section["paragraphs"])

    blob = str(document).lower()
    assert "portfolio" not in blob
    assert "performance" not in blob
    assert "definition" in covered_facts(document)


def test_informational_statement_page_counts_as_statement_download(registry) -> None:
    document = extract(registry, "article_statement.html", "amfi_cas")

    assert covered_facts(document) >= {"statement_download"}
    assert any(
        section["name"] == "statement_download" for section in document["sections"]
    )
    # "Generating" contains "rating" as a substring but is not a rating claim.
    assert policy_violations(document) == []
    assert "e-cas" in render_plain_text(document).lower()


def test_riskometer_page_covers_the_fact_and_drops_return_sentences(registry) -> None:
    document = extract(registry, "article_riskometer.html", "sebi_riskometer")

    assert covered_facts(document) >= {"riskometer", "definition"}
    body = render_plain_text(document).lower()
    assert "risk-measuring tool" in body
    assert "returns are not guaranteed" not in body
    assert any("dropped" in warning for warning in document["warnings"])
    assert policy_violations(document) == []


def test_policy_matching_uses_words_not_substrings() -> None:
    safe = {
        "title": "Understanding the Riskometer",
        "facts": {},
        "sections": [
            {
                "name": "definition",
                "heading": "How it works",
                "paragraphs": ["Generating awareness is part of investor education."],
                "items": ["Benchmark: NIFTY 100 Total Return Index"],
            }
        ],
    }
    assert policy_violations(safe) == []

    unsafe = {
        "title": "Fund ratings",
        "facts": {},
        "sections": [{"name": "x", "heading": "", "paragraphs": [], "items": []}],
    }
    assert "ratings" in policy_violations(unsafe)


def test_extraction_fails_when_banned_content_survives() -> None:
    entry = find_source(load_registry(), "groww_large_cap")
    html = (FIXTURES / "scheme_large_cap.html").read_text(encoding="utf-8")
    html = html.replace("NIFTY 100 Total Return Index", "NIFTY 100 Returns Index")

    with pytest.raises(ExtractionError, match="content policy"):
        extract_document(entry, html, fetched_at="2026-11-01T00:00:00Z")
