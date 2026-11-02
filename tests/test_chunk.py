"""Phase 2 gate: fact-card chunking, metadata, coverage, and determinism."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import AppSettings, ChunkingSettings, CorpusSettings, Settings
from app.pipeline import chunk as chunk_module
from app.pipeline.chunk import estimate_tokens, run_chunk, window_units
from app.schemas import ChunkRecord, SourceEntry, SourceRegistry
from app.sources import CORE_REQUIRED_FACTS

LARGE_CAP_URL = "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"
LARGE_CAP_SLUG = "hdfc-large-cap-direct-growth"
LARGE_CAP_NAME = "HDFC Large Cap Fund Direct Growth"


# ----------------------------------------------------------------------- helpers


def source(
    source_id: str,
    url: str,
    *,
    role: str = "core",
    doc_type: str = "groww_scheme",
    expected_facts: list[str] | None = None,
    scheme_slug: str | None = None,
    scheme_name: str | None = None,
    category: str | None = None,
    plan: str | None = None,
    option: str | None = None,
    priority: int = 1,
    fetch_method: str = "httpx",
) -> SourceEntry:
    return SourceEntry(
        id=source_id,
        title=source_id.replace("_", " ").title(),
        url=url,
        owner="Test",
        doc_type=doc_type,
        role=role,
        source_priority=priority,
        expected_facts=expected_facts or [],
        scheme_slug=scheme_slug,
        scheme_name=scheme_name,
        category=category,
        plan=plan,
        option=option,
        fetch_method=fetch_method,
    )


def registry(*sources: SourceEntry) -> SourceRegistry:
    return SourceRegistry(
        allowlist_domains=["groww.in", "amfiindia.com", "investor.sebi.gov.in"],
        sources=list(sources),
    )


def large_cap_source() -> SourceEntry:
    return source(
        "groww_large_cap",
        LARGE_CAP_URL,
        role="core",
        doc_type="groww_scheme",
        expected_facts=list(CORE_REQUIRED_FACTS),
        scheme_slug=LARGE_CAP_SLUG,
        scheme_name=LARGE_CAP_NAME,
        category="Large Cap",
        plan="Direct",
        option="Growth",
    )


def large_cap_document(
    *,
    facts: dict | None = None,
    sections: list[dict] | None = None,
    title: str = LARGE_CAP_NAME,
    url: str = LARGE_CAP_URL,
    source_id: str = "groww_large_cap",
    scheme_slug: str = LARGE_CAP_SLUG,
    scheme_name: str = LARGE_CAP_NAME,
    category: str = "Large Cap",
) -> dict:
    default_facts = {
        "expense_ratio": {
            "label": "Expense ratio",
            "value": "1.02",
            "unit": "%",
            "section": "key_stats",
            "text": "Expense ratio: 1.02%",
        },
        "exit_load": {
            "label": "Exit load",
            "value": "1% if redeemed within 1 year",
            "unit": None,
            "section": "exit_load",
            "text": "Exit load: 1% if redeemed within 1 year",
        },
        "minimum_sip": {
            "label": "Minimum SIP",
            "value": "100",
            "unit": "INR",
            "section": "minimum_investments",
            "text": "Minimum SIP: ₹100",
        },
        "minimum_lumpsum": {
            "label": "Minimum lumpsum investment",
            "value": "100",
            "unit": "INR",
            "section": "minimum_investments",
            "text": "Minimum lumpsum investment: ₹100",
        },
        "riskometer": {
            "label": "Riskometer level",
            "value": "Very High",
            "unit": None,
            "section": "key_stats",
            "text": "Riskometer level: Very High Risk",
        },
        "benchmark": {
            "label": "Benchmark",
            "value": "NIFTY 100 Total Return Index",
            "unit": None,
            "section": "benchmark",
            "text": "Benchmark: NIFTY 100 Total Return Index",
        },
    }
    default_sections = [
        {
            "name": "scheme_basics",
            "heading": "Scheme basics",
            "paragraphs": [],
            "items": [
                f"Scheme name: {scheme_name}",
                "AMC: HDFC Mutual Fund",
                f"Category: {category}",
                "Plan: Direct",
                "Option: Growth",
                "Launch date: 2013-01-01",
                "ISIN: INF179K01YV8",
                "Registrar: CAMS",
            ],
        },
        {
            "name": "key_stats",
            "heading": "Key stats",
            "paragraphs": [],
            "items": [
                "Expense ratio: 1.02%",
                "Fund size (AUM): ₹37,293.90 Cr",
                "NAV: ₹1,164.95 (as of 2026-10-30)",
            ],
        },
        {
            "name": "minimum_investments",
            "heading": "Minimum investments",
            "paragraphs": [],
            "items": [
                "Minimum SIP: ₹100",
                "Minimum lumpsum investment: ₹100",
                "Minimum additional investment: ₹100",
            ],
        },
        {
            "name": "exit_load",
            "heading": "Exit load",
            "paragraphs": ["Exit load: 1% if redeemed within 1 year"],
            "items": [],
        },
        {
            "name": "benchmark",
            "heading": "Benchmark",
            "paragraphs": ["Benchmark: NIFTY 100 Total Return Index"],
            "items": [],
        },
        {
            "name": "about",
            "heading": "Investment objective",
            "paragraphs": [
                "The scheme seeks to provide long-term capital appreciation by investing "
                "predominantly in large-cap companies."
            ],
            "items": [],
        },
    ]
    return {
        "schema_version": 1,
        "source_id": source_id,
        "url": url,
        "final_url": url,
        "doc_type": "groww_scheme",
        "role": "core",
        "source_priority": 1,
        "extractor_version": "groww_scheme_v1",
        "title": title,
        "fetched_at": "2026-11-01T07:00:00Z",
        "source_date": "2026-10-30",
        "scheme": {
            "scheme_slug": scheme_slug,
            "scheme_name": scheme_name,
            "amc": "HDFC Mutual Fund",
            "category": category,
            "plan": "Direct",
            "option": "Growth",
        },
        "facts": default_facts if facts is None else facts,
        "sections": default_sections if sections is None else sections,
        "warnings": [],
    }


def article_document(
    source_id: str,
    url: str,
    *,
    title: str,
    sections: list[dict],
    facts: dict | None = None,
    role: str = "support",
    doc_type: str = "sebi_investor",
    priority: int = 2,
) -> dict:
    return {
        "schema_version": 1,
        "source_id": source_id,
        "url": url,
        "final_url": url,
        "doc_type": doc_type,
        "role": role,
        "source_priority": priority,
        "extractor_version": "article_v1",
        "title": title,
        "fetched_at": "2026-11-01T08:00:00Z",
        "source_date": "2026-11-01",
        "scheme": None,
        "facts": facts or {},
        "sections": sections,
        "warnings": [],
    }


def amc_document() -> dict:
    return {
        "schema_version": 1,
        "source_id": "groww_amc_hdfc",
        "url": "https://groww.in/mutual-funds/amc/hdfc-mutual-funds",
        "final_url": "https://groww.in/mutual-funds/amc/hdfc-mutual-funds",
        "doc_type": "groww_amc",
        "role": "core",
        "source_priority": 1,
        "extractor_version": "groww_amc_v1",
        "title": "HDFC Mutual Fund",
        "fetched_at": "2026-11-01T07:05:00Z",
        "source_date": "2026-11-01",
        "scheme": None,
        "facts": {
            "amc_overview": {
                "label": "AMC overview",
                "value": "HDFC Mutual Fund",
                "unit": None,
                "section": "amc_overview",
                "text": (
                    "HDFC Mutual Fund. Incorporated: 1999-12-10; "
                    "Total AUM: ₹934,076.00 Cr."
                ),
            }
        },
        "sections": [
            {
                "name": "amc_overview",
                "heading": "AMC overview",
                "paragraphs": [],
                "items": [
                    "Fund house: HDFC Mutual Fund",
                    "Incorporated: 1999-12-10",
                    "Total AUM: ₹934,076.00 Cr",
                ],
            },
            {
                "name": "about",
                "heading": "About the AMC",
                "paragraphs": [
                    "HDFC Asset Management Company Ltd. is an asset management company in India.",
                    "The company services distribution partners through branches across India.",
                ],
                "items": [],
            },
        ],
        "warnings": [],
    }


def settings_for(tmp_path: Path, **chunking: object) -> Settings:
    return Settings(
        app=AppSettings(data_dir=str(tmp_path / "data")),
        corpus=CorpusSettings(sources_file=str(tmp_path / "sources.yaml")),
        chunking=ChunkingSettings(**chunking),
    )


def write_corpus(
    tmp_path: Path,
    sources: list[SourceEntry],
    documents: dict[str, dict],
    *,
    include_citation_files: bool = False,
) -> None:
    raw = tmp_path / "data" / "01_raw"
    raw.mkdir(parents=True, exist_ok=True)
    records = []
    for entry in sources:
        document = documents.get(entry.id)
        if entry.role == "citation_only":
            if include_citation_files and document is not None:
                (raw / f"{entry.id}.json").write_text(
                    json.dumps(document, ensure_ascii=False), encoding="utf-8"
                )
            continue
        assert document is not None, entry.id
        (raw / f"{entry.id}.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        records.append(
            {
                "source_id": entry.id,
                "url": str(entry.url),
                "role": entry.role,
                "http_status": 200,
                "fetched_at": document["fetched_at"],
                "sha256": "test",
            }
        )
    (raw / "manifest.json").write_text(
        json.dumps({"version": 1, "sources": records}, ensure_ascii=False),
        encoding="utf-8",
    )


def run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sources: list[SourceEntry],
    documents: dict[str, dict],
    **chunking: object,
):
    settings = settings_for(tmp_path, **chunking)
    write_corpus(tmp_path, sources, documents)
    monkeypatch.setattr(
        chunk_module, "load_registry", lambda *args, **kwargs: registry(*sources)
    )
    return settings, run_chunk(settings)


def read_chunks(settings: Settings) -> list[ChunkRecord]:
    path = settings.data_dir_path / "02_chunks" / "chunks.jsonl"
    return [
        ChunkRecord(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ------------------------------------------------------------------------- tests


def test_scheme_fact_cards_are_atomic_and_metadata_complete(
    tmp_path, monkeypatch
) -> None:
    settings, summary = run(
        tmp_path, monkeypatch, [large_cap_source()], {"groww_large_cap": large_cap_document()}
    )

    assert summary.ok, summary.describe()
    chunks = read_chunks(settings)

    expense = [chunk for chunk in chunks if chunk.fact_type == "expense_ratio"]
    assert len(expense) == 1
    assert expense[0].chunk_type == "fact_card"
    assert "Value: 1.02%" in expense[0].text
    assert expense[0].text.startswith(f"Scheme: {LARGE_CAP_NAME}\n")
    assert f"AMC: HDFC Mutual Fund | Category: Large Cap | Plan: Direct | Option: Growth" in expense[0].text
    assert expense[0].chunk_id.startswith("groww_large_cap__expense_ratio__")
    assert len(expense[0].chunk_id.rsplit("__", 1)[-1]) == 8

    exit_load = [chunk for chunk in chunks if chunk.fact_type == "exit_load"]
    assert len(exit_load) == 1
    assert "Value: 1% if redeemed within 1 year" in exit_load[0].text

    minimums = [chunk for chunk in chunks if chunk.fact_type == "minimum_sip"]
    assert len(minimums) == 1
    assert minimums[0].chunk_type == "fact_group"
    assert "Minimum SIP: ₹100" in minimums[0].text
    assert "Minimum lumpsum investment: ₹100" in minimums[0].text
    assert "Minimum additional investment: ₹100" in minimums[0].text

    basics = [
        chunk
        for chunk in chunks
        if chunk.fact_type == "scheme_basics" and chunk.chunk_type == "fact_group"
    ]
    assert len(basics) == 1
    assert "Launch date: 2013-01-01" in basics[0].text

    assert any(chunk.fact_type == "aum" for chunk in chunks)
    assert any(chunk.chunk_type == "narrative" for chunk in chunks)

    for chunk in chunks:
        assert chunk.doc_id == "groww_large_cap"
        assert chunk.source_url == LARGE_CAP_URL
        assert chunk.source_priority == 1
        assert chunk.scheme_slug == LARGE_CAP_SLUG
        assert chunk.scheme_name == LARGE_CAP_NAME
        assert chunk.amc == "HDFC Mutual Fund"
        assert chunk.fact_type
        assert chunk.section
        assert chunk.scraped_at == "2026-11-01T07:00:00Z"
        assert 1 <= chunk.token_count <= 300
        assert chunk.text.count("Value:") <= 1

    assert summary.total_chunks == len(chunks)
    assert summary.by_fact_type["expense_ratio"] == 1


def test_elss_lock_in_is_one_card(tmp_path, monkeypatch) -> None:
    url = "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
    entry = source(
        "groww_elss",
        url,
        expected_facts=[*CORE_REQUIRED_FACTS, "lock_in"],
        scheme_slug="hdfc-elss-tax-saver-direct-growth",
        scheme_name="HDFC ELSS Tax Saver Fund Direct Plan Growth",
    )
    document = large_cap_document(
        source_id="groww_elss",
        url=url,
        title="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        scheme_slug="hdfc-elss-tax-saver-direct-growth",
        scheme_name="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        category="ELSS",
    )
    document["facts"]["lock_in"] = {
        "label": "Lock-in period",
        "value": "3 years",
        "unit": None,
        "section": "lock_in",
        "text": "Lock-in period: 3 years",
    }
    document["sections"].append(
        {
            "name": "lock_in",
            "heading": "Lock-in",
            "paragraphs": ["Lock-in period: 3 years"],
            "items": [],
        }
    )

    settings, summary = run(tmp_path, monkeypatch, [entry], {"groww_elss": document})

    assert summary.ok, summary.describe()
    chunks = read_chunks(settings)
    lock_in = [chunk for chunk in chunks if chunk.fact_type == "lock_in"]
    assert len(lock_in) == 1
    assert lock_in[0].chunk_type == "fact_card"
    assert "Value: 3 years" in lock_in[0].text


def test_help_steps_preserve_order_and_drop_notes(tmp_path, monkeypatch) -> None:
    url = "https://groww.in/help/mutual-funds/order/how-to-download-tax-statement"
    entry = source(
        "groww_help_elss_statement",
        url,
        role="support",
        doc_type="groww_help",
        expected_facts=["statement_download"],
        priority=2,
    )
    document = article_document(
        "groww_help_elss_statement",
        url,
        title="What is a mutual funds ELSS report?",
        doc_type="groww_help",
        sections=[
            {
                "name": "answer",
                "heading": "What is a mutual funds ELSS report?",
                "paragraphs": [
                    "The ELSS report shows your purchases in ELSS mutual funds.",
                    "CAMS",
                ],
                "items": [],
            },
            {
                "name": "statement_steps",
                "heading": "Steps",
                "paragraphs": [],
                "items": [
                    "Go to your profile.",
                    "Select the 'Reports' option.",
                    "Click on 'Download'.",
                ],
            },
            {
                "name": "note",
                "heading": "Note",
                "paragraphs": ["Note: The password is your PAN number."],
                "items": [],
            },
        ],
    )

    settings, summary = run(
        tmp_path, monkeypatch, [entry], {"groww_help_elss_statement": document}
    )

    assert summary.ok, summary.describe()
    chunks = read_chunks(settings)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_type == "steps"
    assert chunk.fact_type == "statement_download"
    assert "PAN" not in chunk.text
    assert "CAMS" not in chunk.text
    assert chunk.text.index("1. Go to your profile.") < chunk.text.index(
        "2. Select the 'Reports' option."
    )
    assert chunk.text.index("2. Select the 'Reports' option.") < chunk.text.index(
        "3. Click on 'Download'."
    )


def test_definition_windows_are_bounded_and_overlap(tmp_path, monkeypatch) -> None:
    url = "https://www.amfiindia.com/investor/knowledge-center-info?zoneName=expenseRatio"
    entry = source(
        "amfi_expense_ratio",
        url,
        role="support",
        doc_type="amfi_page",
        expected_facts=["definition"],
        priority=2,
    )
    paragraphs = [
        f"Paragraph {index} explains one part of the expense ratio rule with enough "
        "words to make a complete sentence for the chunk window."
        for index in range(1, 25)
    ]
    document = article_document(
        "amfi_expense_ratio",
        url,
        title="AMFI - Expense Ratio explainer",
        doc_type="amfi_page",
        sections=[
            {
                "name": "definition",
                "heading": "Expense Ratio",
                "paragraphs": paragraphs,
                "items": [],
            }
        ],
    )

    settings, summary = run(tmp_path, monkeypatch, [entry], {"amfi_expense_ratio": document})

    assert summary.ok, summary.describe()
    chunks = read_chunks(settings)
    assert len(chunks) > 1
    assert all(chunk.chunk_type == "definition" for chunk in chunks)
    assert all(chunk.token_count <= 300 for chunk in chunks)
    assert all("Fact: Expense Ratio" in chunk.text for chunk in chunks)

    def body_lines(chunk: ChunkRecord) -> list[str]:
        return [
            line
            for line in chunk.text.splitlines()
            if not line.startswith(("Topic:", "Fact:", "Source:", "Scraped:"))
        ]

    assert body_lines(chunks[0])[-1] in chunks[1].text
    assert all("Paragraph" in line for line in body_lines(chunks[0]))


def test_amc_summary_key_info_and_about(tmp_path, monkeypatch) -> None:
    entry = source(
        "groww_amc_hdfc",
        "https://groww.in/mutual-funds/amc/hdfc-mutual-funds",
        doc_type="groww_amc",
        expected_facts=["amc_overview"],
    )
    settings, summary = run(
        tmp_path, monkeypatch, [entry], {"groww_amc_hdfc": amc_document()}
    )

    assert summary.ok, summary.describe()
    chunks = read_chunks(settings)
    assert len(chunks) == 3
    assert {chunk.chunk_type for chunk in chunks} == {"fact_card", "fact_group", "narrative"}
    assert all(chunk.fact_type == "amc_overview" for chunk in chunks)
    assert all(chunk.amc == "HDFC Mutual Fund" for chunk in chunks)

    summary_card = next(chunk for chunk in chunks if chunk.chunk_type == "fact_card")
    assert "Value: HDFC Mutual Fund. Incorporated: 1999-12-10" in summary_card.text

    key_info = next(chunk for chunk in chunks if chunk.chunk_type == "fact_group")
    assert "Fund house: HDFC Mutual Fund" in key_info.text

    about = next(chunk for chunk in chunks if chunk.chunk_type == "narrative")
    assert "Fact: About the AMC" in about.text


def test_missing_core_fact_is_fatal(tmp_path, monkeypatch) -> None:
    document = large_cap_document()
    document["facts"].pop("benchmark")
    document["sections"] = [
        section for section in document["sections"] if section["name"] != "benchmark"
    ]

    settings, summary = run(
        tmp_path, monkeypatch, [large_cap_source()], {"groww_large_cap": document}
    )

    assert not summary.ok
    assert summary.core_missing == {"groww_large_cap": ["benchmark"]}
    assert "fatal" in summary.describe()
    assert settings.data_dir_path.joinpath("02_chunks", "chunk_report.md").exists()


def test_missing_support_fact_is_a_warning(tmp_path, monkeypatch) -> None:
    url = "https://www.amfiindia.com/investor/knowledge-center-info"
    entry = source(
        "amfi_expense_ratio",
        url,
        role="support",
        doc_type="amfi_page",
        expected_facts=["definition", "statement_download"],
        priority=2,
    )
    document = article_document(
        "amfi_expense_ratio",
        url,
        title="AMFI - Expense Ratio explainer",
        sections=[
            {
                "name": "definition",
                "heading": "Expense Ratio",
                "paragraphs": ["The expense ratio is a percentage of the fund's net assets."],
                "items": [],
            }
        ],
    )

    settings, summary = run(tmp_path, monkeypatch, [entry], {"amfi_expense_ratio": document})

    assert summary.ok, summary.describe()
    assert summary.core_missing == {}
    assert summary.missing_expected == {"amfi_expense_ratio": ["statement_download"]}
    assert "support, warning" in summary.describe()


def test_policy_violation_is_rejected(tmp_path, monkeypatch) -> None:
    document = large_cap_document()
    document["sections"][-1]["paragraphs"].append(
        "The fund has delivered strong returns over five years."
    )

    settings, summary = run(
        tmp_path, monkeypatch, [large_cap_source()], {"groww_large_cap": document}
    )

    assert not summary.ok
    assert summary.errors
    assert "content policy" in summary.errors[0][1]
    assert "returns" in summary.errors[0][1]
    assert read_chunks(settings) == []


def test_citation_only_documents_are_never_chunked(tmp_path, monkeypatch) -> None:
    citation = source(
        "hdfc_factsheets",
        "https://www.hdfcfund.com/mutual-funds/factsheets",
        role="citation_only",
        doc_type="hdfc_page",
        expected_facts=[],
        priority=3,
        fetch_method="manual",
    )
    citation_document = article_document(
        "hdfc_factsheets",
        "https://www.hdfcfund.com/mutual-funds/factsheets",
        title="HDFC MF - Fund factsheets",
        role="citation_only",
        doc_type="hdfc_page",
        priority=3,
        sections=[
            {
                "name": "definition",
                "heading": "Factsheet",
                "paragraphs": ["Citation-only page should never be chunked."],
                "items": [],
            }
        ],
    )

    settings = settings_for(tmp_path)
    write_corpus(
        tmp_path,
        [large_cap_source(), citation],
        {"groww_large_cap": large_cap_document(), "hdfc_factsheets": citation_document},
        include_citation_files=True,
    )
    monkeypatch.setattr(
        chunk_module,
        "load_registry",
        lambda *args, **kwargs: registry(large_cap_source(), citation),
    )

    summary = run_chunk(settings)
    assert summary.ok, summary.describe()
    assert summary.total_sources == 1
    chunks = read_chunks(settings)
    assert chunks
    assert all(chunk.doc_id != "hdfc_factsheets" for chunk in chunks)


def test_second_run_is_byte_identical_and_ids_are_stable(tmp_path, monkeypatch) -> None:
    sources = [large_cap_source()]
    documents = {"groww_large_cap": large_cap_document()}
    settings = settings_for(tmp_path)
    write_corpus(tmp_path, sources, documents)
    monkeypatch.setattr(
        chunk_module, "load_registry", lambda *args, **kwargs: registry(*sources)
    )

    first = run_chunk(settings)
    assert first.ok, first.describe()
    first_ids = [chunk.chunk_id for chunk in read_chunks(settings)]
    outputs = settings.data_dir_path / "02_chunks"
    first_bytes = {path.name: path.read_bytes() for path in outputs.iterdir() if path.is_file()}

    second = run_chunk(settings)
    assert second.ok, second.describe()
    second_ids = [chunk.chunk_id for chunk in read_chunks(settings)]
    second_bytes = {path.name: path.read_bytes() for path in outputs.iterdir() if path.is_file()}

    assert first_ids == second_ids
    assert first_bytes == second_bytes


def test_context_header_can_be_disabled(tmp_path, monkeypatch) -> None:
    settings, summary = run(
        tmp_path,
        monkeypatch,
        [large_cap_source()],
        {"groww_large_cap": large_cap_document()},
        add_context_header=False,
    )

    assert summary.ok, summary.describe()
    chunks = read_chunks(settings)
    expense = next(chunk for chunk in chunks if chunk.fact_type == "expense_ratio")
    assert expense.text == "Value: 1.02%"
    assert all(not chunk.text.startswith("Scheme:") for chunk in chunks)


def test_window_units_respects_budget_and_carries_overlap() -> None:
    units = [
        f"Sentence {index} has ten words so every unit costs a few tokens."
        for index in range(20)
    ]

    windows = window_units(units, budget_tokens=40, overlap_tokens=20)

    assert len(windows) > 1
    assert all(estimate_tokens(" ".join(window)) <= 40 for window in windows)
    assert windows[1][0] == windows[0][-1]


def test_definition_classifier_ignores_incidental_body_words() -> None:
    """Only the heading/title may classify a definition chunk.

    A page about ELSS mentions "lock-in" inside a benefits paragraph, but the
    chunk is a definition of ELSS.  The explicit lock-in section is what carries
    ``fact_type=lock_in``.
    """
    elss = source(
        "sebi_elss",
        "https://investor.sebi.gov.in/elss.html",
        role="support",
        doc_type="sebi_investor",
        expected_facts=["definition", "lock_in"],
    )

    # Incidental "lock-in" in the body/title does not reclassify a definition.
    assert (
        chunk_module._definition_fact_type(
            elss, "Benefits of ELSS", "A Guide to ELSS"
        )
        == "definition"
    )
    assert (
        chunk_module._definition_fact_type(elss, "Lock-in period", "A Guide to ELSS")
        == "lock_in"
    )


def test_definition_label_falls_back_to_document_title() -> None:
    """Generic AMFI section headings use the page title as the fact label."""
    amfi = source(
        "amfi_ter",
        "https://www.amfiindia.com/investor/knowledge-center-info",
        role="support",
        doc_type="amfi_page",
        expected_facts=["definition", "expense_ratio"],
    )
    document = {"title": "AMFI - TER of Mutual Fund Schemes"}

    label = chunk_module._definition_label("Disclaimer", document)
    assert label == "TER of Mutual Fund Schemes"
    assert (
        chunk_module._definition_fact_type(amfi, label, "TER of Mutual Fund Schemes")
        == "expense_ratio"
    )

    # A real heading is kept verbatim rather than replaced by the title.
    assert chunk_module._definition_label("Expense Ratio", document) == "Expense Ratio"


def test_report_and_csv_are_written_with_required_sections(tmp_path, monkeypatch) -> None:
    settings, summary = run(
        tmp_path, monkeypatch, [large_cap_source()], {"groww_large_cap": large_cap_document()}
    )

    assert summary.ok, summary.describe()
    output = settings.data_dir_path / "02_chunks"
    report = (output / "chunk_report.md").read_text(encoding="utf-8")
    assert "## Counts by source" in report
    assert "## Counts by fact type" in report
    assert "## Token buckets" in report
    assert "## Missing expected facts" in report
    assert "`groww_large_cap`" in report

    csv_text = (output / "chunks.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0].startswith("chunk_id,doc_id,source_url")
    assert "groww_large_cap__expense_ratio__" in csv_text

    jsonl_lines = (output / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(jsonl_lines) == summary.total_chunks
    assert all(json.loads(line)["chunk_id"] for line in jsonl_lines)
