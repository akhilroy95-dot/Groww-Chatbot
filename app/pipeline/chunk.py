"""Phase 2: turn extracted documents into atomic, citable fact cards.

The chunker reads the Phase 1 artifacts (``data/01_raw/{source_id}.json`` plus
``manifest.json``) and writes ``data/02_chunks/chunks.jsonl``, ``chunks.csv`` and
``chunk_report.md``.

Strategy: ``structure_aware_fact_cards_v1`` (PRD 8.4).  A section name maps to a
chunker function:

* ``key_stats`` / ``exit_load`` / ``lock_in`` / ``benchmark`` -> one fact card
  per atomic fact;
* ``minimum_investments`` / ``scheme_basics`` -> one fact group;
* ``about`` / ``investment_objective`` -> heading-scoped narrative windows;
* ``statement_steps`` / ``statement_download`` -> one ordered-step chunk;
* ``definition`` -> one chunk per concept, windowed when a concept is long;
* ``amc_overview`` -> a summary card plus a key-information group card.

Every chunk repeats a compact context header *inside* its embedded text, so a
bare value such as ``1.02%`` is retrievable when the user names only the fact.
IDs are deterministic: ``{doc_id}__{fact_type}__{content_sha256[:8]}``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from ..config import ConfigError, Settings
from ..schemas import ChunkRecord, SourceEntry, SourceRegistry
from ..sources import fetchable_sources, load_registry
from .extract import clean_text, policy_violations

CHUNKER_VERSION = "structure_aware_fact_cards_v1"

# The corpus is HDFC-only.  Support pages (AMFI/SEBI/Groww Help) are used to
# explain HDFC facts, so every chunk carries the corpus AMC for filtering.  Only
# scheme-specific chunks also carry a ``scheme_slug``.
CORPUS_AMC = "HDFC Mutual Fund"

# Gate 2 says no chunk may exceed 300 tokens; the config targets 250 for fact
# cards and 300 for narrative windows.
HARD_MAX_TOKENS = 300

FACT_LABELS: dict[str, str] = {
    "expense_ratio": "Expense ratio",
    "exit_load": "Exit load",
    "minimum_sip": "Minimum SIP",
    "minimum_lumpsum": "Minimum lumpsum investment",
    "lock_in": "Lock-in period",
    "riskometer": "Riskometer level",
    "benchmark": "Benchmark",
    "scheme_basics": "Scheme basics",
    "amc_overview": "AMC overview",
    "statement_download": "Statement download",
    "definition": "Definition",
    "aum": "Fund size (AUM)",
}

# Order matters only for the human-readable artifact; chunk IDs are content
# based, so re-ordering here does not change an existing chunk's identity.
FACT_CARD_ORDER: tuple[str, ...] = (
    "expense_ratio",
    "exit_load",
    "lock_in",
    "riskometer",
    "benchmark",
)

_SCHEME_SECTION_NAMES = {
    "key_stats",
    "minimum_investments",
    "exit_load",
    "lock_in",
    "benchmark",
    "scheme_basics",
}

_ABOUT_SECTION_NAMES = {"about", "investment_objective", "objective"}

_STEP_SECTION_NAMES = {"statement_steps", "statement_download"}

_TRIVIAL_BODY_TOKENS = 5


class ChunkError(ConfigError):
    """A loaded document could not be chunked without breaking a gate."""


# --------------------------------------------------------------------- estimates


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate used for the gate.

    ``tiktoken`` is optional in this environment and would add a model-specific
    dependency to a purely structural stage.  Four characters per token is the
    standard English approximation and errs on the safe side for short labels.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- summary


@dataclass
class ChunkSummary:
    total_sources: int = 0
    chunked_sources: int = 0
    total_chunks: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    by_fact_type: dict[str, int] = field(default_factory=dict)
    missing_expected: dict[str, list[str]] = field(default_factory=dict)
    core_missing: dict[str, list[str]] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    token_min: int = 0
    token_max: int = 0
    token_mean: float = 0.0

    @property
    def ok(self) -> bool:
        """Only load/chunk errors and missing *core* facts are fatal."""
        return not self.errors and not self.core_missing

    def describe(self) -> str:
        lines = [
            f"fetchable sources: {self.total_sources}",
            f"chunked: {self.chunked_sources}/{self.total_sources}",
            (
                f"chunks: {self.total_chunks}  "
                f"(tokens min {self.token_min}, max {self.token_max}, "
                f"mean {self.token_mean:.1f})"
            ),
        ]
        if self.by_fact_type:
            counts = ", ".join(
                f"{name} {count}" for name, count in sorted(self.by_fact_type.items())
            )
            lines.append(f"by fact type: {counts}")
        if self.core_missing:
            lines.append("missing expected facts (core, fatal):")
            for source_id, facts in sorted(self.core_missing.items()):
                lines.append(f"  - {source_id}: {', '.join(facts)}")
        support_missing = {
            source_id: facts
            for source_id, facts in self.missing_expected.items()
            if source_id not in self.core_missing
        }
        if support_missing:
            lines.append("missing expected facts (support, warning):")
            for source_id, facts in sorted(support_missing.items()):
                lines.append(f"  - {source_id}: {', '.join(facts)}")
        if self.errors:
            lines.append("errors:")
            for source_id, error in self.errors:
                lines.append(f"  - {source_id}: {error}")
        if self.outputs:
            lines.append("outputs:")
            for path in self.outputs:
                lines.append(f"  - {path}")
        return "\n".join(lines)


# --------------------------------------------------------------------- run_chunk


def run_chunk(
    settings: Settings,
    *,
    force: bool = False,
    registry: SourceRegistry | None = None,
) -> ChunkSummary:
    """Load Phase 1 artifacts and write deterministic chunk artifacts.

    ``force`` is accepted for CLI parity.  Chunking is a pure function of the
    loaded JSON, so every run rewrites the same bytes; there is no separate
    chunk cache to invalidate.
    """
    del force  # deterministic rebuild; kept for a stable command surface

    settings.ensure_data_dirs()
    source_registry = registry or load_registry(settings.sources_path)
    raw_dir = settings.data_dir_path / "01_raw"
    out_dir = settings.data_dir_path / "02_chunks"
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = raw_dir / "manifest.json"
    if not manifest_path.exists():
        raise ConfigError(
            f"no load manifest at {manifest_path}\n"
            "fix: run python -m app.cli load before chunking"
        )
    manifest = _read_manifest(manifest_path)
    if not manifest:
        raise ConfigError(
            f"load manifest is empty or unreadable: {manifest_path}\n"
            "fix: run python -m app.cli load --force"
        )

    entries = fetchable_sources(source_registry)
    summary = ChunkSummary(total_sources=len(entries))
    documents: list[tuple[SourceEntry, dict]] = []

    for entry in entries:
        record = manifest.get(entry.id)
        if record is None:
            summary.errors.append((entry.id, "missing manifest row"))
            continue
        if record.get("http_status") != 200:
            summary.errors.append(
                (entry.id, f"manifest HTTP status is {record.get('http_status')!r}, not 200")
            )
            continue
        path = raw_dir / f"{entry.id}.json"
        if not path.exists():
            summary.errors.append((entry.id, f"missing extracted document: {path}"))
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            summary.errors.append((entry.id, f"could not read {path.name}: {exc}"))
            continue

        problem = _validate_document(entry, document)
        if problem:
            summary.errors.append((entry.id, problem))
            continue
        documents.append((entry, document))

    chunks: list[ChunkRecord] = []
    seen_hashes: set[str] = set()

    for entry, document in documents:
        drafts = _drafts_for_document(entry, document, settings)
        source_chunks: list[ChunkRecord] = []

        for draft in drafts:
            try:
                chunk = _finalize(entry, document, draft, settings)
            except (ChunkError, ValidationError) as exc:
                summary.errors.append((entry.id, str(exc)))
                continue
            if chunk.content_sha256 in seen_hashes:
                continue
            seen_hashes.add(chunk.content_sha256)
            source_chunks.append(chunk)

        if not source_chunks:
            summary.errors.append((entry.id, "no chunks produced"))
            continue

        coverage = _chunk_coverage(source_chunks)
        missing = [fact for fact in entry.expected_facts if fact not in coverage]
        if missing:
            summary.missing_expected[entry.id] = missing
            if entry.role == "core":
                summary.core_missing[entry.id] = missing

        summary.by_source[entry.id] = len(source_chunks)
        summary.chunked_sources += 1
        for chunk in source_chunks:
            summary.by_fact_type[chunk.fact_type] = (
                summary.by_fact_type.get(chunk.fact_type, 0) + 1
            )
        chunks.extend(source_chunks)

    summary.total_chunks = len(chunks)
    if chunks:
        token_counts = [chunk.token_count for chunk in chunks]
        summary.token_min = min(token_counts)
        summary.token_max = max(token_counts)
        summary.token_mean = sum(token_counts) / len(token_counts)

    jsonl_path = out_dir / "chunks.jsonl"
    csv_path = out_dir / "chunks.csv"
    report_path = out_dir / "chunk_report.md"

    _write_text(jsonl_path, _render_jsonl(chunks))
    _write_text(csv_path, _render_csv(chunks))
    _write_text(report_path, _render_report(summary, chunks, source_registry, settings))
    summary.outputs = [jsonl_path, csv_path, report_path]

    return summary


# ------------------------------------------------------------------- validation


def _read_manifest(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return {}
    return {
        record["source_id"]: record
        for record in records
        if isinstance(record, dict) and record.get("source_id")
    }


def _validate_document(entry: SourceEntry, document: dict) -> str | None:
    if not isinstance(document, dict):
        return "extracted document is not a JSON object"
    checks = (
        ("source_id", entry.id),
        ("url", str(entry.url)),
        ("role", entry.role),
        ("source_priority", entry.source_priority),
    )
    for field_name, expected in checks:
        actual = document.get(field_name)
        if actual != expected:
            return f"{field_name} mismatch: expected {expected!r}, found {actual!r}"
    if not document.get("fetched_at"):
        return "document has no scraped_at/fetched_at timestamp"
    if not isinstance(document.get("sections"), list):
        return "document has no sections list"
    violations = policy_violations(document)
    if violations:
        return "document failed the content policy: " + ", ".join(violations)
    return None


def _chunk_coverage(chunks: Iterable[ChunkRecord]) -> set[str]:
    """Facts represented by at least one chunk.

    ``definition`` and ``statement_download`` can be expressed by a chunk's
    *form* (``chunk_type``) even when its ``fact_type`` is more specific, such as
    ``riskometer`` on a definition page.
    """
    covered: set[str] = set()
    for chunk in chunks:
        covered.add(chunk.fact_type)
        if chunk.chunk_type == "definition":
            covered.add("definition")
        if chunk.chunk_type == "steps":
            covered.add("statement_download")
        if chunk.chunk_type == "fact_group" and chunk.fact_type == "minimum_sip":
            covered.add("minimum_lumpsum")
        if chunk.chunk_type == "fact_group" and chunk.fact_type == "scheme_basics":
            covered.add("scheme_basics")
    return covered


# ---------------------------------------------------------------------- drafts


@dataclass
class _Draft:
    fact_label: str
    fact_type: str
    section: str
    chunk_type: str
    body_lines: list[str]


def _drafts_for_document(
    entry: SourceEntry, document: dict, settings: Settings
) -> list[_Draft]:
    if entry.doc_type == "groww_scheme":
        return _scheme_drafts(entry, document, settings)
    if entry.doc_type == "groww_amc":
        return _amc_drafts(entry, document, settings)
    return _article_drafts(entry, document, settings)


# -- scheme pages -----------------------------------------------------------


def _scheme_drafts(
    entry: SourceEntry, document: dict, settings: Settings
) -> list[_Draft]:
    drafts: list[_Draft] = []
    facts = document.get("facts") or {}

    for fact_type in FACT_CARD_ORDER:
        fact = facts.get(fact_type)
        if not fact:
            continue
        drafts.append(
            _Draft(
                fact_label=FACT_LABELS[fact_type],
                fact_type=fact_type,
                section=fact.get("section") or fact_type,
                chunk_type="fact_card",
                body_lines=[f"Value: {_fact_value(fact)}"],
            )
        )

    aum = _section_item_value(document, "key_stats", ("Fund size (AUM)", "AUM"))
    if aum:
        drafts.append(
            _Draft(
                fact_label=FACT_LABELS["aum"],
                fact_type="aum",
                section="key_stats",
                chunk_type="fact_card",
                body_lines=[f"Value: {aum}"],
            )
        )

    minimum_items = _section_items(document, "minimum_investments")
    if not minimum_items:
        minimum_items = [
            fact["text"]
            for fact in (facts.get("minimum_sip"), facts.get("minimum_lumpsum"))
            if fact and fact.get("text")
        ]
    if minimum_items:
        drafts.append(
            _Draft(
                fact_label="Minimum investments",
                fact_type="minimum_sip",
                section="minimum_investments",
                chunk_type="fact_group",
                body_lines=minimum_items,
            )
        )

    basics_items = _section_items(document, "scheme_basics")
    if basics_items:
        drafts.append(
            _Draft(
                fact_label=FACT_LABELS["scheme_basics"],
                fact_type="scheme_basics",
                section="scheme_basics",
                chunk_type="fact_group",
                body_lines=basics_items,
            )
        )

    for section in document.get("sections", []):
        if section.get("name") in _ABOUT_SECTION_NAMES and not _dropped(
            section, settings
        ):
            drafts.extend(
                _narrative_drafts(
                    entry,
                    document,
                    section,
                    settings,
                    fact_type="scheme_basics",
                )
            )

    return drafts


# -- AMC page ---------------------------------------------------------------


def _amc_drafts(
    entry: SourceEntry, document: dict, settings: Settings
) -> list[_Draft]:
    drafts: list[_Draft] = []
    facts = document.get("facts") or {}
    overview = facts.get("amc_overview")

    if overview:
        drafts.append(
            _Draft(
                fact_label=FACT_LABELS["amc_overview"],
                fact_type="amc_overview",
                section="amc_overview",
                chunk_type="fact_card",
                body_lines=[f"Value: {_fact_value(overview)}"],
            )
        )

    key_info = _section_items(document, "amc_overview")
    if key_info:
        drafts.append(
            _Draft(
                fact_label="AMC key information",
                fact_type="amc_overview",
                section="amc_overview",
                chunk_type="fact_group",
                body_lines=key_info,
            )
        )

    for section in document.get("sections", []):
        if section.get("name") in _ABOUT_SECTION_NAMES and not _dropped(
            section, settings
        ):
            drafts.extend(
                _narrative_drafts(
                    entry,
                    document,
                    section,
                    settings,
                    fact_type="amc_overview",
                )
            )

    return drafts


# -- help, AMFI, SEBI articles ---------------------------------------------


def _article_drafts(
    entry: SourceEntry, document: dict, settings: Settings
) -> list[_Draft]:
    drafts: list[_Draft] = []
    facts = document.get("facts") or {}
    sections = [
        section
        for section in document.get("sections", [])
        if not _dropped(section, settings)
    ]
    step_sections = [
        section for section in sections if section.get("name") in _STEP_SECTION_NAMES
    ]

    if step_sections:
        drafts.append(_steps_draft(document, step_sections))

    for section in sections:
        name = section.get("name")
        if name in _STEP_SECTION_NAMES or name == "note":
            # Notes are deliberately excluded: the ELSS note names a PAN
            # password, and the product never asks for or repeats PII.
            continue
        if name == "answer" and step_sections:
            continue
        if name == "lock_in":
            continue  # the fact card below carries the label/value pair
        if name in _ABOUT_SECTION_NAMES:
            drafts.extend(
                _narrative_drafts(
                    entry,
                    document,
                    section,
                    settings,
                    fact_type="definition",
                )
            )
            continue
        if name == "definition":
            drafts.extend(_definition_drafts(entry, document, section, settings))
            continue
        if name in ("answer", "statement_download"):
            drafts.extend(
                _narrative_drafts(
                    entry,
                    document,
                    section,
                    settings,
                    fact_type="definition"
                    if "definition" in entry.expected_facts
                    else "other",
                )
            )
            continue
        drafts.extend(
            _narrative_drafts(
                entry,
                document,
                section,
                settings,
                fact_type="definition"
                if "definition" in entry.expected_facts
                else "other",
            )
        )

    lock_in = facts.get("lock_in")
    if lock_in:
        drafts.append(
            _Draft(
                fact_label=FACT_LABELS["lock_in"],
                fact_type="lock_in",
                section=lock_in.get("section") or "lock_in",
                chunk_type="fact_card",
                body_lines=[f"Value: {_fact_value(lock_in)}"],
            )
        )

    return drafts


def _steps_draft(document: dict, step_sections: list[dict]) -> _Draft:
    answer = [
        clean_text(paragraph)
        for section in document.get("sections", [])
        if section.get("name") == "answer"
        for paragraph in section.get("paragraphs", [])
    ]
    # Drop one-word button labels such as a bare "CAMS".
    answer = [paragraph for paragraph in answer if len(paragraph.split()) > 1]

    steps = [
        clean_text(item)
        for section in step_sections
        for item in section.get("items", [])
    ]
    steps = [step for step in steps if step]

    paragraphs = [
        clean_text(paragraph)
        for section in step_sections
        for paragraph in section.get("paragraphs", [])
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]

    body: list[str] = list(answer)
    if steps:
        body.append("Steps:")
        body.extend(f"{index}. {step}" for index, step in enumerate(steps, 1))
    else:
        body.extend(paragraphs)

    section = step_sections[0].get("name") or "statement_steps"
    return _Draft(
        fact_label=FACT_LABELS["statement_download"],
        fact_type="statement_download",
        section=section,
        chunk_type="steps",
        body_lines=body,
    )


# ------------------------------------------------------------------ windowing


def _narrative_drafts(
    entry: SourceEntry,
    document: dict,
    section: dict,
    settings: Settings,
    *,
    fact_type: str,
) -> list[_Draft]:
    heading = _clean_heading(section.get("heading")) or _section_title(section)
    units = [
        clean_text(text)
        for text in [*section.get("paragraphs", []), *section.get("items", [])]
    ]
    units = [unit for unit in units if unit]
    if not units:
        return []

    if heading:
        units = [unit for unit in units if unit.lower() != heading.lower()]
    if not units:
        return []

    label = heading or "About"
    budget = _body_budget(document, entry, label, settings, narrative=True)
    windows = window_units(
        units,
        budget_tokens=budget,
        overlap_tokens=settings.chunking.overlap_tokens,
    )
    return _window_drafts(label, fact_type, section.get("name") or "narrative", "narrative", windows)


def _definition_drafts(
    entry: SourceEntry,
    document: dict,
    section: dict,
    settings: Settings,
) -> list[_Draft]:
    heading, units = _definition_units(section)
    if not units:
        return []
    if estimate_tokens(" ".join(units)) < _TRIVIAL_BODY_TOKENS:
        return []

    label = _definition_label(heading, document)
    fact_type = _definition_fact_type(entry, label, _document_title(document))
    budget = _body_budget(document, entry, label, settings, narrative=True)
    windows = window_units(
        units,
        budget_tokens=budget,
        overlap_tokens=settings.chunking.overlap_tokens,
    )
    return _window_drafts(label, fact_type, "definition", "definition", windows)


def _window_drafts(
    label: str,
    fact_type: str,
    section: str,
    chunk_type: str,
    windows: list[list[str]],
) -> list[_Draft]:
    drafts: list[_Draft] = []
    for index, window in enumerate(windows, 1):
        window_label = label
        if len(windows) > 1:
            window_label = f"{label} (part {index} of {len(windows)})"
        drafts.append(
            _Draft(
                fact_label=window_label,
                fact_type=fact_type,
                section=section,
                chunk_type=chunk_type,
                body_lines=list(window),
            )
        )
    return drafts


def window_units(
    units: Iterable[str],
    *,
    budget_tokens: int,
    overlap_tokens: int,
) -> list[list[str]]:
    """Pack units into windows bounded by ``budget_tokens``.

    A unit is normally a paragraph, a list item, or a step, so a label/value or
    a numbered instruction is never cut in half.  A unit larger than the budget
    is split on word boundaries.  Overlap carries whole trailing units up to the
    configured token count; it never duplicates a unit more than once.
    """
    budget = max(10, budget_tokens)
    overlap = max(0, overlap_tokens)

    expanded: list[str] = []
    for unit in units:
        cleaned = clean_text(unit)
        if not cleaned:
            continue
        if estimate_tokens(cleaned) > budget:
            expanded.extend(_split_long_unit(cleaned, budget))
        else:
            expanded.append(cleaned)
    if not expanded:
        return []

    windows: list[list[str]] = []
    start = 0
    while start < len(expanded):
        end = start
        used = 0
        while end < len(expanded):
            unit_tokens = estimate_tokens(expanded[end])
            separator = 1 if end > start else 0
            if end > start and used + unit_tokens + separator > budget:
                break
            used += unit_tokens + separator
            end += 1
        if end == start:  # defensive: budget smaller than one token
            end = start + 1
        windows.append(expanded[start:end])
        if end >= len(expanded):
            break

        back = end
        carried = 0
        while back > start + 1:
            candidate = estimate_tokens(expanded[back - 1]) + 1
            if carried + candidate > overlap:
                break
            carried += candidate
            back -= 1
        start = max(back, start + 1)

    return windows


def _split_long_unit(unit: str, budget: int) -> list[str]:
    words = unit.split()
    if len(words) <= 1:
        return [unit]
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and estimate_tokens(candidate) > budget:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _body_budget(
    document: dict,
    entry: SourceEntry,
    fact_label: str,
    settings: Settings,
    *,
    narrative: bool,
) -> int:
    limit = (
        settings.chunking.narrative_max_tokens if narrative else settings.chunking.max_tokens
    )
    limit = min(limit, HARD_MAX_TOKENS)
    header = _compose_text(
        document,
        entry,
        fact_label=fact_label,
        body_lines=[],
        add_header=settings.chunking.add_context_header,
    )
    return max(10, limit - estimate_tokens(header) - 2)


# ------------------------------------------------------------------- composing


def _finalize(
    entry: SourceEntry,
    document: dict,
    draft: _Draft,
    settings: Settings,
) -> ChunkRecord:
    text = _compose_text(
        document,
        entry,
        fact_label=draft.fact_label,
        body_lines=draft.body_lines,
        add_header=settings.chunking.add_context_header,
    )
    token_count = estimate_tokens(text)
    if token_count > HARD_MAX_TOKENS:
        raise ChunkError(
            f"{entry.id}: chunk {draft.fact_type!r} is {token_count} tokens "
            f"(hard maximum {HARD_MAX_TOKENS})"
        )
    if not text.strip():
        raise ChunkError(f"{entry.id}: empty {draft.fact_type!r} chunk")

    violations = policy_violations({"text": text})
    if violations:
        raise ChunkError(
            f"{entry.id}: chunk {draft.fact_type!r} failed the content policy: "
            + ", ".join(violations)
        )

    digest = _sha256(text)
    scheme = document.get("scheme") or {}
    return ChunkRecord(
        chunk_id=f"{entry.id}__{draft.fact_type}__{digest[:8]}",
        doc_id=entry.id,
        source_url=str(entry.url),
        source_title=entry.title,
        source_owner=entry.owner,
        source_priority=entry.source_priority,
        scraped_at=document.get("fetched_at"),
        source_date=document.get("source_date"),
        scheme_slug=scheme.get("scheme_slug") or entry.scheme_slug,
        scheme_name=scheme.get("scheme_name") or entry.scheme_name,
        amc=scheme.get("amc") or CORPUS_AMC,
        category=scheme.get("category") or entry.category,
        plan=scheme.get("plan") or entry.plan,
        option=scheme.get("option") or entry.option,
        fact_type=draft.fact_type,
        section=draft.section,
        chunk_type=draft.chunk_type,
        text=text,
        token_count=token_count,
        content_sha256=digest,
        chunker_version=CHUNKER_VERSION,
    )


def _compose_text(
    document: dict,
    entry: SourceEntry,
    *,
    fact_label: str,
    body_lines: Iterable[str],
    add_header: bool,
) -> str:
    lines: list[str] = []
    if add_header:
        scheme = document.get("scheme") or {}
        scheme_name = scheme.get("scheme_name") or entry.scheme_name
        if scheme_name:
            lines.append(f"Scheme: {scheme_name}")

        context: list[str] = []
        amc = scheme.get("amc") or (CORPUS_AMC if document.get("doc_type") == "groww_amc" else None)
        if amc:
            context.append(f"AMC: {amc}")
        category = scheme.get("category") or entry.category
        if category:
            context.append(f"Category: {category}")
        plan = scheme.get("plan") or entry.plan
        if plan:
            context.append(f"Plan: {plan}")
        option = scheme.get("option") or entry.option
        if option:
            context.append(f"Option: {option}")
        if context:
            lines.append(" | ".join(context))

        if not scheme_name and document.get("doc_type") != "groww_amc":
            title = clean_text(document.get("title"))
            if title:
                lines.append(f"Topic: {title}")

        lines.append(f"Fact: {fact_label}")

    lines.extend(clean_text(line) for line in body_lines if clean_text(line))

    if add_header:
        lines.append(f"Source: {entry.url}")
        scraped = (document.get("fetched_at") or document.get("source_date") or "unknown")[:10]
        lines.append(f"Scraped: {scraped}")

    return "\n".join(lines)


# --------------------------------------------------------------------- helpers


def _fact_value(fact: dict) -> str:
    text = clean_text(fact.get("text"))
    label = clean_text(fact.get("label"))
    if text and label:
        prefix = label + ":"
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix) :].strip()
    return text or clean_text(fact.get("value"))


def _section_items(document: dict, name: str) -> list[str]:
    items: list[str] = []
    for section in document.get("sections", []):
        if section.get("name") != name:
            continue
        items.extend(clean_text(item) for item in section.get("items", []))
    return [item for item in items if item]


def _section_item_value(
    document: dict, section_name: str, labels: tuple[str, ...]
) -> str | None:
    for section in document.get("sections", []):
        if section.get("name") != section_name:
            continue
        for item in section.get("items", []):
            cleaned = clean_text(item)
            for label in labels:
                prefix = label + ":"
                if cleaned.lower().startswith(prefix.lower()):
                    value = cleaned[len(prefix) :].strip()
                    if value:
                        return value
    return None


def _section_title(section: dict) -> str:
    for text in [*section.get("paragraphs", []), *section.get("items", [])]:
        cleaned = clean_text(text)
        if cleaned and len(cleaned.split()) <= 12 and not cleaned.endswith("."):
            return cleaned
    return ""


def _definition_units(section: dict) -> tuple[str, list[str]]:
    heading = _clean_heading(section.get("heading"))
    paragraphs = [clean_text(text) for text in section.get("paragraphs", [])]
    items = [clean_text(text) for text in section.get("items", [])]
    paragraphs = [text for text in paragraphs if text]
    items = [text for text in items if text]

    if not heading and paragraphs:
        first = paragraphs[0]
        if len(first.split()) <= 12 and not first.endswith("."):
            heading = _clean_heading(first)
            paragraphs = paragraphs[1:]

    if heading and paragraphs and paragraphs[0].lower() == heading.lower():
        paragraphs = paragraphs[1:]

    return heading or "Definition", [*paragraphs, *items]


def _clean_heading(value: object) -> str:
    text = clean_text(value)
    text = re.sub(r"\s*:\s*$", "", text)
    return text.strip()


_GENERIC_HEADINGS = {
    "definition",
    "definitions",
    "disclaimer",
    "note",
    "overview",
    "general",
    "introduction",
}

_TITLE_PREFIXES = (
    "AMFI - ",
    "AMFI \u2013 ",
    "SEBI Investor - ",
    "SEBI Investor \u2013 ",
    "Groww - ",
    "Groww \u2013 ",
)


def _document_title(document: dict) -> str:
    title = _clean_heading(document.get("title"))
    for prefix in _TITLE_PREFIXES:
        if title.startswith(prefix):
            return title[len(prefix) :].strip()
    return title


def _definition_label(heading: str, document: dict) -> str:
    """Use the section heading unless it is a generic page label.

    AMFI's TER page calls its factual section "Disclaimer"; embedding that as
    the fact label makes the chunk hard to match.  Fall back to the document
    title ("TER of Mutual Fund Schemes") instead.
    """
    cleaned = _clean_heading(heading)
    if cleaned and cleaned.lower() not in _GENERIC_HEADINGS:
        return cleaned
    return _document_title(document) or cleaned or "Definition"


def _definition_fact_type(entry: SourceEntry, heading: str, title: str) -> str:
    """Classify a concept by its heading/title, not by incidental body words.

    A page titled "A Guide to ELSS" mentions lock-in inside "Benefits of ELSS",
    but that chunk is a definition of ELSS, not the ELSS lock-in fact.  Only the
    explicit lock-in section and fact card should carry ``fact_type=lock_in``.
    """
    expected = set(entry.expected_facts)
    signal = f"{heading} {title}".lower()
    if "riskometer" in expected and re.search(r"\briskometer\b", signal):
        return "riskometer"
    if "expense_ratio" in expected and re.search(
        r"\b(?:expense ratio|ter|total expense ratio)\b", signal
    ):
        return "expense_ratio"
    if "lock_in" in expected and re.search(r"\block[- ]?in\b", signal):
        return "lock_in"
    return "definition"


def _dropped(section: dict, settings: Settings) -> bool:
    drop = {name.lower() for name in settings.chunking.drop_sections}
    name = clean_text(section.get("name")).lower()
    heading = clean_text(section.get("heading")).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", heading).strip("_")
    return name in drop or heading in drop or slug in drop


# ---------------------------------------------------------------------- writers


def _render_jsonl(chunks: Iterable[ChunkRecord]) -> str:
    lines = [
        json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for chunk in chunks
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _render_csv(chunks: Iterable[ChunkRecord]) -> str:
    columns = list(ChunkRecord.model_fields)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for chunk in chunks:
        row = chunk.model_dump(mode="json")
        writer.writerow({column: row.get(column) for column in columns})
    return buffer.getvalue()


def _render_report(
    summary: ChunkSummary,
    chunks: list[ChunkRecord],
    registry: SourceRegistry,
    settings: Settings,
) -> str:
    lines = [
        "# Chunk report",
        "",
        f"- strategy: `{CHUNKER_VERSION}`",
        f"- configured max tokens: fact cards {settings.chunking.max_tokens}, "
        f"narrative {settings.chunking.narrative_max_tokens}, hard {HARD_MAX_TOKENS}",
        f"- token estimate: `ceil(characters / 4)` (deterministic, no tokenizer download)",
        f"- fetchable sources: {summary.total_sources}",
        f"- sources with chunks: {summary.chunked_sources}",
        f"- chunks: {summary.total_chunks}",
        f"- tokens: min {summary.token_min}, max {summary.token_max}, "
        f"mean {summary.token_mean:.1f}",
        "",
        "## Counts by source",
        "",
        "| source_id | role | priority | chunks | missing expected |",
        "|---|---|---:|---:|---|",
    ]

    entries = {entry.id: entry for entry in registry.sources}
    for source_id in sorted(summary.by_source):
        entry = entries.get(source_id)
        role = entry.role if entry else "unknown"
        priority = entry.source_priority if entry else 0
        missing = ", ".join(summary.missing_expected.get(source_id, [])) or "none"
        lines.append(
            f"| `{source_id}` | {role} | {priority} | {summary.by_source[source_id]} | {missing} |"
        )

    lines.extend(["", "## Counts by fact type", "", "| fact_type | chunks |", "|---|---:|"])
    for fact_type in sorted(summary.by_fact_type):
        lines.append(f"| `{fact_type}` | {summary.by_fact_type[fact_type]} |")

    lines.extend(
        [
            "",
            "## Token buckets",
            "",
            "| bucket | chunks |",
            "|---|---:|",
        ]
    )
    buckets = (
        ("1-50", 1, 50),
        ("51-100", 51, 100),
        ("101-200", 101, 200),
        ("201-300", 201, 300),
        ("301+", 301, 10**9),
    )
    for label, low, high in buckets:
        count = sum(1 for chunk in chunks if low <= chunk.token_count <= high)
        lines.append(f"| {label} | {count} |")

    lines.extend(["", "## Missing expected facts", ""])
    if summary.core_missing:
        lines.append("**Core (fatal):**")
        for source_id, facts in sorted(summary.core_missing.items()):
            lines.append(f"- `{source_id}`: {', '.join(facts)}")
    else:
        lines.append("- core: none")

    support_missing = {
        source_id: facts
        for source_id, facts in summary.missing_expected.items()
        if source_id not in summary.core_missing
    }
    if support_missing:
        lines.append("")
        lines.append("**Support (warning):**")
        for source_id, facts in sorted(support_missing.items()):
            lines.append(f"- `{source_id}`: {', '.join(facts)}")
    else:
        lines.append("- support: none")

    if summary.errors:
        lines.extend(["", "## Errors", ""])
        for source_id, error in summary.errors:
            lines.append(f"- `{source_id}`: {error}")

    return "\n".join(lines).rstrip() + "\n"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    temporary.replace(path)
