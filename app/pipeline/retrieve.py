"""Phase 5: deterministic retrieval over the fact-card collection.

Contract (implementation.md phase 5, architecture.md 7.3-7.5)::

    question
      -> normalize (expand ER/SIP/ELSS/CAS, strip punctuation)
      -> resolve scheme (alias -> fuzzy -> session -> clarify)
      -> classify fact type
      -> embed the query with the same MiniLM model
      -> Chroma query (candidate_k=20, optional ``where``)
      -> adjust (+0.05 fact match, +0.02 priority-1 source)
      -> MMR (lambda=0.7) -> top_k=8
      -> gate (similarity >= 0.35 AND scheme support)
      -> assemble <=5 context blocks, <=1200 tokens
      -> data/05_retrieval/{request_id}.json

Nothing here calls an LLM.  The trace is written even for clarify/not-found
outcomes, so the inspector can prove *why* an answer was withheld.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

import numpy as np
from pydantic import ValidationError

from ..config import Settings, ConfigError
from ..ids import request_id as new_request_id
from ..logging import redact_text
from ..schemas import RetrievedCandidate, RetrievalTrace, SourceEntry, SourceRegistry
from ..sources import load_registry
from .chunk import estimate_tokens
from .embed import Encoder, _encode, _load_encoder
from .index import _client, _collection_names, _persist_path

# --------------------------------------------------------------------------- types

SCHEME_SCOPED_FACTS: frozenset[str] = frozenset(
    {
        "expense_ratio",
        "exit_load",
        "minimum_sip",
        "minimum_lumpsum",
        "lock_in",
        "riskometer",
        "benchmark",
        "scheme_basics",
    }
)

NON_SCHEME_FACTS: frozenset[str] = frozenset(
    {"amc_overview", "statement_download", "definition", "other"}
)

OTHER_AMC_TOKENS: tuple[str, ...] = (
    "sbi",
    "axis",
    "icici",
    "kotak",
    "aditya birla",
    "nippon",
    "tata",
    "canara",
    "union mutual",
    "idfc",
    "bandhan",
    "motilal",
    "quant",
    "parag parikh",
    "franklin",
    "dsp",
    "mirae",
    "hsbc",
    "invesco",
    "edelweiss",
    "sundaram",
    "whiteoak",
    "navi",
    "pgim",
    "bajaj",
    "360 one",
)

UNSUPPORTED_HDFC_CATEGORIES: tuple[str, ...] = (
    "small cap",
    "multi cap",
    "multicap",
    "focused",
    "value fund",
    "contra",
    "dividend yield",
    "arbitrage",
    "balanced advantage",
    "dynamic asset",
    "liquid",
    "overnight",
    "money market",
    "ultra short",
    "short duration",
    "corporate bond",
    "credit risk",
    "gilt",
    "gold",
    "silver",
    "index fund",
    "nifty",
    "sensex",
    "sectoral",
    "thematic",
    "retirement",
    "children",
    "savings fund",
)

_ABBREVIATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\ber\b"), "expense ratio"),
    (re.compile(r"\bter\b"), "total expense ratio"),
    (re.compile(r"\bsip\b"), "sip systematic investment plan"),
    (re.compile(r"\belss\b"), "elss equity linked savings scheme"),
    (re.compile(r"\bcas\b"), "cas consolidated account statement"),
    (re.compile(r"\baum\b"), "aum assets under management"),
    (re.compile(r"\bamc\b"), "amc asset management company"),
    (re.compile(r"\bnav\b"), "nav net asset value"),
)

_DEFINITION_MARKERS = re.compile(
    r"\b(what is|what are|what does|meaning|define|definition|explain)\b"
)
_INSTRUCTION_LINE = re.compile(
    r"(ignore\s+(all\s+|any\s+)?(previous|prior|above)?\s*instructions?"
    r"|^\s*(system|assistant|developer)\s*:"
    r"|you are now\b"
    r"|answer without a source\b)",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r"<[^>]+>")

# Words that describe the fact or the question, not the scheme.  They are
# stripped before fuzzy matching so "hdfc larg cap expense ratio" is compared
# as "hdfc larg cap", not as a six-token sentence.
_QUESTION_AND_FACT_WORDS = re.compile(
    r"\b("
    r"expense ratio|total expense ratio|expense|"
    r"exit load|exit|"
    r"minimum sip|minimum lumpsum|minimum additional|"
    r"sip|systematic investment plan|lumpsum|lump sum|one time|"
    r"lock in period|lock in|lockin|"
    r"riskometer|risk level|risk|benchmark|index|"
    r"statement|download|tax proof|80c|capital gains?|"
    r"amc|asset management company|fund house|sponsor|"
    r"scheme basics|scheme details|fund details|launch date|isin|registrar|"
    r"mutual fund|"
    r"what|which|who|is|are|the|of|for|do|does|how|can|i|my|get|to|a|an|in|on|"
    r"period|tell|me|about|please"
    r")\b"
)


class RetrievalError(ConfigError):
    """Retrieval could not run without breaking a contract."""


@dataclass(frozen=True)
class Resolution:
    status: Literal["resolved", "ambiguous", "missing", "out_of_scope"]
    slug: str | None = None
    method: str | None = None
    candidates: tuple[tuple[str, float], ...] = ()
    message: str | None = None


@dataclass
class Candidate:
    chunk_id: str
    document: str
    metadata: dict[str, object]
    similarity: float
    adjusted: float
    vector: np.ndarray | None = None

    @property
    def source_priority(self) -> int:
        value = self.metadata.get("source_priority", 4)
        return int(value) if isinstance(value, (int, float, str)) else 4

    @property
    def fact_type(self) -> str:
        value = self.metadata.get("fact_type")
        return str(value) if value is not None else "other"

    @property
    def scheme_slug(self) -> str | None:
        value = self.metadata.get("scheme_slug")
        return str(value) if value is not None else None

    @property
    def content_sha256(self) -> str:
        value = self.metadata.get("content_sha256")
        return str(value) if value is not None else self.chunk_id

    def preview(self, length: int = 220) -> str:
        text = " ".join(self.document.split())
        return text if len(text) <= length else text[: length - 1].rstrip() + "…"


@dataclass
class RetrievalResult:
    request_id: str
    status: Literal["ok", "clarify", "not_found", "out_of_scope"]
    intent: str
    normalised: str
    scheme_slug: str | None = None
    scheme_source: str | None = None
    filters: dict[str, object] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    selected: list[Candidate] = field(default_factory=list)
    context_chunks: list[Candidate] = field(default_factory=list)
    context_blocks: list[str] = field(default_factory=list)
    context_text: str = ""
    context_tokens: int = 0
    gate_passed: bool = False
    top_similarity: float | None = None
    reason: str | None = None
    message: str | None = None
    trace_path: Path | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.gate_passed


# ------------------------------------------------------------------ normalization


def normalize_query(text: str) -> str:
    """Lowercase, expand the domain abbreviations, and strip punctuation."""
    value = (text or "").lower().strip()
    value = value.replace("&", " and ").replace("-", " ")
    for pattern, replacement in _ABBREVIATIONS:
        value = pattern.sub(f" {replacement} ", value)
    value = re.sub(r"[^a-z0-9%.\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def classify_intent(normalised: str) -> str:
    """Rule-based fact classifier.  Order matters; the first match wins."""
    q = normalised
    if not q:
        return "other"

    definition_like = bool(_DEFINITION_MARKERS.search(q))

    # Definition questions must not be mistaken for account-action questions.
    if definition_like and ("cas" in q or "consolidated account statement" in q):
        return "definition"

    if any(
        token in q
        for token in (
            "statement",
            "tax proof",
            "80c",
            "capital gain",
            "capital gains",
            "download",
            "how to get",
            "where can i get",
            "how do i get",
        )
    ):
        return "statement_download"

    if "expense ratio" in q or "total expense ratio" in q or "expense" in q:
        return "expense_ratio"
    if "exit load" in q or "exit" in q:
        return "exit_load"
    if "lock in" in q or "lockin" in q or "lock in period" in q:
        return "lock_in"
    if "riskometer" in q or "risk level" in q or "risk" in q:
        return "riskometer"
    if "benchmark" in q or "index" in q:
        return "benchmark"
    if "minimum sip" in q or "sip" in q or "systematic investment" in q:
        return "minimum_sip"
    if "lumpsum" in q or "lump sum" in q or "one time" in q:
        return "minimum_lumpsum"
    if "amc" in q or "fund house" in q or "sponsor" in q or "mutual fund" in q:
        return "amc_overview"
    if any(
        token in q
        for token in (
            "launch date",
            "isin",
            "registrar",
            "scheme name",
            "scheme basics",
            "fund details",
            "scheme details",
        )
    ):
        return "scheme_basics"
    if definition_like:
        return "definition"
    return "other"


def is_definition_question(normalised: str) -> bool:
    return bool(_DEFINITION_MARKERS.search(normalised))


def _scheme_phrase(normalised: str) -> str:
    """Strip fact and question words, leaving the part that names a scheme."""
    value = _QUESTION_AND_FACT_WORDS.sub(" ", normalised)
    return re.sub(r"\s+", " ", value).strip()


# ---------------------------------------------------------------- scheme resolution


def _basic_alias(text: str) -> str:
    value = (text or "").lower().replace("-", " ").replace("&", " and ")
    value = re.sub(r"[^a-z0-9%.\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _phrase_in(query: str, phrase: str) -> bool:
    if not phrase:
        return False
    return re.search(
        rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", query
    ) is not None


def _scheme_alias_table(registry: SourceRegistry) -> list[tuple[str, str]]:
    """Return ``(alias, slug)`` pairs for every core scheme."""
    pairs: list[tuple[str, str]] = []
    for entry in registry.sources:
        if entry.role != "core" or not entry.scheme_slug:
            continue
        slug = entry.scheme_slug
        aliases = list(entry.aliases)
        if entry.scheme_name:
            aliases.append(entry.scheme_name)
        if entry.category:
            aliases.append(entry.category)
        aliases.append(slug.replace("-", " "))
        for alias in aliases:
            cleaned = _basic_alias(alias)
            if cleaned:
                pairs.append((cleaned, slug))
    # Longest first so "hdfc large cap fund" beats "large cap".
    return sorted(set(pairs), key=lambda pair: (-len(pair[0]), pair[0]))


def resolve_scheme(
    query: str,
    registry: SourceRegistry,
    *,
    normalised: str | None = None,
    session_last_scheme: str | None = None,
    intent: str | None = None,
) -> Resolution:
    """Resolve a scheme phrase with alias -> fuzzy -> session fallbacks."""
    normalised = normalised if normalised is not None else normalize_query(query)
    intent = intent or classify_intent(normalised)

    explicit_scheme = any(
        entry.role == "core" and entry.scheme_slug for entry in registry.sources
    ) and "hdfc" in normalised
    other_amc = tuple(token for token in OTHER_AMC_TOKENS if _phrase_in(normalised, token))

    # Exact alias / scheme-name / slug phrase.
    alias_hits: list[tuple[int, str, str]] = []
    for alias, slug in _scheme_alias_table(registry):
        if _phrase_in(normalised, alias):
            alias_hits.append((len(alias), slug, alias))

    if alias_hits:
        longest = max(length for length, _, _ in alias_hits)
        winners = {slug for length, slug, _ in alias_hits if length == longest}
        if len(winners) == 1:
            slug = next(iter(winners))
            return Resolution("resolved", slug, "alias", tuple((s, 1.0) for s in winners))
        return Resolution(
            "ambiguous",
            method="alias",
            candidates=tuple((slug, 1.0) for slug in sorted(winners)),
            message="More than one HDFC scheme matched. Which scheme do you mean?",
        )

    # Fuzzy fallback over aliases.  token_sort_ratio tolerates word order and a
    # one-word typo but penalises missing scheme words; stripping the
    # fact/question words avoids matching aliases on generic tokens alone.
    fuzzy_scores: list[tuple[float, str]] = []
    try:
        from rapidfuzz import fuzz
    except Exception:  # pragma: no cover - rapidfuzz is in requirements
        from difflib import SequenceMatcher

        def _ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100

        score = _ratio
    else:
        score = fuzz.token_sort_ratio

    scheme_phrase = _scheme_phrase(normalised)
    for alias, slug in _scheme_alias_table(registry):
        if not scheme_phrase or len(alias) < 5:
            continue
        value = float(score(scheme_phrase, alias))
        if value >= 85.0:
            fuzzy_scores.append((value, slug))

    if fuzzy_scores:
        fuzzy_scores.sort(reverse=True)
        best_score, best_slug = fuzzy_scores[0]
        close = {slug for value, slug in fuzzy_scores if best_score - value <= 3.0}
        if len(close) == 1:
            return Resolution(
                "resolved",
                best_slug,
                "fuzzy",
                tuple((slug, round(value / 100, 4)) for value, slug in fuzzy_scores[:5]),
            )
        return Resolution(
            "ambiguous",
            method="fuzzy",
            candidates=tuple(
                (slug, round(value / 100, 4)) for value, slug in fuzzy_scores[:5]
            ),
            message="More than one HDFC scheme matched. Which scheme do you mean?",
        )

    # Session inheritance only for scheme-scoped facts.
    if session_last_scheme and intent in SCHEME_SCOPED_FACTS:
        if any(
            entry.scheme_slug == session_last_scheme and entry.role == "core"
            for entry in registry.sources
        ):
            return Resolution("resolved", session_last_scheme, "session")

    # The only indexed lock-in fact belongs to ELSS.
    if intent == "lock_in" and not session_last_scheme:
        slug = next(
            (
                entry.scheme_slug
                for entry in registry.sources
                if entry.role == "core" and entry.scheme_slug and "elss" in entry.scheme_slug
            ),
            None,
        )
        if slug:
            return Resolution("resolved", slug, "fact_type")

    if other_amc and not explicit_scheme:
        return Resolution(
            "out_of_scope",
            method="other_amc",
            message=(
                "I can only answer factual questions about the four HDFC schemes "
                "indexed for this demo (Large Cap, Flexi Cap, ELSS, and Mid Cap)."
            ),
        )

    if explicit_scheme and any(
        _phrase_in(normalised, token) for token in UNSUPPORTED_HDFC_CATEGORIES
    ):
        return Resolution(
            "out_of_scope",
            method="unsupported_scheme",
            message=(
                "That HDFC scheme is not in this demo's index. I can answer for "
                "HDFC Large Cap, Flexi Cap, ELSS, and Mid Cap direct-growth plans."
            ),
        )

    return Resolution(
        "missing",
        method=None,
        message="Which HDFC scheme do you mean: Large Cap, Flexi Cap, ELSS, or Mid Cap?",
    )


# ------------------------------------------------------------------ adjustments/MMR


def _adjust(candidates: Sequence[Candidate], *, intent: str, settings: Settings) -> None:
    fact_boost = settings.retrieval.fact_type_boost
    priority_bonus = settings.retrieval.source_priority_bonus
    for candidate in candidates:
        candidate.adjusted = float(candidate.similarity)
        if candidate.fact_type == intent:
            candidate.adjusted += fact_boost
        if candidate.source_priority == 1:
            candidate.adjusted += priority_bonus
        candidate.adjusted = round(candidate.adjusted, 6)


def _mmr(
    candidates: list[Candidate],
    *,
    top_k: int,
    lambda_: float,
) -> list[Candidate]:
    """Maximal Marginal Relevance with cosine similarity over candidate vectors."""
    if not candidates or top_k <= 0:
        return []
    remaining = list(range(len(candidates)))
    selected: list[int] = []

    while remaining and len(selected) < top_k:
        best_index: int | None = None
        best_score = float("-inf")
        for index in remaining:
            relevance = candidates[index].adjusted
            if selected:
                redundancy = 0.0
                vector = candidates[index].vector
                if vector is not None:
                    for chosen in selected:
                        other = candidates[chosen].vector
                        if other is not None:
                            redundancy = max(redundancy, float(np.dot(vector, other)))
                score = lambda_ * relevance - (1.0 - lambda_) * redundancy
            else:
                score = relevance
            if score > best_score:
                best_score = score
                best_index = index
        if best_index is None:  # pragma: no cover - defensive
            break
        selected.append(best_index)
        remaining.remove(best_index)

    return [candidates[index] for index in selected]


# -------------------------------------------------------------------- context text


def _clean_chunk_text(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _HTML_TAG.sub("", raw_line).strip()
        if not line:
            continue
        if line.lower().startswith(("source:", "scraped:")):
            continue
        if _INSTRUCTION_LINE.search(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _context_block(index: int, candidate: Candidate) -> str:
    metadata = candidate.metadata
    scheme = metadata.get("scheme_name") or metadata.get("amc") or "HDFC Mutual Fund"
    fact_type = metadata.get("fact_type") or "other"
    section = metadata.get("section") or "unknown"
    source_url = metadata.get("source_url") or ""
    scraped_at = metadata.get("source_date") or metadata.get("scraped_at") or ""
    text = _clean_chunk_text(candidate.document)
    return (
        f"[S{index}]\n"
        f"scheme: {scheme}\n"
        f"fact_type: {fact_type}\n"
        f"section: {section}\n"
        f"source_url: {source_url}\n"
        f"scraped_at: {scraped_at}\n"
        f"text: {text}\n"
        f"[/S{index}]"
    )


def assemble_context(
    chunks: Sequence[Candidate],
    *,
    max_tokens: int,
    max_blocks: int = 5,
) -> tuple[list[str], str, int]:
    """Build numbered context blocks, deduped and within the token budget."""
    blocks: list[str] = []
    seen_hashes: set[str] = set()
    total = 0
    for candidate in chunks:
        if len(blocks) >= max_blocks:
            break
        if candidate.content_sha256 in seen_hashes:
            continue
        block = _context_block(len(blocks) + 1, candidate)
        tokens = estimate_tokens(block)
        if total + tokens > max_tokens:
            continue
        blocks.append(block)
        seen_hashes.add(candidate.content_sha256)
        total += tokens
    text = "[CONTEXT]\n" + "\n".join(blocks) + ("\n" if blocks else "") + "[/CONTEXT]"
    return blocks, text, total


# --------------------------------------------------------------------- Chroma I/O


def _open_collection(client, settings: Settings):
    name = settings.vector_db.collection
    if name not in _collection_names(client):
        raise ConfigError(
            f"vector collection {name!r} does not exist\n"
            "fix: python -m app.cli index"
        )
    collection = client.get_collection(name)
    metadata = collection.metadata or {}
    expected = settings.embedding
    stored = (
        metadata.get("embedding_model"),
        metadata.get("embedding_revision"),
        metadata.get("dimension"),
        metadata.get("distance", metadata.get("hnsw:space")),
    )
    wanted = (
        expected.model_name,
        expected.revision,
        expected.dimension,
        settings.vector_db.distance,
    )
    if stored != wanted:
        raise ConfigError(
            f"collection {name!r} was built with {stored}, but config says {wanted}\n"
            "fix: python -m app.cli index --rebuild"
        )
    return collection


def _vectors_by_id(settings: Settings) -> dict[str, np.ndarray]:
    base = settings.data_dir_path / "03_embeddings"
    ids_path = base / "embedding_ids.json"
    matrix_path = base / "embeddings.npy"
    if not ids_path.exists() or not matrix_path.exists():
        return {}
    try:
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
        matrix = np.load(matrix_path, allow_pickle=False)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(ids, list) or matrix.ndim != 2 or len(ids) != matrix.shape[0]:
        return {}
    return {
        str(chunk_id): matrix[index].astype(np.float32, copy=False)
        for index, chunk_id in enumerate(ids)
    }


def _query(
    collection,
    *,
    vector: np.ndarray,
    candidate_k: int,
    where: dict[str, object] | None,
    vector_lookup: dict[str, np.ndarray],
) -> list[Candidate]:
    kwargs: dict[str, object] = {
        "query_embeddings": [vector.tolist()],
        "n_results": max(1, candidate_k),
        "include": ["documents", "metadatas", "distances", "embeddings"],
    }
    if where:
        kwargs["where"] = where
    try:
        response = collection.query(**kwargs)
        embeddings = response.get("embeddings")
    except Exception:
        kwargs["include"] = ["documents", "metadatas", "distances"]
        response = collection.query(**kwargs)
        embeddings = None

    ids = (response.get("ids") or [[]])[0]
    documents = (response.get("documents") or [[]])[0]
    metadatas = (response.get("metadatas") or [[]])[0]
    distances = (response.get("distances") or [[]])[0]
    vectors = (embeddings or [[]])[0] if embeddings is not None else []

    candidates: list[Candidate] = []
    for position, chunk_id in enumerate(ids):
        metadata = dict(metadatas[position] or {}) if position < len(metadatas) else {}
        document = documents[position] if position < len(documents) else ""
        distance = float(distances[position]) if position < len(distances) else 1.0
        similarity = round(1.0 - distance, 6)
        candidate_vector = None
        if position < len(vectors) and vectors[position] is not None:
            candidate_vector = np.asarray(vectors[position], dtype=np.float32)
        elif str(chunk_id) in vector_lookup:
            candidate_vector = vector_lookup[str(chunk_id)]
        candidates.append(
            Candidate(
                chunk_id=str(chunk_id),
                document=str(document or ""),
                metadata=metadata,
                similarity=similarity,
                adjusted=similarity,
                vector=candidate_vector,
            )
        )
    return candidates


# ------------------------------------------------------------------------ trace I/O


def _write_trace(
    settings: Settings,
    *,
    request_id: str,
    query: str,
    result: RetrievalResult,
) -> Path:
    directory = settings.data_dir_path / "05_retrieval"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request_id}.json"
    trace = RetrievalTrace(
        request_id=request_id,
        query_redacted=redact_text(query) if query else None,
        intent=result.intent,
        scheme_slug=result.scheme_slug,
        filters=result.filters,
        candidates=[
            RetrievedCandidate(
                chunk_id=candidate.chunk_id,
                similarity=candidate.similarity,
                adjusted=candidate.adjusted,
                source_priority=candidate.source_priority,
                text_preview=candidate.preview(),
            )
            for candidate in result.candidates
        ],
        selected=[candidate.chunk_id for candidate in result.context_chunks],
        gate_passed=result.gate_passed,
        status=result.status,
        reason=result.reason,
        query_normalized=redact_text(result.normalised) if result.normalised else None,
        scheme_source=result.scheme_source,
        top_similarity=result.top_similarity,
        context_tokens=result.context_tokens,
        timings_ms=result.timings_ms,
        notes=result.message,
    )
    payload = json.dumps(trace.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _finish(
    settings: Settings,
    *,
    request_id: str,
    query: str,
    result: RetrievalResult,
    started: float,
) -> RetrievalResult:
    result.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
    result.trace_path = _write_trace(
        settings, request_id=request_id, query=query, result=result
    )
    return result


# ------------------------------------------------------------------------ retrieve


def retrieve(
    settings: Settings,
    *,
    query: str,
    registry: SourceRegistry | None = None,
    session_last_scheme: str | None = None,
    request_id: str | None = None,
    encoder: Encoder | None = None,
    write_trace: bool = True,
) -> RetrievalResult:
    """Run the full retrieval pipeline and return evidence plus a stored trace."""
    started = time.perf_counter()
    request_id = request_id or new_request_id()
    registry = registry or load_registry(settings.sources_path)
    question = (query or "").strip()
    normalised = normalize_query(question)
    intent = classify_intent(normalised)
    definition_question = is_definition_question(normalised)

    result = RetrievalResult(
        request_id=request_id,
        status="not_found",
        intent=intent,
        normalised=normalised,
    )

    if not question:
        result.status = "clarify"
        result.reason = "empty_query"
        result.message = "Please ask a question about an HDFC mutual fund scheme."
        return (
            _finish(
                settings,
                request_id=request_id,
                query=question,
                result=result,
                started=started,
            )
            if write_trace
            else result
        )

    resolution = resolve_scheme(
        question,
        registry,
        normalised=normalised,
        session_last_scheme=session_last_scheme,
        intent=intent,
    )
    result.scheme_slug = resolution.slug
    result.scheme_source = resolution.method

    if resolution.status == "out_of_scope":
        result.status = "out_of_scope"
        result.reason = "out_of_scope"
        result.message = resolution.message
        return _finish(
            settings, request_id=request_id, query=question, result=result, started=started
        ) if write_trace else result

    if resolution.status == "ambiguous":
        result.status = "clarify"
        result.reason = "ambiguous"
        result.message = resolution.message
        return _finish(
            settings, request_id=request_id, query=question, result=result, started=started
        ) if write_trace else result

    # A scheme-scoped question with no scheme and no session is clarified before
    # any embedding work: guessing a scheme is worse than asking one question.
    if (
        resolution.status == "missing"
        and intent in SCHEME_SCOPED_FACTS
        and not definition_question
    ):
        result.status = "clarify"
        result.reason = "missing_scheme"
        result.message = resolution.message
        return _finish(
            settings, request_id=request_id, query=question, result=result, started=started
        ) if write_trace else result

    filters = _filters_for(
        intent=intent,
        scheme_slug=resolution.slug,
        definition_question=definition_question,
    )
    result.filters = filters

    # Embed the query with the same model that built the collection.
    encode_started = time.perf_counter()
    active_encoder = encoder or _load_encoder(settings)
    vector = _encode(active_encoder, [normalised], settings)[0]
    result.timings_ms["embed"] = int((time.perf_counter() - encode_started) * 1000)

    query_started = time.perf_counter()
    client = _client(_persist_path(settings))
    collection = _open_collection(client, settings)
    candidates = _query(
        collection,
        vector=vector,
        candidate_k=settings.retrieval.candidate_k,
        where=filters or None,
        vector_lookup=_vectors_by_id(settings),
    )
    result.timings_ms["chroma"] = int((time.perf_counter() - query_started) * 1000)

    _adjust(candidates, intent=intent, settings=settings)
    candidates.sort(key=lambda candidate: candidate.adjusted, reverse=True)
    result.candidates = candidates

    selected = _mmr(
        candidates,
        top_k=settings.retrieval.top_k,
        lambda_=settings.retrieval.mmr.lambda_ if settings.retrieval.mmr.enabled else 1.0,
    )

    # Gate 1: scheme support for scheme-scoped facts.
    if resolution.slug and intent in SCHEME_SCOPED_FACTS:
        selected = [
            candidate for candidate in selected if candidate.scheme_slug == resolution.slug
        ]

    top_similarity = max((candidate.similarity for candidate in selected), default=None)
    result.top_similarity = round(top_similarity, 6) if top_similarity is not None else None

    if top_similarity is None:
        result.status = "not_found"
        result.reason = "no_candidates"
        result.message = "I could not find a matching fact in the indexed sources."
        return _finish(
            settings, request_id=request_id, query=question, result=result, started=started
        ) if write_trace else result

    if top_similarity < settings.retrieval.min_similarity:
        result.status = "not_found"
        result.reason = "below_similarity"
        result.message = "I could not find a confident match in the indexed sources."
        return _finish(
            settings, request_id=request_id, query=question, result=result, started=started
        ) if write_trace else result

    if intent == "other" and top_similarity < 0.5:
        result.status = "not_found"
        result.reason = "unsupported_fact"
        result.message = "That fact is not in the indexed sources for this demo."
        return _finish(
            settings, request_id=request_id, query=question, result=result, started=started
        ) if write_trace else result

    # Gate 2: keep only candidates above the threshold.
    gated = [
        candidate
        for candidate in selected
        if candidate.similarity >= settings.retrieval.min_similarity
    ]

    blocks, context_text, context_tokens = assemble_context(
        gated, max_tokens=settings.retrieval.context_max_tokens
    )
    result.selected = gated
    result.context_chunks = gated[: len(blocks)]
    result.context_blocks = blocks
    result.context_text = context_text
    result.context_tokens = context_tokens
    result.gate_passed = bool(blocks)

    if not blocks:
        result.status = "not_found"
        result.reason = "no_context"
        result.message = "I could not assemble a grounded context for this question."
        return _finish(
            settings, request_id=request_id, query=question, result=result, started=started
        ) if write_trace else result

    result.status = "ok"
    result.reason = None
    result.message = None
    return _finish(
        settings, request_id=request_id, query=question, result=result, started=started
    ) if write_trace else result


# ------------------------------------------------------------------------ filters


def _filters_for(
    *,
    intent: str,
    scheme_slug: str | None,
    definition_question: bool,
) -> dict[str, object]:
    if intent == "statement_download":
        return {"fact_type": {"$in": ["statement_download", "definition"]}}
    if intent == "definition":
        return {"fact_type": "definition"}
    if intent == "amc_overview":
        return {"doc_id": "groww_amc_hdfc"}
    if definition_question and scheme_slug is None:
        if intent in {"other"}:
            return {"fact_type": "definition"}
        return {"fact_type": {"$in": [intent, "definition"]}}
    if scheme_slug and intent in SCHEME_SCOPED_FACTS:
        return {"scheme_slug": scheme_slug}
    return {}


# -------------------------------------------------------------------- public helpers


def trace_for(settings: Settings, request_id: str) -> RetrievalTrace | None:
    """Read a stored retrieval trace (used by the inspector and tests)."""
    path = settings.data_dir_path / "05_retrieval" / f"{request_id}.json"
    if not path.exists():
        return None
    try:
        return RetrievalTrace(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def scheme_entry(registry: SourceRegistry, slug: str | None) -> SourceEntry | None:
    if not slug:
        return None
    return next(
        (
            entry
            for entry in registry.sources
            if entry.scheme_slug == slug and entry.role == "core"
        ),
        None,
    )
