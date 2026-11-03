# Architecture — Groww MF Facts Assistant

**System architecture for the facts-only RAG pipeline defined in `PRD.md`**

| Field | Value |
|---|---|
| Document | Software Architecture Document (SAD) |
| Version | 1.0 |
| Date | 2026-11-01 |
| Status | Derived from `PRD.md` v1.0; ready to implement |
| Source of truth | `PRD.md` (requirements), `Docs/Problemstatement.txt` (original ask) |
| Target runtime | Local, single-user: Python 3.11 + Streamlit + ChromaDB |
| Corpus | 21 public URLs (5 core, 9 support, 7 citation-only) |

This document describes **how the system is built**, not what it must do. Convention used throughout: `PRD §x` refers to a section of `PRD.md`; a bare `§x.y` refers to a section of this document.

---

## 1. Purpose and reading guide

| Reader | Read |
|---|---|
| Implementer | §4–§9 (components, stages, runtime flow, LLM, guardrails) |
| Reviewer / evaluator | §2, §7, §9, §20 (drivers, query plane, guardrails, traceability) |
| Demo presenter | §11.3, §14, §22 (inspector, run topology, build order) |
| Maintainer | §6, §10, §16, §18 (data contracts, config, testing, extension points) |

Conventions used in this document:

- **Stage** = an offline batch step that writes a durable artifact (`data/0N_*`).
- **Service** = an in-process module with a typed interface, not a network service.
- **Artifact** = a file on disk that the next stage reads and the inspector renders.
- **Trace** = the per-request record joining retrieval and generation for one query.

---

## 2. Architectural drivers

These are the forces that shape the design. Each maps to a PRD requirement; none is optional.

| # | Driver | Architectural consequence |
|---|---|---|
| A1 | Every RAG stage must be independently inspectable (PRD §6.6, §7.1) | Stage-separated batch pipeline; immutable versioned artifacts; no in-memory-only work; CLI per stage |
| A2 | Exactly one citation per answer, never hallucinated (PRD §6.4, §11.3) | Citations resolved from a registry by `source_id`; LLM never emits URLs; renderer is the only place a URL is written |
| A3 | No advice, no performance claims, no PII (PRD §12) | Layered guardrails G1–G6 (input, scope, evidence, prompt, output, render); performance sections dropped at ingestion; deterministic refusals |
| A4 | Nothing may be asserted that is not in the cited chunk (PRD §12.4) | Context-only prompt + numeric grounding validator + extractive fallback |
| A5 | Models and vector store are configuration, not code (PRD §9) | `LLMClient` protocol; adapter per provider; embedding model name embedded in collection name; config hash in manifest |
| A6 | Corpus is tiny but drifting (5–14 pages, values change) (PRD §17) | Idempotent re-runs, content-hash caching, cheap full re-index rather than incremental complexity |
| A7 | Demo must run on a laptop in <3 minutes (PRD §15, Appendix I) | Single process, local Chroma, CPU embeddings, small top-k, no external queue/cache/server |
| A8 | HDFC pages bot-block 403 (PRD Appendix A) | Role separation: `citation_only` sources are in the registry but never fetched or embedded |

**Design stance:** prefer a pipeline of small pure-ish modules over a framework. LangChain/LlamaIndex are deliberately not used — the assignment is to demonstrate each stage explicitly, and a framework would hide the very seams the inspector must show.

---

## 3. System context (C4 level 1)

```text
                        ┌────────────────────────────┐
                        │  Retail investor /         │
                        │  Groww support reviewer    │
                        └──────────────┬─────────────┘
                                       │ HTTPS (browser, localhost)
                                       ▼
                 ┌─────────────────────────────────────────┐
                 │  Groww MF Facts Assistant (local app)   │
                 │  Streamlit UI + pipeline + Chroma       │
                 └───┬───────────┬──────────────┬──────────┘
                     │           │              │
      HTTPS GET      │           │ HTTPS POST   │ local FS
      (build time)   │           │ (query time) │
                     ▼           ▼              ▼
        ┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
        │ Public sources   │ │ LLM provider │ │ ./data artifacts │
        │ groww.in         │ │ Groq (default│ │ raw, chunks,     │
        │ amfiindia.com    │ │ Gemini/OpenAI│ │ vectors, traces  │
        │ sebi.gov.in      │ │ Anthropic/   │ │                  │
        │ investor.sebi... │ │ Mistral)     │ │                  │
        │ hdfcfund.com*    │ └──────────────┘ └──────────────────┘
        └──────────────────┘
          * citation-only, bot-403
```

Actors and externals:

| Element | Type | Interaction |
|---|---|---|
| End user | Person | Asks one factual question at a time; reads one answer, one link, one date |
| Streamlit UI | Process (localhost) | Only entry point; renders answer card and inspector |
| Public source sites | External HTTP | Read-only GET at build time; 2 s delay; robots.txt respected |
| LLM provider | External HTTPS API | One completion per query; selected by `LLM_PROVIDER` in `.env` |
| Local filesystem | Storage | All artifacts; no database server, no object store |
| ChromaDB | Embedded library | Persistent local collection inside `data/04_vector_db/chroma` |

Trust boundaries:

1. **Untrusted:** user query, retrieved page text, LLM output. All three are validated before rendering.
2. **Trusted:** `config/sources.yaml` registry, `config/config.yaml`, `.env` secrets.
3. **Never crosses a boundary outward:** raw PII, API keys, absolute local paths in UI output.

*Out of scope for this system:* user accounts, folio/account data, order placement, portfolio APIs, multi-tenant hosting, scheduled production crawling.

---

## 4. Container and component view (C4 level 2)

One Python process hosts everything; containers here are logical modules.

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                           Streamlit app (app/ui/)                            │
│  Ask view ────────────────┐        Pipeline Inspector view (local only)     │
└───────────┬───────────────┴───────────────────────┬──────────────────────────┘
            │                                       │ reads
            ▼                                       ▼
┌───────────────────────────────┐        ┌─────────────────────────────────────┐
│ QueryService (app/service.py) │        │ ArtifactReader (app/inspector.py)   │
│ orchestrates the query path   │        │ pure read-only file/collection view │
└───┬───────┬────────┬──────────┘        └─────────────────────────────────────┘
    │       │        │
    │       │        └──────────────▶ GenerationService ──▶ LLMClient (adapter)
    │       │                              ▲                     │
    │       │                              │                     ▼
    │       │                        Guardrails (app/guardrails/)  provider API
    │       ▼                              ▲
    │  RetrievalService ───────────────────┘
    │      │  │
    │      │  └──▶ Embedder (app/pipeline/embed.py, shared with build)
    │      ▼
    │  VectorStore (app/pipeline/index.py → Chroma)
    │
    └──▶ CitationResolver (registry from config/sources.yaml)

     BUILD-TIME PIPELINE (CLI, app/cli.py) ── runs before the UI is useful
     ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
     │ P0 sources │─▶│ P1 load  │─▶│ P2 chunk │─▶│ P3 embed │─▶│ P4 index │  │ P7 eval  │
     │ validate   │  │ raw+JSON │  │ jsonl    │  │ npy+ids  │  │ chroma   │  │ golden   │
     └────────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

### 4.1 Component inventory

| Component | Module (planned) | Responsibility | Depends on |
|---|---|---|---|
| Config loader | `app/config.py` | Merge `config.yaml` + `.env` + env overrides; validate; expose typed settings; compute `config_sha256` | PyYAML, pydantic |
| Source registry | `app/pipeline/sources.py` | Parse/validate `sources.yaml`; enforce domain allowlist; produce registry dict keyed by `source_id` | config |
| Fetcher | `app/pipeline/load.py` | HTTP GET with UA/timeout/retry/delay/robots; optional Playwright; raw HTML cache | httpx, (playwright) |
| Extractor | `app/pipeline/load.py` | HTML → typed sections; drop denylisted sections; record provenance | selectolax/bs4 |
| Chunker | `app/pipeline/chunk.py` | Sections → fact cards + narrative windows; metadata; content hashes; report | tiktoken |
| Embedder | `app/pipeline/embed.py` | Chunk text → normalized 384-d vectors; cache by hash; manifest | sentence-transformers |
| Indexer | `app/pipeline/index.py` | Upsert vectors + flat metadata into Chroma; prune stale IDs; stats | chromadb |
| Query normalizer | `app/guardrails/intent.py` | Alias expansion, scheme resolution, fact-type classification | rapidfuzz |
| VectorStore | `app/pipeline/index.py` | `query(embedding, k, where)` → scored chunks | chroma |
| RetrievalService | `app/pipeline/retrieve.py` | Candidate retrieval → fact-type boost → MMR → relevance gate → context build → trace | Embedder, VectorStore, intent |
| PromptBuilder | `app/pipeline/generate.py` | System prompt + numbered context blocks + question; token budget enforcement | config |
| LLMClient | `app/llm/*.py` | Provider-agnostic `complete(messages, **opts) -> LLMResponse` | provider SDK / httpx |
| Generator | `app/pipeline/generate.py` | Call LLM, parse JSON, retry once, extractive fallback | LLMClient, PromptBuilder |
| Validators | `app/pipeline/validate.py` | Schema, citation, scheme, numeric grounding, length, banned terms, PII echo | guardrails |
| CitationResolver | `app/guardrails/citations.py` | `source_id` → registry URL + label + source date; allowlist check | Source registry |
| Refusal engine | `app/guardrails/intent.py` + `output_checks.py` | Deterministic refusal/clarify/not-found templates | config, registry |
| Renderer | `app/service.py` | Assemble answer body + one Source line + Last-updated line | all of the above |
| QueryService | `app/service.py` | Single entry point: `answer(question, session) -> AnswerResult` | Retrieval, Generator, Validators |
| Tracer | `app/trace.py` | Write retrieval/answer traces; redact; assign `request_id` | filesystem |
| ArtifactReader | `app/inspector.py` | Read artifacts for the inspector; no recomputation | filesystem, Chroma |
| Evaluator | `app/eval/run_eval.py` | Golden set → metrics report | QueryService (in-process) |
| CLI | `app/cli.py` | Subcommands per stage; `--force`, `--dry-run`, `--query` | pipeline modules |
| UI | `app/ui/streamlit_app.py`, `app/ui/pages/2_Pipeline_Inspector.py` | Ask + Inspector views | QueryService, ArtifactReader |

**Implementation note (Phases 0-6).**  The shipped code consolidates the query
path into `app/pipeline/retrieve.py` and `app/pipeline/answer.py`, with
`AnswerService` as the single entry point; the deterministic input gate lives in
`app/guardrails/input.py`, the provider adapter in `app/llm/client.py`, and the
inspector reader in `app/ui/artifacts.py`.  The table above remains the design
target; the evaluator is still a Phase 7 stub.

### 4.2 Layering rule

```text
ui ──▶ service ──▶ pipeline ──▶ llm
                    │  │
                    │  └──▶ guardrails (pure functions, no I/O)
                    └──▶ config, trace

pipeline never imports ui. guardrails never import pipeline or llm.
llm never imports pipeline, guardrails, or ui.
```

This keeps guardrails unit-testable without models, and lets a provider adapter be swapped without touching retrieval.

---

## 5. Offline data plane — stage-by-stage architecture

The build pipeline is a **linear, artifact-mediated chain**. Each stage is a CLI subcommand, reads only its declared upstream artifact, and writes a new immutable one under `data/`.

### 5.1 Pipeline contract

| Stage | CLI | Reads | Writes | Idempotency key | Hard failure |
|---|---|---|---|---|---|
| P0 Sources | `sources validate` | `config/sources.yaml` | `data/00_sources/validation_report.md`, `validation.json` | URL + date | Domain outside allowlist; malformed registry |
| P1 Load | `load` | `sources.yaml` | `data/01_raw/{id}.html/.txt/.json`, `manifest.json` | URL + `fetched_at` | Core page unfetchable and no cache |
| P2 Chunk | `chunk` | `data/01_raw/*.json` | `data/02_chunks/chunks.jsonl`, `chunks.csv`, `chunk_report.md` | `content_sha256` | Target fact missing from all chunks |
| P3 Embed | `embed` | `chunks.jsonl` | `data/03_embeddings/embeddings.npy`, `embedding_ids.json`, `manifest.json` | `content_sha256` per chunk | Count/dim/norm mismatch |
| P4 Index | `index` | embeddings + chunks | `data/04_vector_db/chroma/`, `collection_stats.json` | `chunk_id` upsert | Model name ≠ collection model |
| P5/P6 Query | `retrieve`, `answer` | Chroma + registry | `data/05_retrieval/{request_id}.json`, `data/06_answers/{request_id}.json` | `request_id` | n/a (degrades instead) |
| P7 Eval | `eval` | golden set + QueryService | `data/07_eval/eval_report.md`, `eval_results.jsonl` | run id | Below-target metrics (report, not crash) |

Three invariants hold across all stages:

1. **No silent recompute.** A stage never regenerates an upstream artifact; if an input is missing it fails with a remediation hint (`run: python -m app.cli chunk`).
2. **No destructive overwrite.** New runs write to a new `run_id` sub-path or versioned file; the “current” pointer is `manifest.json`'s `run_id`.
3. **Provenance is preserved.** Every chunk and trace carries `source_url`, `scraped_at`, and `source_sha256`, so any rendered value can be walked back to the exact bytes fetched.

### 5.2 P0 — Source registry and validation

```text
sources.yaml ──▶ RegistryLoader ──▶ (schema check) ──▶ DomainAllowlist ──▶
              ──▶ HeadCheck (httpx HEAD/GET, 2s delay, UA) ──▶ validation.json/md
```

Key behaviours:

- Registry entries are validated against a schema (`id` unique, `role ∈ {core, support, citation_only}`, domain ∈ allowlist, `url` https, `expected_facts` non-empty for `core`).
- `role` drives everything downstream: `core`/`support` are fetched and indexed; `citation_only` is **never fetched**, only resolved at render time. This is how the four bot-403 HDFC pages stay usable without bypassing controls.
- Validation records `http_status`, `final_url` (redirect check must stay on-allowlist), `content_type`, `checked_at`, `notes`. A 403 on a `citation_only` entry is recorded as `expected: bot_blocked`, not an error.
- Output is a markdown table for the demo plus JSON for machine checks.

### 5.3 P1 — Load and extract

```text
registry[role != citation_only]
   │
   ├─ robots.txt check ──▶ disallow? ──▶ mark skipped, keep prior snapshot
   ▼
Fetcher (httpx, UA, timeout, retry, delay) ──▶ RawStore {id}.html   [byte-exact]
   │                                                   │ sha256
   │ JS-required public page? ──▶ Playwright (permitted pages only)
   │ bot-blocked (403/CAPTCHA)? ──▶ do not bypass; cite only / manual browser check
   ▼
Extractor vN (per doc_type) ──▶ SectionModel {key_stats, minimum_investments, exit_load,
   │                                          about, investment_objective, benchmark,
   │                                          fund_house, statement_steps, definitions}
   ▼
Denylist filter (returns, ratings, holdings, fund_management, compare, calculators, why_invest)
   ▼
{id}.txt  +  {id}.json  +  manifest.json entry
```

Extractor design:

| Aspect | Decision | Why |
|---|---|---|
| Parser | `selectolax` for speed, fall back to `bs4` | 5–14 pages; speed irrelevant but deterministic output matters |
| Extraction style | Label → value anchoring, not whole-page text dump | Groww renders key stats as label/value pairs; anchoring preserves the exact pairing |
| Versioning | `extractor_version` string written into every section record | Groww HTML drift is the top risk (PRD §17); version lets us diff and roll back |
| Fixtures | One saved HTML per core page in `tests/fixtures/` | Unit tests must not depend on live Groww HTML |
| Missing facts | `missing_expected[]` in manifest; loader exits non-zero if a **core** page misses a required fact | Fail loudly during build, not silently at query time |
| Dates | Parse source-visible dates (e.g. “NAV: 30 Oct '26”) into `source_date` | The “Last updated from sources” line must reflect the page, not the clock |

Extracted sections are **typed and flat** — no HTML, no nested soup — because the chunker maps one section type to one chunking rule.

### 5.4 P2 — Chunking

Chosen strategy: `structure_aware_fact_cards_v1` (PRD §8.4). Architecturally it is a **rule table from section type to chunker function**:

```text
sections.json
   │
   ├─ key_stats / exit_load / benchmark / lock_in ──▶ FactCardChunker (1 fact = 1 chunk)
   ├─ minimum_investments ─────────────────────────▶ FactGroupChunker (first+second+SIP together)
   ├─ scheme_basics ───────────────────────────────▶ FactGroupChunker (category/plan/option/launch/AMC)
   ├─ about / investment_objective ────────────────▶ NarrativeChunker (heading-scoped, ≤300 tok, 40 overlap)
   ├─ statement_steps ─────────────────────────────▶ OrderedStepChunker (one chunk, steps preserved)
   ├─ definitions ─────────────────────────────────▶ DefinitionChunker (one concept = one chunk)
   └─ amc_overview ────────────────────────────────▶ SummaryCard + KeyInfoTableCard
   ▼
context header injection ──▶ content hash ──▶ dedupe ──▶ chunks.jsonl + report
```

Chunk shape decisions:

- **Context header is prepended to the embedded text**, not stored only as metadata. A bare value like `1.02%` embeds poorly; a header (`Scheme | Category | Plan | Fact type | Source`) makes the vector self-describing and keeps retrieval honest when the user names only a fact.
- **No overlap for fact cards.** Overlap on atomic facts creates duplicate candidates that MMR then has to fight.
- **Narrative overlap only.** 40 tokens, because statement steps and objectives can span a window boundary.
- **Dedupe by `content_sha256`**, but keep cross-source duplicates when they add provenance; `source_priority` breaks the tie at retrieval.
- **Deterministic ordering.** Chunk IDs are `{doc_id}__{fact_type}__{hash8}`, so re-runs produce identical IDs and Chroma upserts are safe.

The chunk report is a first-class artifact: counts by source, by fact type, by token bucket, plus a list of any expected fact with zero chunks.

### 5.5 P3 — Embedding

```text
chunks.jsonl ──▶ hash cache lookup ──▶ miss? ──▶ SentenceTransformer(model, revision)
   │                                                            │
   │◀──────────── cached vectors ───────────────────────────────┘
   ▼
normalize(L2) ──▶ embeddings.npy (N,384) float32 ──▶ assert count/dim/norm
   ▼
manifest.json {model, revision, dim, dtype, normalized, count, chunks_sha256, embeddings_sha256, config_sha256}
```

- The embedding model is referenced by **fully qualified name and revision**, and that name is also encoded in the Chroma collection name. A model swap is therefore a *new collection*, never a silent mixed-vector query.
- `assert max|‖v‖₂ − 1| < 1e-3` after normalization; cosine similarity then equals dot product, which makes score interpretation stable across providers.
- The cache is keyed by `content_sha256`, so a single page edit re-embeds only its changed cards.
- Device/batch size are config, not code; CPU is the default so the demo has no GPU dependency.

### 5.6 P4 — Indexing

Collection: `groww_mf_facts__minilm_l6_v2__v1`, `hnsw:space=cosine`, persistent path `data/04_vector_db/chroma`.

```text
ids        = chunk_id
documents  = chunk.text            (exactly what was embedded)
embeddings = normalized vector
metadatas  = flat scalars only      (Chroma restriction)
```

Metadata flattening rule: `dict`/`list` values are serialized to JSON strings (e.g. `expected_facts`) and never used as filter fields. Filtered fields are a fixed allowlist:

`doc_id, source_url, source_owner, source_priority(int), scheme_slug, fact_type, section, chunk_type, plan, category, scraped_at(str ISO), source_date(str YYYY-MM-DD), token_count(int)`.

Index-time pruning: compare `collection.get(ids=...)` against current `chunk_id` set; delete stale IDs, upsert changed ones. `collection_stats.json` records count, model, dimension, created_at, and 5 sample IDs so the inspector can prove the collection matches the chunk file.

### 5.7 P7 — Evaluation harness

Evaluation is a **consumer of the same `QueryService`** the UI uses, not a parallel code path. That is deliberate: metrics must measure the production path or they measure nothing.

```text
golden_set.jsonl ──▶ for each item: QueryService.answer() ──▶ compare
   │                        │
   │                        └── uses retrieval trace to compute hit@3 / scheme accuracy
   ▼
eval_results.jsonl (per-query verdicts) ──▶ metrics.py ──▶ eval_report.md
```

Report groups: fact accuracy, citation validity, citation correctness, length compliance, refusal accuracy, PII leakage, hit@3, scheme resolution, latency p50/p95, not-found precision.

### 5.8 Idempotency, versioning, and refresh

| Concern | Mechanism |
|---|---|
| Re-running a stage | Cache/upsert by content hash or ID; no-op when inputs unchanged |
| Detecting source drift | `manifest.json` stores `sha256` per source; loader compares and logs `changed: true` |
| Rolling back a bad extractor | Keep prior `01_raw/{run_id}/` snapshot; `--run-id` selects it |
| Stale values | `source_date` + `scraped_at` on every chunk; UI shows “Index last refreshed” |
| Config drift | `config_sha256` in embedding manifest; mismatch blocks indexing |
| Model drift | Model name in collection name; mismatch blocks query with a clear message |

Refresh is **manual** in v1 (`corpus.refresh.mode: manual`). A refresh is exactly: `sources validate → load → chunk → embed → index`, then re-run `eval`. No incremental crawl daemon, no scheduler.

---

## 6. Persistence and data contracts

### 6.1 Artifact layout

```text
data/
├─ 00_sources/
│  ├─ validation.json           # machine
│  └─ validation_report.md      # demo table
├─ 01_raw/
│  ├─ {run_id}/                 # per-run snapshot
│  │  ├─ {source_id}.html       # byte-exact response
│  │  ├─ {source_id}.txt        # cleaned main text
│  │  └─ {source_id}.json       # typed sections
│  └─ manifest.json             # run index + checksums
├─ 02_chunks/
│  ├─ chunks.jsonl              # canonical
│  ├─ chunks.csv                # human review
│  └─ chunk_report.md
├─ 03_embeddings/
│  ├─ embeddings.npy
│  ├─ embedding_ids.json
│  └─ manifest.json
├─ 04_vector_db/
│  ├─ chroma/                   # persistent client directory
│  └─ collection_stats.json
├─ 05_retrieval/{request_id}.json
├─ 06_answers/{request_id}.json
├─ 07_eval/{golden_set.jsonl, eval_results.jsonl, eval_report.md}
└─ logs/app.jsonl               # redacted operational log
```

### 6.2 Record schemas (canonical fields)

| Record | Identity | Required fields | Notes |
|---|---|---|---|
| Source registry entry | `source_id` | `id, title, url, owner, doc_type, role, source_priority` | `role=citation_only` ⇒ no fetch |
| Raw manifest entry | `source_id` | `url, http_status, fetched_at, bytes, sha256, extractor_version, sections_found, missing_expected` | Append-only per run |
| Chunk | `chunk_id` | PRD §8.4 metadata block + `text, token_count, content_sha256, chunker_version` | One JSON object per line |
| Embedding manifest | file | `embedding_model, revision, dimension, dtype, normalized, count, chunks_sha256, embeddings_sha256, config_sha256, created_at` | Count triple-asserted |
| Retrieval trace | `request_id` | `query_redacted, intent, scheme_slug, filters, candidates[], selected[], gate_passed` | `query_redacted` is null for PII cases |
| Answer trace | `request_id` | `prompt_sha256, llm{provider,model,latency_ms,tokens_*}, raw_response, validation{}, final{}` | Full prompt stored only for non-PII queries |
| Eval result | `(run_id, query_id)` | `expected, actual, verdict, metrics{}` | Used to build the report |

Schemas live in `app/schemas.py` as pydantic models; both writers and the inspector validate against them, so a schema change is a single-file change plus a version bump.

### 6.3 Chroma schema and query surface

```python
collection.query(
    query_embeddings=[qvec],
    n_results=candidate_k,          # 20
    where={"scheme_slug": slug},    # optional, only when resolution is confident
    include=["documents", "metadatas", "distances"],
)
# similarity = 1 - distance   (cosine space)
```

Deliberate constraints:

- **One collection per (embedding model, index version).** Switching MiniLM → another model requires a new collection name and a full re-index; the app refuses to query a collection whose name does not match `config.embedding`.
- **Filters are additive, never exclusive-by-default.** If scheme resolution is ambiguous, the query runs unfiltered and ambiguity is handled by the decision layer, not by guessing a `where` clause.
- **No where-document filters on free text.** Fact-type preference is a post-retrieval score boost (`+0.05`), not a hard filter, so a misclassified intent still has a recovery path.

### 6.4 Request lifecycle artifact pair

Every user interaction produces exactly two files, both keyed by `request_id = req_{ulid}`:

```text
05_retrieval/req_x.json   ← what the retriever saw and chose
06_answers/req_x.json     ← what the model said and what validation did with it
```

The inspector joins them by `request_id`. This split is intentional: retrieval can be debugged and evaluated without any LLM call, and generation can be replayed against a stored retrieval trace.

---

## 7. Online query plane

### 7.1 Request lifecycle

```text
POST question (UI)
  │
  ├─ L0  input gate ────────── length, encoding, PII regex, advice/perf keywords, injection phrases
  │        └─ hit? ──▶ deterministic refusal/clarify ──▶ render (one link) ──▶ trace
  │
  ├─ L1  normalize ─────────── lowercase, alias expand (ER→expense ratio, SIP, ELSS, CAS),
  │        │                    strip punctuation, collapse whitespace
  │        └─▶ QueryContext {raw, normalized, session_last_scheme}
  │
  ├─ L2  intent + scheme ───── fact_type classification (rules), scheme resolution (alias→fuzzy→session)
  │        └─ ambiguous? ──▶ clarify (scheme-list link) ──▶ trace
  │        └─ unsupported? ──▶ out-of-scope refusal ──▶ trace
  │
  ├─ L3  retrieve ──────────── embed(query) → Chroma(candidate_k=20) → boost → MMR(top_k=8)
  │        └─ gate fail? ──▶ not-found (official page link) ──▶ trace
  │
  ├─ L4  assemble context ──── top 5 chunks, numbered [S1..S5], ≤1200 tokens, strip instruction-like lines
  │
  ├─ L5  generate ──────────── prompt → LLMClient.complete → JSON parse (retry once)
  │        └─ provider error/invalid JSON ──▶ extractive fallback from top chunk
  │
  ├─ L6  validate ──────────── schema, source_id∈context, allowlist, scheme match, numeric grounding,
  │        │                    length, banned terms, performance claim, PII echo
  │        └─ fail ──▶ repair (fallback) or downgrade (not-found / refusal)
  │
  └─ L7  render + persist ──── answer + one Source line + Last updated + why-this-answer snippet
           └─▶ 06_answers/{id}.json
```

Everything from L0 to L4 is **model-independent**. Only L5 touches a network LLM. This is what makes the refusal behaviour deterministic and the fallback path trustworthy.

### 7.2 Decision state machine

The answer status is a closed set. Transitions are explicit so no path can “fall through” into an unsourced answer.

```text
                 ┌──────────────┐
   input ───────▶│   received   │
                 └──────┬───────┘
        PII ────────────┼──────────────▶ refused(pii)          → link: Groww help/CAS
        advice ─────────┼──────────────▶ refused(advice)       → link: scheme or AMC page
        performance ────┼──────────────▶ refused(performance)  → link: HDFC factsheet
        other AMC/scope ┼──────────────▶ refused(out_of_scope) → link: Groww AMC list
                        ▼
                 ┌──────────────┐   no scheme + no session
                 │  classified  │──────────────▶ clarify      → link: AMC page / scheme list
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐   gate fail / no chunk
                 │  retrieved   │──────────────▶ not_found    → link: resolved scheme page
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐   LLM ok + valid
                 │  generated   │──────────────▶ answered
                 └──────┬───────┘
                        │ invalid after retry
                        ▼
                 ┌──────────────┐
                 │  extractive  │──────────────▶ answered (source = top chunk)
                 └──────────────┘
```

Invariants:

- Exactly one terminal state per request; states are stored as `final.status` alongside `final.reason`.
- **Every** terminal state carries exactly one `citation_url` and one `last_updated_from_sources`. There is no state that renders without them.
- `answered` may only be reached with `source_id ∈ retrieved context` and `numbers_grounded = true`.

### 7.3 Scheme resolution

```text
scheme phrase
   │
   ├─ exact alias hit (registry aliases + scheme_name + slug words) ──▶ slug
   ├─ rapidfuzz token_set_ratio ≥ 85 ────────────────────────────────▶ slug
   ├─ no phrase but session has last_scheme ─────────────────────────▶ last_scheme  (labelled in trace)
   ├─ no phrase, no session, question is scheme-scoped ──────────────▶ clarify
   └─ other AMC token detected (sbi/axis/icici/hdfc-sec…) ───────────▶ out_of_scope
```

Notes:

- The alias table is **data**, in `config/sources.yaml` (`aliases: [...]`), not a hard-coded `if` ladder — adding a scheme later is a config change.
- “HDFC” alone is not a scheme match (it is the AMC); it resolves to `amc:hdfc-mutual-fund` only when the fact type is AMC-scoped.
- Statement/definition questions are scheme-agnostic by design: `scheme_filter` is skipped, and retrieval is filtered by `fact_type` instead.
- Session inheritance of the last scheme is disclosed in the answer trace (`scheme_source: "session"`), never guessed silently in the UI.

### 7.4 Retrieval architecture

Two-phase scoring, then diversity, then a gate:

| Phase | Operation | Config |
|---|---|---|
| Candidate | Chroma ANN over 20 nearest, optional `where` on `scheme_slug` | `retrieval.candidate_k=20` |
| Adjust | `score = similarity + 0.05 if metadata.fact_type == intent`; `+0.02` if `source_priority==1` | `fact_type_boost`, priority bonus |
| Diversify | MMR over candidate vectors, λ=0.7, keep 8 | `retrieval.mmr` |
| Gate | keep chunks with `similarity ≥ 0.35` **and** scheme support; if none, not-found | `min_similarity` |
| Assemble | top 5 by adjusted score, ≤1200 tokens, dedupe by `content_sha256` | `context_max_tokens` |

Why MMR on a 150–300 chunk corpus: near-duplicate fact cards exist across Groww pages, AMFI explainers, and SEBI guides. Without diversity the context can be five restatements of the same sentence, which wastes tokens and hides the authoritative source. λ=0.7 keeps relevance dominant.

Why a low threshold (0.35) with a hard *post*-gate: MiniLM produces compressed similarity ranges; a high threshold silently refuses correct paraphrase queries. The gate is therefore “threshold **and** metadata support”, not threshold alone.

### 7.5 Context assembly

```text
[CONTEXT]
[S1]
scheme: HDFC Large Cap Fund Direct Growth
fact_type: expense_ratio
section: key_stats
source_url: https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth
scraped_at: 2026-11-01
text: Scheme: HDFC Large Cap Fund Direct Growth | Fact: Expense ratio | Value: 1.02%
[/S1]
...
[/CONTEXT]
```

Assembly rules:

- Blocks are ordered by adjusted score; `S1` is always the top candidate.
- `source_url` is included so the model can name the source, but the **rendered link still comes from the registry**, so a corrupted context block cannot inject a URL.
- Instruction-like lines (`ignore previous`, `system:`, `you are now`) are stripped from chunk text before insertion.
- Hard token budget of 1200; if exceeded, drop lowest-scored blocks (never truncate a block mid-fact).
- Only chunks that passed the gate are eligible; context is never padded with weak candidates to look confident.

---

## 8. LLM integration architecture

### 8.1 Protocol

Vendor SDKs are confined to adapters. Everything above talks to one interface:

```python
class LLMClient(Protocol):
    name: str
    model: str

    def complete(
        self,
        messages: list[Message],      # system + user, already formatted
        *,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
        json_mode: bool = True,
    ) -> LLMResponse: ...

@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    tokens_in: int | None
    tokens_out: int | None
    finish_reason: str | None
```

The generator depends only on this protocol. Provider selection happens once at startup in `app/llm/client.py`; no pipeline module ever imports `groq` or `anthropic`.

### 8.2 Adapters

All transports use `httpx` directly; no vendor SDK is imported at runtime.

| Adapter | Transport | JSON strategy | Notes |
|---|---|---|---|
| `OpenAICompatibleClient` (`groq`) | `POST /chat/completions` | `response_format={"type":"json_object"}` | Default; retries once without `response_format` if the provider rejects it |
| `OpenAICompatibleClient` (`openai`) | `POST /chat/completions` | `response_format={"type":"json_object"}` | Same code path as Groq |
| `OpenAICompatibleClient` (`mistral`) | `POST /chat/completions` | `response_format={"type":"json_object"}` | Same code path as Groq |
| `AnthropicClient` | `POST /v1/messages` | prompt-enforced JSON | System prompt is a top-level field; content blocks are concatenated; no native JSON mode |
| `gemini` | — | — | Not wired; raises `LLMUnavailable` and falls back to extractive mode |

Cross-adapter rules:

- System prompt is passed as the provider's system role where supported, otherwise prepended as the first user turn (Anthropic handles it natively; Gemini uses `system_instruction`).
- `temperature=0.0` everywhere; `max_tokens=220`; `timeout_s=30`.
- No provider-specific fields leak into the prompt; the JSON schema is inside the system prompt text for all providers so behaviour is comparable.
- Token counts are recorded when the provider returns them; otherwise `None` and latency is the only cost signal.

### 8.3 Configuration resolution

```text
.env  LLM_PROVIDER, LLM_MODEL
  │
  ▼
config.yaml generation.provider = "${LLM_PROVIDER:-groq}"
             generation.model    = "${LLM_MODEL:-qwen/qwen3.8-27b}"
  │
  ▼
ConfigLoader: env > yaml default > built-in default
  │
  ├─ provider not in {groq, gemini, openai, anthropic, mistral} ──▶ startup error
  ├─ api_key_env missing/empty ──▶ startup error with the exact var name
  └─ ok ──▶ LLMClient instance (model passed straight through, never hard-coded)
```

The **model string is never validated against a hard-coded list** — it is passed to the provider, and the provider's 4xx is surfaced as a startup/`answer` error with a remediation hint. This is what “LLM model from `.env`” means in practice: adding a new Groq model is a `.env` edit, not a code change.

### 8.4 Fallback chain

```text
primary provider ──(timeout/5xx/rate-limit)──▶ retry once (same model)
        │
        ├── still failing ──▶ optional secondary model (generation.fallback_model, if set)
        │
        └── still failing / invalid JSON / failed validation ──▶ extractive fallback
```

Extractive fallback, precisely:

1. Take the top gated chunk.
2. Emit `f"{fact_label} for {scheme}: {value}."` from the fact card's `Fact`/`Value` fields (fact cards are structured, so this is mechanical).
3. Cite the chunk's `source_id`.
4. Mark `final.status = "answered"`, `final.mode = "extractive"`, and record `llm.error` in the trace.

The fallback is *not* a silently degraded answer: the inspector and the eval report both show `mode=extractive`, so the demo can be honest about it.

---

## 9. Guardrail architecture

Guardrails are layered so that a failure at any layer cannot produce an ungrounded or unsafe answer. Each layer is a **pure function over plain data**, unit-testable without models or network.

```text
            ┌─────────────────────────────────────────────────────┐
 user text  │ G1 input gate       length, PII, advice, performance│──▶ refusal
            └───────────────────────┬─────────────────────────────┘
                                    ▼
            ┌─────────────────────────────────────────────────────┐
            │ G2 scope gate       scheme known? supported AMC?    │──▶ clarify / out_of_scope
            └───────────────────────┬─────────────────────────────┘
                                    ▼
            ┌─────────────────────────────────────────────────────┐
            │ G3 evidence gate    min_similarity + metadata support│──▶ not_found
            └───────────────────────┬─────────────────────────────┘
                                    ▼
            ┌─────────────────────────────────────────────────────┐
            │ G4 prompt hygiene   strip instructions, delimit, tag│
            └───────────────────────┬─────────────────────────────┘
                                    ▼
            ┌─────────────────────────────────────────────────────┐
            │ G5 output validators schema/citation/scheme/numbers/ │──▶ fallback or downgrade
            │                      length/banned/PII-echo          │
            └───────────────────────┬─────────────────────────────┘
                                    ▼
            ┌─────────────────────────────────────────────────────┐
            │ G6 renderer         one URL from registry, one date, │
            │                     escaped markdown, no raw HTML     │
            └─────────────────────────────────────────────────────┘
```

### 9.1 G1 — input gate

| Check | Detection | Terminal |
|---|---|---|
| Length | `len(question) > 500` | clarify → “Please shorten the question.” |
| PII | PAN, Aadhaar, account/folio, OTP, email, phone, IFSC regex with context guards | `refused(pii)` |
| Advice | `should i`, `buy`, `sell`, `hold`, `recommend`, `better`, `best`, `suitable for me`, `safe?`, `guarantee`, `portfolio` | `refused(advice)` |
| Performance | `return`, `cagr`, `annualised`, `outperform`, `compare`, `highest`, `will it grow`, `past performance` | `refused(performance)` |
| Injection | “ignore your instructions”, “show your system prompt”, “answer without a source” | `refused(out_of_scope)` |

Context guards matter: `1.02%`, `₹500`, `3 years`, `1 year` are facts, not PII. Numeric PII patterns require a nearby keyword (`pan`, `aadhaar`, `account`, `folio`, `otp`, `ifsc`) or a shape that cannot be a fund fact (e.g. 12 digits, `ABCDE1234F`).

### 9.2 G4 — prompt hygiene

- Context blocks are wrapped in `[S#]…[/S#]` and prefixed with a system note that CONTEXT is untrusted reference data.
- Lines matching instruction patterns are removed from chunk text (`ignore (all|previous) instructions`, `system:`, `assistant:`, `you are now`).
- Chunk text is plain text only; HTML tags are stripped at extraction and again at assembly (defence in depth).
- The model cannot see `.env`, keys, file paths, or the registry.

### 9.3 G5 — output validation pipeline

Ordered, fail-fast, each with a defined downgrade:

| # | Validator | Failure → |
|---|---|---|
| 1 | JSON parses and matches `AnswerSchema` | retry once → extractive |
| 2 | `status` ∈ enum | retry once → extractive |
| 3 | `source_id` present in context indices | retry once → extractive |
| 4 | `source_id` resolves in registry; domain in allowlist | not_found |
| 5 | `scheme_slug` in answer matches resolved scheme (name presence check) | extractive |
| 6 | Every numeric token in answer appears in cited chunk (normalized: strip `₹`,`,`,`%`, units) | extractive |
| 7 | Sentence count ≤3 and word count ≤70 (metadata lines excluded) | trim → extractive |
| 8 | No banned terms / performance claims / advice verbs | performance refusal or extractive |
| 9 | No PII echo (compare against detected input PII shapes and generic patterns) | redact → `refused(pii)` |
| 10 | Exactly one citation after render | renderer-enforced (structurally impossible to exceed) |

Validator 6 is the load-bearing one: it is the only defence against a fluent but wrong number, because a wrong number is likely to *look* plausible and pass every other check.

### 9.4 G6 — renderer as the citation chokepoint

The renderer is the **only** component that writes a URL:

```python
def render(result) -> str:
    src = registry[result.source_id]                  # validated upstream
    assert is_allowlisted(src.url)
    body = enforce_length(result.answer)              # ≤3 sentences / ≤70 words
    return (
        f"{body}\n\n"
        f"Source: [{src.short_label}]({src.url})\n"
        f"Last updated from sources: {result.source_date}"
    )
```

Consequences:

- The LLM cannot create a second link, a markdown image, or a raw URL in the body; the body is escaped and scanned for `http` before rendering.
- The date comes from `source_date` (page-visible) falling back to `scraped_at`, never `datetime.now()`.
- Refusals and not-found responses use the same renderer with a fixed `source_id`, so the “one link + one date” rule holds for **every** response class.

---

## 10. Configuration and runtime contracts

### 10.1 Three-layer configuration

```text
defaults (in code)  ←  config/config.yaml  ←  environment variables / .env
        least specific                      most specific (wins)
```

| Layer | Holds | Committed? |
|---|---|---|
| Code defaults | Safe values so the app can start | Yes |
| `config/config.yaml` | Corpus, chunking, embedding, vector DB, retrieval, guardrails, UI copy | Yes |
| `config/sources.yaml` | 21-source registry, aliases, roles, priorities | Yes |
| `.env` | `LLM_PROVIDER`, `LLM_MODEL`, provider API keys | No (`.gitignore`) |

Precedence is implemented once in `app/config.py`; nothing else reads `os.environ` directly. That single chokepoint is what makes “model comes from `.env`” auditable.

### 10.2 Startup validation (fail fast)

| Check | On failure |
|---|---|
| `LLM_PROVIDER` is a known adapter | Exit with the list of valid providers |
| `LLM_MODEL` non-empty | Exit: “set LLM_MODEL in .env” |
| Active provider's API key env var present | Exit naming the exact variable |
| `config.yaml` parses and matches schema | Exit with the offending key |
| `sources.yaml` domains ⊆ allowlist | Exit with offending URL |
| Chroma dir exists and collection model matches config | Exit: “run `python -m app.cli index`” |
| `chunks.jsonl` count == embedding ids count == collection count | Exit: “pipeline out of sync; re-run index” |

Every message names the **fix command**, because the demo failure mode people actually hit is a stale index after a config edit.

### 10.3 Runtime configuration surface

| Setting | Env override | Effect of change |
|---|---|---|
| `generation.provider` / `model` | `LLM_PROVIDER`, `LLM_MODEL` | New adapter at next start; no re-index |
| `embedding.model_name` / `revision` | — (yaml) | **Requires** re-embed + re-index into a new collection |
| `vector_db.provider` | — | Adapter boundary; Chroma is the only v1 implementation |
| `chunking.strategy` | — | Requires chunk → embed → index |
| `retrieval.candidate_k/top_k/min_similarity/mmr` | — | Query-time only; re-run eval to confirm |
| `guardrails.*` | — | Query-time only |
| `ui.examples`, `ui.welcome` | — | UI only |

Rule of thumb reflected in the table: **anything upstream of the vector store is a rebuild; anything at query time is a config edit.** The architecture keeps that boundary visible so operators know the cost of a change.

---

## 11. UI architecture

### 11.1 Two views, one process

| View | File | Reads | Writes |
|---|---|---|---|
| Ask | `app/ui/streamlit_app.py` | `AnswerService` | session state only |
| Pipeline Inspector | `app/ui/pages/2_Pipeline_Inspector.py` | `ArtifactReader` (files + Chroma read-only) | nothing |

The inspector **never calls `AnswerService`** and never recomputes embeddings. It renders what the pipeline already wrote. This is what makes it evidence rather than a second, possibly divergent implementation.

### 11.2 Ask view structure

```text
┌─────────────────────────────────────────────────────────────┐
│ Groww MF Facts Assistant                                    │
│ Ask a fact about HDFC mutual fund schemes — expense ratio,  │
│ exit load, SIP minimum, ELSS lock-in, risk, benchmark, or   │
│ statements.                                                 │
│ [ expense ratio of HDFC Large Cap ] [ ELSS lock-in ]        │
│ [ download capital-gains statement ]                        │
│ ─────────────────────────────────────────────────────────── │
│ question input (max 500 chars)                  [ Ask ]     │
│ ⚠ Facts-only. No investment advice.                         │
│ ─────────────────────────────────────────────────────────── │
│ ANSWER CARD                                                 │
│  • answer body (≤3 sentences)                               │
│  • Source: [label](one url)                                 │
│  • Last updated from sources: YYYY-MM-DD                    │
│  ▸ Why this answer? → cited snippet + similarity + chunk id  │
│ ─────────────────────────────────────────────────────────── │
│ footer: disclaimer · index last refreshed · no-PII note     │
└─────────────────────────────────────────────────────────────┘
```

Interaction rules:

- Example buttons **fill** the input; they do not auto-submit (avoids surprising a demo presenter).
- Session state holds `last_scheme` and `last_request_id` only. No transcript is persisted; refreshing the page starts clean.
- PII-flagged input is cleared from the widget immediately after the refusal, so the value does not linger in the browser or in Streamlit's rerun buffer.
- The answer card renders markdown with links disabled in the body and exactly one enabled link in the Source line.

### 11.3 Inspector view structure

Tabs mirror the pipeline so the demo can walk left-to-right:

| Tab | Source of truth | Shows |
|---|---|---|
| 1. Source | `01_raw/{id}.json` + manifest | URL, HTTP status, fetched_at, sha256, sections found, missing facts, raw text excerpt |
| 2. Chunks | `02_chunks/chunks.jsonl` | Filter by source/fact type; full chunk text, token count, metadata table |
| 3. Embeddings | `03_embeddings/*` | Model, revision, dim, first 8 values, L2 norm, count |
| 4. Vector DB | Chroma + `collection_stats.json` | Collection name, count, distance space, sample IDs, raw query result for a chosen query |
| 5. Prompt | `06_answers/{id}.json` | Exact user prompt (system rules are sent separately), token estimate |
| 6. LLM | `06_answers/{id}.json` | Provider, model, latency, token counts, raw response string |
| 7. Validation | `06_answers/{id}.json` | Pass/fail per validator with the failing detail |
| 8. Final | `06_answers/{id}.json` | Rendered answer, citation, date, trace path |

Guarded by `?debug=1` **or** `APP_ENV=local`; on anything else the page renders a “local-only” notice and no data.

---

## 12. Observability

### 12.1 What is logged

`data/logs/app.jsonl`, one JSON object per line:

```json
{
  "ts": "2026-11-01T10:22:31.412Z",
  "request_id": "req_01JC...",
  "event": "answer",
  "stage": "generation",
  "status": "answered",
  "mode": "llm",
  "intent": "expense_ratio",
  "scheme_slug": "hdfc-large-cap-direct-growth",
  "provider": "groq",
  "model": "qwen/qwen3.8-27b",
  "latency_ms": 1412,
  "retrieval_ms": 38,
  "generation_ms": 1290,
  "top_similarity": 0.71,
  "citation_ok": true,
  "numbers_grounded": true,
  "query_hash": "sha256:…",
  "query_redacted": null
}
```

### 12.2 What is never logged

- Raw query text for PII-flagged requests (`query_redacted: null`, only `query_hash` + `pii_types`).
- API keys, `.env` values, Authorization headers.
- Full prompts/responses for PII-flagged requests.
- Absolute filesystem paths (traces use relative `data/…` paths so they can be shared).

### 12.3 Trace correlation

`request_id` is the join key across four files: `05_retrieval/`, `06_answers/`, `logs/app.jsonl`, and the UI's “Why this answer?” expander. One copyable ID makes any demo claim checkable.

---

## 13. Security and privacy architecture

| Threat | Control | Layer |
|---|---|---|
| PII ingestion | Regex + context guards before retrieval; no persistence of raw text | G1, tracer |
| PII echo by LLM | Output scan; redact; downgrade to PII refusal | G5.9 |
| Prompt injection via page text | Untrusted-context framing, instruction stripping, fixed schema, no tool access | G4 |
| Prompt injection via user | Advice/instruction keyword refusal; “show system prompt” refused | G1 |
| Citation hallucination | `source_id`-only contract; registry resolution; allowlist assert | G5, G6 |
| Hallucinated numbers | Numeric grounding against cited chunk | G5.6 |
| Secret leakage | `.env` unread by UI; never logged; startup validation only reads keys | config |
| Source abuse | robots.txt, 2 s delay, retries capped, no auth bypass, citation-only for 403 pages | P1 |
| Untrusted markdown/HTML in output | Body escaped; links restricted to the single Source line | G6 |
| Clickjacking/CSRF | Local-only Streamlit bind (127.0.0.1); no public deployment in v1 | deployment |

**Privacy-by-design assertion:** the system never *needs* PII to answer any in-scope question, so the correct architecture is refusal at the boundary rather than storage with access controls.

---

## 14. Deployment and run topology

### 14.1 Topology

```text
Laptop (single machine)
├─ .venv (Python 3.11)
├─ Streamlit on http://127.0.0.1:8501        ← UI process
├─ Chroma persistent dir ./data/04_vector_db ← embedded, same process
├─ ./data artifacts                          ← filesystem
└─ outbound HTTPS: groww.in / amfiindia.com / sebi.gov.in / groq.com
```

No server, container, queue, or managed database is required. Docker is optional and out of scope for v1.

### 14.2 First-run sequence

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # set LLM_PROVIDER, LLM_MODEL, GROQ_API_KEY

python -m app.cli sources validate
python -m app.cli load
python -m app.cli chunk
python -m app.cli embed
python -m app.cli index
python -m app.cli eval
streamlit run app/ui/streamlit_app.py
```

Cold-start budget: dependency install is the long pole; after that, `sources → index` is a few minutes for 14 pages and the first question answers in ~2 s.

### 14.3 Demo topology

- Pre-build all artifacts **before** recording, so the demo shows answers in seconds.
- Keep the inspector pointed at a fixed `request_id` from a warm-up query.
- If hosting is ever needed: rebuild the index at container start (ephemeral FS), disable the inspector, and gate with an access token — none of which is required for the submitted demo.

---

## 15. Performance and capacity

| Path | Cost driver | Target |
|---|---|---|
| Query embed (MiniLM, CPU, short text) | ~10–30 ms | included in retrieval_ms |
| Chroma ANN (N≈200, k=20) | <10 ms | — |
| Prompt + LLM (Groq) | ~1.0–2.5 s | p95 <5 s end-to-end |
| Validation | <20 ms | — |
| Trace write | <5 ms | — |
| Full rebuild (14 pages) | fetch ~2 s/page + embed ~seconds | <5 min |

Sizing assumptions: 14 indexed pages → ~150–300 chunks → 384-d float32 vectors → <1 MB of embeddings. The dataset fits comfortably in memory, so there is no sharding, batching-at-query, or cache tier. The only meaningful latency is the LLM call, which is why the timeout (30 s) and the extractive fallback exist.

Scaling limits are explicit: the design holds to roughly a few hundred pages and a single user. Beyond that, the first things to break are (a) full re-index cost, (b) MiniLM paraphrase recall, (c) Chroma single-process access. §18 names the intended upgrade points.

---

## 16. Testing architecture

| Level | Scope | Fixtures | Asserts |
|---|---|---|---|
| Unit – extract | Saved HTML per core page | `tests/fixtures/*.html` | Expected facts present (§5.3); denylisted sections absent |
| Unit — chunk | Section JSON | inline dicts | 1 fact = 1 chunk; no expected fact split; metadata complete; IDs stable |
| Unit — guardrails | Pure functions | query strings | PII/advice/performance classification; no false positive on `₹500`/`1.02%` |
| Unit — validation | Answer JSON | synthetic | numeric grounding, sentence/word limits, citation allowlist |
| Unit — citation | Registry | — | `source_id` → URL; non-allowlisted domain rejected |
| Integration | load → chunk → embed → index | cached raw pages | counts line up; collection matches chunks; smoke query returns expected fact in top 3 |
| Contract | Adapter interface | fake client | `LLMClient` conformance; provider factory error messages |
| End-to-end | 30-query golden set via `QueryService` | `golden_set.jsonl` | PRD §14.2 metrics |
| Regression | Snapshot | committed counts + fact values | Fail on silent source drift or chunk-count changes |

Test isolation rule: **no unit or integration test touches the network.** Live fetching is exercised only by `sources validate` and is never part of `pytest`. This keeps CI deterministic and avoids hammering Groww during development.

---

## 17. Failure modes and degradation matrix

Every failure has a defined, user-visible outcome. Nothing "falls back" to an ungrounded answer.

| # | Failure | Detected at | System behaviour | User sees |
|---|---|---|---|---|
| F1 | Source returns 403 (HDFC pages) | P0/P1 | Entry marked `citation_only`; never fetched; still resolvable for citations | Normal answers; refusals can link there |
| F2 | Groww HTML structure changed | P1 extractor | `missing_expected` non-empty → loader exits non-zero | Build-time error naming the field; demo uses last good snapshot |
| F3 | Expected fact absent from all chunks | P2 report | Build warning; fact marked unsupported | Not-found with the scheme link (never a guess) |
| F4 | Embedding model changed in config | P3/P4/startup | Refuse to index/query a mismatched collection | Startup error: “re-run embed + index” |
| F5 | Chroma dir missing or count mismatch | Startup | Fail fast with remediation command | Startup error, not a wrong answer |
| F6 | Query has no scheme and no session scheme | L2 | Clarify | “Which scheme do you mean?” + AMC link |
| F7 | Retrieval below threshold | L3 | Not-found | “I couldn't find that fact in the indexed public pages.” + scheme link |
| F8 | Wrong scheme retrieved (cross-scheme) | L3 filter / L6.5 | Validator rejects scheme mismatch → extractive or not-found | Correct-scheme answer or safe not-found |
| F9 | LLM timeout / 5xx / rate limit | L5 | One retry → secondary model (if configured) → extractive | Correct answer, `mode=extractive` in trace |
| F10 | LLM returns non-JSON | L5/L6.1 | Retry once → extractive | Correct answer; failure visible in inspector |
| F11 | LLM invents a URL | L6.4/G6 | Rejected; URL never rendered | Answer with registry URL, or not-found |
| F12 | LLM invents a number | L6.6 | Numeric grounding fails → extractive | Correct value from the fact card |
| F13 | LLM exceeds 3 sentences | L6.7 | Trim → extractive | Compliant answer |
| F14 | LLM slips in advice/performance language | L6.8 | Banned-term check → refusal template | Deterministic refusal + educational link |
| F15 | PII present in query | G1 | Refuse; clear input; log hash only | PII refusal; value not echoed |
| F16 | Prompt injection inside page text | G4 | Instruction lines stripped; context framed as untrusted | Normal factual answer |
| F17 | Prompt injection in user text | G1 | Refused as out-of-scope | Polite refusal |
| F18 | API key missing/invalid | Startup | Fail fast naming the env var | Startup error |
| F19 | Disk full / unwritable trace path | L7 | Answer still renders; log warning | Answer + warning in logs |
| F20 | Two concurrent queries | L3/L7 | Chroma read-only queries are safe; request IDs prevent trace collision | Both answer normally |

**Degradation ladder (never violated):** grounded LLM answer → grounded extractive answer → not-found → refusal. The system never moves *up* this ladder, and never answers without a citation.

---

## 18. Extension points

The design is closed around four explicit seams. Everything else is expected to stay still.

| Seam | Interface | Add a … | Cost |
|---|---|---|---|
| Source | `sources.yaml` entry | New page or scheme | Validate URL → load → chunk → embed → index → eval |
| Chunk strategy | `Chunker` protocol (`chunk(sections) -> list[Chunk]`) | New strategy | Config switch + rebuild; old artifacts preserved |
| Embedding backend | `Embedder` protocol (`encode(texts) -> np.ndarray`) | Different model/runtime | New collection name + full re-embed |
| Vector store | `VectorStore` protocol (`upsert/query/delete/count`) | pgvector, Qdrant, FAISS | Adapter + config provider switch |
| LLM | `LLMClient` protocol | New provider | ~80 lines adapter + env key |
| Guardrail | `Validator` protocol (`check(answer, ctx) -> Verdict`) | New rule | Register in the validator chain, add a test |

What is intentionally **not** pluggable in v1: the answer contract (one sentence-bounded body, one link, one date), the citation-from-registry rule, and the refusal policy. Those are product guarantees, not configuration.

Natural v2 upgrades, in the order they would be needed:

1. **More schemes/AMCs** → per-AMC alias tables, `amc` filter, larger collection, threshold retune.
2. **Brighter corpus** → replace MiniLM with a stronger embedder; the collection-name rule already prevents mixed vectors.
3. **Scheduled refresh** → cron around the existing 5-command chain; add `changed_sources` diff report before re-index.
4. **Multi-user hosting** → move Chroma behind a service, add auth, disable inspector, add per-request rate limits.
5. **Regional languages** → multilingual embedder + language-aware prompts; refusal templates need translation review.

---

## 19. Architecture decisions (ADR summary)

Full rationale lives in `PRD.md` Appendix F; this table records the *architectural* consequence.

| ADR | Decision | Architectural consequence | Reversible? |
|---|---|---|---|
| AD-01 | Stage-separated batch pipeline with file artifacts | 8 CLI stages; immutable `data/0N_*`; inspector reads files, not services | Hard (structural) |
| AD-02 | Structure-aware fact cards, not fixed windows | Chunker is a rule table by section type; metadata is rich; IDs deterministic | Yes (re-chunk + re-embed) |
| AD-03 | Citation resolved from `source_id` in a registry | LLM output schema has no URL field; renderer is the only URL writer | No (product guarantee) |
| AD-04 | Deterministic pre-LLM refusals | Guardrails are pure functions ahead of retrieval; model cannot override | No |
| AD-05 | Performance/holdings dropped at ingestion | Extractor denylist; retrieval cannot surface what was never stored | No |
| AD-06 | Embedding model identity bound to collection name | Model change = new collection; startup mismatch check | Yes (rebuild) |
| AD-07 | Provider-agnostic `LLMClient`; model from `.env` | Adapters isolated under `app/llm/`; no vendor import above that layer | Yes |
| AD-08 | Extractive fallback as the last resort | Answer is mechanical from fact cards; always cited; flagged in trace | Yes |
| AD-09 | Local single-process deployment | No queue, no DB server, no cache layer; inspector is local-only | Yes (v2) |
| AD-10 | Raw query not stored for PII cases | Tracer stores hash + types only; prompt/response withheld | No (privacy) |

---

## 20. Traceability: PRD → architecture

| PRD requirement | Where the architecture satisfies it |
|---|---|
| §6.6 inspect every stage | §4.1 ArtifactReader, §5.1 pipeline contract, §11.3 inspector tabs |
| §7.1 stage artifacts + “no silent recompute” | §5.1 invariants, §5.8 idempotency, §6.1 layout |
| §8.3 ingestion drops performance | §5.3 denylist, §9.1 performance gate, AD-05 |
| §8.4 fact-card chunking | §5.4 rule table, §6.2 chunk schema |
| §8.5 MiniLM embeddings | §5.5 manifest + norm assertions |
| §8.6 Chroma collection | §5.6 flat metadata, §6.3 query surface |
| §8.7 retrieval + scheme resolution | §7.3, §7.4 |
| §8.8 generation + citations + guardrails | §7.1, §8, §9 |
| §6.2 one link + ≤3 sentences + date | §9.4 renderer chokepoint |
| §6.4 allowlist-only citations | §5.2 registry validation, §9.3 validator 4 |
| §9.2 model from `.env`; 5 providers | §8.1–8.3 |
| §12.1 PII never stored | §9.1, §12.2, AD-10 |
| §12.4 numeric grounding | §9.3 validator 6 |
| §13 UI + inspector | §11 |
| §14 evaluation | §5.7 harness over `QueryService` |
| §16 acceptance / demo | §14 topology, §15 budgets, §17 matrix |
| §17 risks | §17 failure mode column + §18 upgrade seams |

---

## 21. Open technical questions

| # | Question | Working default | Decision needed by |
|---|---|---|---|
| 1 | Extract with `selectolax` or `bs4`? | selectolax, bs4 as fallback | P1 build |
| 2 | Is Playwright needed for any *core* Groww page? | No — all 5 core pages returned 200 to a bot UA | P1 build (verify at implementation) |
| 3 | Does Groq expose `qwen/qwen3.8-27b` on the demo account? | Try it; else `openai/gpt-oss-120b` | Before P6 demo |
| 4 | Exact `min_similarity` after tuning? | 0.35, tuned on golden set | P5 gate |
| 5 | Store full prompt for non-PII traces? | Yes (needed for inspector) | P6 build |
| 6 | Sentence splitting library? | `regex`-based splitter with abbreviation guards, not `nltk` | P6 build |
| 7 | Does the inspector need live Chroma queries or only stats? | Live read-only query for tab 4 | P7 build |
| 8 | Token counting for budgets? | `tiktoken` cl100k as a proxy (not provider-exact) | P2 build |

None of these change the component boundaries; all are implementation-detail choices with a documented default.

---

## 22. Build sequence (architecture-driven)

The order is not arbitrary: each step produces the artifact the next step inspects.

```text
1. Skeleton + config         ┌ app/config.py, config.yaml, sources.yaml, .env.example,
   + schemas                  │ app/schemas.py, data/ dirs, CLI stub
                              ▼
2. P0 sources validate       → registry + validation report          [gate: 21 URLs, 4 bot-403 marked]
                              ▼
3. P1 load/extract           → raw HTML/TXT/JSON + manifest          [gate: 5/5 core, no missing facts]
                              ▼
4. P2 chunk                  → chunks.jsonl + report                 [gate: 1 fact = 1 chunk, metadata]
                              ▼
5. P3 embed                  → npy + ids + manifest                  [gate: count/dim/norm]
                              ▼
6. P4 index                  → Chroma + stats                        [gate: count match, smoke query]
                              ▼
7. Guardrails (pure)         → PII/intent/citation/validators + unit tests   ← can start earlier, in parallel
                              ▼
8. P5 retrieval              → retrieve CLI + traces                 [gate: hit@3 ≥ 90%]
                              ▼
9. P6 generation             → adapters, prompt, validation, fallback [gate: accuracy ≥ 90%, citation 100%]
                              ▼
10. P7 UI + inspector + eval → Streamlit + eval report + docs + video [gate: ≤3 min demo]
```

Two sequencing notes that matter architecturally:

- **Guardrails (step 7) have no upstream dependency** and should be built and tested while the data pipeline is still settling. They are pure functions; delaying them to the end is the most common way this design degrades into “prompt-only” behaviour.
- **Retrieval is evaluated before generation is built** (step 8 before 9), so a bad answer is attributable to either retrieval or generation, never to both at once.

---

## 23. Summary of architectural guarantees

1. **Every rendered response has exactly one registry URL and one source date** — structurally, because only the renderer writes URLs.
2. **No answer exists without a supporting chunk** — the evidence gate precedes generation, and schema validation forbids a citation outside the context.
3. **No number exists without grounding** — every numeric token is checked against the cited chunk.
4. **No advice, no performance claims, no PII** — enforced before retrieval, after generation, and by omission at ingestion.
5. **Every stage is inspectable from stored files** — raw → chunk → vector → match → prompt → response → verdict, without re-running anything.
6. **Models and stores are configuration** — the pipeline depends on four protocols and never on a vendor.
7. **Every failure degrades to a safe, cited outcome** — never to an unsourced guess.

---

*End of architecture document. Requirements, phases, and acceptance criteria remain authoritative in `PRD.md`.*
