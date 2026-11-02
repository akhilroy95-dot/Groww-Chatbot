"""Pydantic contracts for artifacts written and read by the pipeline.

Every stage writes one of these shapes, the inspector reads the same shapes, and
tests assert against them. Changing a field here is a schema change: bump the
artifact version in the relevant manifest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

Role = Literal["core", "support", "citation_only"]
FetchMethod = Literal["httpx", "playwright", "manual"]
ChunkType = Literal["fact_card", "fact_group", "narrative", "steps", "definition"]
AnswerStatus = Literal["answered", "refused", "not_found", "clarify"]
RefusalReason = Literal["advice", "performance", "pii", "out_of_scope", "ambiguous"]
AnswerMode = Literal["llm", "extractive", "deterministic"]

FactType = Literal[
    "expense_ratio",
    "exit_load",
    "minimum_sip",
    "minimum_lumpsum",
    "lock_in",
    "riskometer",
    "benchmark",
    "scheme_basics",
    "amc_overview",
    "statement_download",
    "definition",
    "other",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------- sources


class SourceEntry(StrictModel):
    id: str = Field(min_length=1)
    title: str
    url: HttpUrl
    owner: str
    doc_type: str
    role: Role
    source_priority: int = Field(ge=1, le=4)
    scheme_slug: str | None = None
    scheme_name: str | None = None
    category: str | None = None
    plan: str | None = None
    option: str | None = None
    aliases: list[str] = Field(default_factory=list)
    fetch_method: FetchMethod = "httpx"
    expected_facts: list[FactType] = Field(default_factory=list)
    notes: str | None = None


class SourceRegistry(StrictModel):
    version: int = 1
    allowlist_domains: list[str]
    sources: list[SourceEntry]


class ValidationRow(StrictModel):
    source_id: str
    url: str
    role: Role
    http_status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    bytes: int | None = None
    checked_at: str
    status: Literal["ok", "not_fetched", "expected_bot_block", "warn", "fail"]
    notes: str | None = None


# ---------------------------------------------------------------------- chunks


class ChunkRecord(StrictModel):
    chunk_id: str
    doc_id: str
    source_url: str
    source_title: str
    source_owner: str
    source_priority: int = Field(ge=1, le=4)
    scraped_at: str
    source_date: str | None = None
    scheme_slug: str | None = None
    scheme_name: str | None = None
    amc: str | None = None
    category: str | None = None
    plan: str | None = None
    option: str | None = None
    fact_type: str
    section: str
    chunk_type: ChunkType
    text: str = Field(min_length=1)
    token_count: int = Field(ge=1)
    content_sha256: str = Field(min_length=8)
    chunker_version: str


# ------------------------------------------------------------------ embeddings


class EmbeddingManifest(StrictModel):
    embedding_model: str
    revision: str
    dimension: int
    dtype: Literal["float32"]
    normalized: bool
    count: int = Field(ge=0)
    chunks_sha256: str
    embeddings_sha256: str
    config_sha256: str
    created_at: str


# ------------------------------------------------------------------ retrieval


class RetrievedCandidate(StrictModel):
    chunk_id: str
    similarity: float
    adjusted: float
    source_priority: int
    text_preview: str = ""


class RetrievalTrace(StrictModel):
    request_id: str
    query_redacted: str | None = None
    intent: str = "other"
    scheme_slug: str | None = None
    filters: dict[str, object] = Field(default_factory=dict)
    candidates: list[RetrievedCandidate] = Field(default_factory=list)
    selected: list[str] = Field(default_factory=list)
    gate_passed: bool = False
    status: Literal["ok", "clarify", "not_found", "out_of_scope"] = "not_found"
    reason: str | None = None
    query_normalized: str | None = None
    scheme_source: str | None = None
    top_similarity: float | None = None
    context_tokens: int = 0
    timings_ms: dict[str, int] = Field(default_factory=dict)
    notes: str | None = None


# ------------------------------------------------------------------- answers


class AnswerSchema(StrictModel):
    """The exact JSON shape the LLM must return (PRD §11.1). No URL field, ever."""

    status: AnswerStatus
    reason: RefusalReason | None = None
    answer: str
    fact_type: FactType | str = "other"
    scheme_slug: str | None = None
    source_id: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ValidationReport(StrictModel):
    json_ok: bool = False
    citation_ok: bool = False
    scheme_ok: bool = False
    numbers_grounded: bool = False
    sentences: int = 0
    words: int = 0
    pii_echo: bool = False
    performance_claim: bool = False


class Citation(StrictModel):
    source_id: str
    short_label: str
    url: str
    source_date: str


class AnswerResult(StrictModel):
    """What the renderer is allowed to print. One link, one date, always."""

    request_id: str
    status: AnswerStatus
    reason: str | None = None
    body: str
    source_id: str
    citation_url: str
    citation_label: str
    last_updated_from_sources: str
    mode: AnswerMode = "llm"
    scheme_slug: str | None = None
    evidence: dict[str, object] | None = None


class AnswerTrace(StrictModel):
    request_id: str
    prompt_sha256: str
    llm: dict[str, object] = Field(default_factory=dict)
    raw_response: str | None = None
    validation: ValidationReport = Field(default_factory=ValidationReport)
    final: AnswerResult
    prompt: str | None = None
    retrieval_path: str | None = None
    scheme_source: str | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)
