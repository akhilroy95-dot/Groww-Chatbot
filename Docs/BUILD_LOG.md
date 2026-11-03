# Build Log

One entry per phase from `implementation.md`. Each entry records what was built,
what the gate checks, and the exact commands used to prove it.

---

## Phase 0 — Directories and config

**Date:** 2026-11-01
**Status:** complete

### What was built

| Area | Files |
|---|---|
| Repository | `.gitignore`, `.env.example`, `requirements.txt`, `pyproject.toml` |
| Config | `config/config.yaml` (chunking, embedding, vector DB, retrieval, generation, guardrails, UI, evaluation) |
| Loader | `app/config.py` — YAML → `.env` → process env, `${VAR:-default}` expansion, strict pydantic models, `ConfigError`, `config_sha256`, `ensure_data_dirs` |
| Contracts | `app/schemas.py` — source, chunk, embedding manifest, retrieval trace, answer/validation shapes |
| Logging | `app/logging.py` — JSONL `data/logs/app.jsonl`, secret-key redaction, PII span redaction, salted fingerprints |
| CLI | `app/cli.py` — `sources validate`, `load`, `chunk`, `embed`, `index`, `retrieve`, `answer`, `eval`, `doctor` |
| Tests | `tests/conftest.py`, `tests/test_config.py`, `tests/test_cli.py`, `tests/test_logging.py` |

### Directory tree

```text
config/
app/{pipeline,guardrails,llm,ui/pages,eval}/
data/{00_sources,01_raw,02_chunks,03_embeddings,04_vector_db,05_retrieval,06_answers,07_eval,logs}/
tests/fixtures/
```

`data/` and `.env` are gitignored; every `data/*` folder keeps a `.gitkeep` so the
tree survives a fresh clone.

### Design decisions

- **No key required for the pipeline.** `load_settings(require_provider=True)` is
  used only by commands that call a model. Scraping, chunking, embedding,
  indexing, and retrieval must run on a machine with no provider credentials.
- **Strict YAML.** `extra="forbid"` means a typo in `config/config.yaml` fails with
  the exact path (`corpus.fetch.render_js`) instead of being silently ignored.
- **Precedence.** Process environment beats `.env`, which beats `config.yaml`.
  `.env` is loaded with `override=False`.
- **Logs never contain PII.** Free text is redacted span-by-span
  (`[REDACTED:PAN]`), secret fields become `[REDACTED]`, and `token_count` is
  deliberately not treated as a credential.
- **One renderer, one URL.** The LLM output schema (`AnswerSchema`) has no URL
  field; the renderer resolves `source_id` from the registry.

### Gate evidence

```powershell
.\.venv\Scripts\python.exe -m app.cli --help      # 9 commands listed
.\.venv\Scripts\python.exe -m app.cli doctor      # config ok, 9/9 subdirs, no key needed
.\.venv\Scripts\python.exe -m pytest -q           # 48 passed
```

- [x] All directories exist; `data/` and `.env` are gitignored.
- [x] `--help` lists 8 pipeline commands + `doctor`.
- [x] Missing config / unknown provider / empty model name the variable and the fix.
- [x] Pipeline commands run without any provider key.
- [x] Redaction matrix covered by tests (PAN, Aadhaar, account, OTP, email, phone, IFSC).
- [x] False-positive guards: `1.02%`, `Rs 500`, `3 years`, `NIFTY 500 TRI`, scheme names.

### Environment notes

- Only Python 3.15.1 is available on this machine (the plan mentions 3.11). A
  `.venv` was created with it; `requires-python = ">=3.11"` still holds.
- Phase 0 dependencies installed: `pydantic 2.14.2`, `PyYAML 6.0.3`,
  `python-dotenv 1.2.4`, `pytest 9.2.0`. Heavy dependencies (torch, chromadb,
  streamlit) are installed in their phases.

---

## Phase 1 - Scrape the documents

**Date:** 2026-11-01
**Status:** complete

### What was built

| Area | Files |
|---|---|
| Registry | `config/sources.yaml` - 21 public URLs (5 core, 9 support, 7 citation-only), allowlist, aliases, expected facts |
| Registry API | `app/sources.py` - strict loading/validation, role quotas, allowlist matching, fetchable vs citation-only queries |
| Fetch | `app/pipeline/fetch.py` - identifying UA, timeout, 5xx retry/backoff, robots.txt cache with retry, redirect allowlist check, 403 hard stop, byte-exact storage + sha256 |
| Extract | `app/pipeline/extract.py` - `groww_scheme_v1`, `groww_amc_v1`, `article_v1`; narrative sanitizer; policy checks; fact coverage |
| Load | `app/pipeline/load.py` - cache-aware orchestration, HTML/JSON/TXT + `manifest.json`, core-vs-support missing facts |
| Validate | `app/pipeline/validate.py` - registry + live reachability, Markdown report with citation-only rows marked `not_fetched` |
| CLI | `app/cli.py` - `sources validate [--offline]` and `load [--force] [--only ID]` wired to real implementations |
| Tests | `tests/test_sources.py`, `tests/test_extract.py`, `tests/test_fetch.py`, `tests/test_load.py`, fixtures under `tests/fixtures/phase1/` |

### Artifact layout

```text
data/00_sources/validation_report.md
data/01_raw/{source_id}.html   # byte-exact response body
data/01_raw/{source_id}.json   # allowlisted, typed sections only
data/01_raw/{source_id}.txt    # human-readable sibling for the inspector
data/01_raw/manifest.json      # status, sha256, sections, missing facts, source date
```

### Design decisions

- **The registry is the only URL source.** A fetch, render, or citation can only
  use an id that resolves in `config/sources.yaml`. Allowlist matching is exact
  host or subdomain, so `groww.in.evil.example` and `notgroww.in` never match.
- **403 is terminal.** Bot protection is never retried, solved, or bypassed.
  HDFC pages are citation-only and never produce a file in `data/01_raw/`.
- **Robots once per host, with retry.** AMFI's robots endpoint occasionally
  times out; the cache now retries twice with backoff before treating a host as
  unavailable. A 404 robots response means "no restrictions", per the standard.
- **Allowlisted fields, not page text.** Groww scheme pages are read from the
  server-rendered `__NEXT_DATA__` JSON; only identity, cost, minimum, risk,
  benchmark, and lock-in fields are copied. Return series, ratings, holdings,
  peer tables, fund-manager data, and NAV history never enter the artifact.
- **Articles are linearised and filtered.** Groww Help wraps intro copy in bare
  `div`s, so a `div` is accepted when it is a text leaf. AMFI/SEBI use the
  richest of `.page-content-wrapper` / `main` / `article`. Sentences and headings
  mentioning returns, ratings, holdings, performance, or advice are dropped.
- **Fact coverage is semantic, not keyword-fuzzy.** Support pages can cover
  `riskometer` / `expense_ratio` from their sanitised headings and body. Core
  scheme facts must come from allowlisted fields.
- **Policy matching is word-based.** `Return Index` (a benchmark) and
  `Generating` (contains "rating") are not violations; `returns`, `CAGR`,
  `holdings`, and `rating(s)` are.
- **Cache identity includes `fetched_at`.** A second `load` performs zero
  network calls and rewrites byte-identical JSON, so the inspector and eval
  runs are reproducible.
- **Only core gaps fail the build.** A missing support fact is reported as a
  warning; a missing core fact or any fetch/extraction failure exits non-zero.

### Gate evidence

```powershell
.\.venv\Scripts\python.exe -m app.cli load
# fetchable sources: 14
# indexed: 14  (network 3, cache 11)   # completes the corpus after the robots fix

.\.venv\Scripts\python.exe -m app.cli load
# indexed: 14  (network 0, cache 14)   # JSON/TXT sha256 unchanged

.\.venv\Scripts\python.exe -m app.cli sources validate
# registry: 21 sources (core 5, support 9, citation-only 7)
# reachable: 14/14

.\.venv\Scripts\python.exe -m pytest
# 89 passed
```

- [x] 14 pages indexed; every manifest row is HTTP 200.
- [x] No `returns`, `CAGR`, `holdings`, or `rating(s)` in any extracted JSON.
- [x] No HDFC citation-only page appears in `data/01_raw/`.
- [x] Re-running `load` makes zero network calls and produces byte-identical JSON.
- [x] `sources validate` proves 14/14 reachable and skips the 7 citation-only rows.
- [x] 403, redirect-off-allowlist, robots disallow, 5xx retry, and robots timeout
      are covered by transport-level tests.

### Environment notes

- Phase 1 dependencies installed: `httpx 0.28.1`, `beautifulsoup4 4.15.0`,
  `lxml 6.1.3`, `selectolax 1.0.2`, `tenacity 9.2.1`.
- AMFI `robots.txt` was intermittently slow during the first full load; the
  retry path and a 15 s robots timeout make the run deterministic in practice.
- Live values verified on 2026-11-01: Large Cap ER 1.02%, exit load 1% within
  1 year, SIP Rs 100, risk Very High, benchmark NIFTY 100 TRI; Flexi Cap ER
  0.75%; ELSS SIP Rs 500, lock-in 3 years, exit load Nil; Mid Cap ER 0.74%.

---

## Phase 2 - Chunk

**Date:** 2026-11-01
**Status:** complete

### What was built

| Area | Files |
|---|---|
| Chunker | `app/pipeline/chunk.py` - Phase 1 artifact loader/validator, fact card/group/narrative/steps/definition/AMC chunkers, context headers, token-budget windowing, deterministic IDs, dedupe, JSONL/CSV/report writers |
| CLI | `app/cli.py` - real `chunk [--force]` command replacing the Phase 0 stub |
| Tests | `tests/test_chunk.py` (15 tests) and updated `tests/test_cli.py` (chunk help + missing-artifact behavior) |

### Artifact layout

```text
data/02_chunks/chunks.jsonl      # one JSON object per chunk, sorted keys
data/02_chunks/chunks.csv        # flat view of the same rows for inspection
data/02_chunks/chunk_report.md   # counts by source/fact type/token bucket + gaps
```

### Chunk rules implemented (`structure_aware_fact_cards_v1`)

| Section | Output |
|---|---|
| `key_stats`, `exit_load`, `lock_in`, `benchmark` | one `fact_card` per atomic fact (`Value: ...` on one line) |
| `minimum_investments` | one `fact_group` (SIP / lumpsum / additional) |
| `scheme_basics` | one `fact_group` (name, AMC, category, plan, option, launch date, ISIN, registrar) |
| `about` / `investment_objective` | heading-scoped `narrative` windows, 40-token overlap |
| `statement_steps` | one ordered `steps` chunk, numbering preserved |
| `definition` | one `definition` chunk per concept, windowed when long |
| `amc_overview` | summary `fact_card` + key-information `fact_group` |
| AUM | `fact_card` with `fact_type="aum"` (chunk-only fact type) |

- Every chunk repeats `Scheme | AMC | Category | Plan | Option` and `Fact:` inside
  the embedded text, then `Source:` and `Scraped:` at the end.
- `chunk_id = {doc_id}__{fact_type}__{content_sha256[:8]}`; rows are deduped by
  `content_sha256` in deterministic registry order.
- The content policy runs on both the loaded document and every composed chunk.
- Statement `note` sections are excluded (the ELSS note names a PAN password);
  CAS step text is preserved as ordered source instructions.
- **Definition labels come from headings, not body words.** AMFI's TER page calls
  its factual section `Disclaimer`, so the label falls back to the document title
  (`TER of Mutual Fund Schemes`). Conversely, a SEBI ELSS definition that merely
  mentions "lock-in" in a benefits paragraph stays `fact_type=definition`; only
  the explicit lock-in section and fact card are labelled `lock_in`.
- Support/AMC chunks carry `amc="HDFC Mutual Fund"` instead of `scheme_slug`;
  only the four scheme chunks also carry `scheme_slug` / `scheme_name`.
- Token estimate is deterministic `ceil(characters / 4)`; the hard gate is 300
  tokens (config targets 250 for fact cards, 300 for narrative).

### Design decisions

- **No chunk cache.** Chunking is a pure function of the loaded JSON, so every
  run rewrites identical bytes. `--force` is accepted for command parity.
- **Grouping over padding.** The rough `60-120` chunk estimate assumes one chunk
  per key-stat line. Structure-aware grouping deliberately keeps related facts
  together (minimums, scheme basics) and emits concept-level chunks for support
  pages, so the real corpus is **54 chunks**. Gate 2 has no count requirement;
  inventing filler cards would make retrieval noisier, not better.
- **AMC metadata is explicit.** `FactType` has no AUM member, so AUM is chunk-only
  metadata, and Gate 2's "`scheme_slug` (or AMC)" is satisfied by `amc`.

### Gate evidence

```powershell
.\.venv\Scripts\python.exe -m app.cli chunk
# fetchable sources: 14
# chunked: 14/14
# chunks: 54  (tokens min 37, max 292, mean 106.9)
# by fact type: amc_overview 3, aum 4, benchmark 4, definition 6, exit_load 4,
#               expense_ratio 8, lock_in 2, minimum_sip 4, riskometer 9,
#               scheme_basics 6, statement_download 4

.\.venv\Scripts\python.exe -m app.cli chunk   # second run
# same 54 chunk_ids; chunks.jsonl / chunks.csv / chunk_report.md byte-identical
# chunks.jsonl sha256 stable across three runs:
# 706895912782785FE848354BB9CFED118222CD3C014C9D6CA4D5B425218373C6

.\.venv\Scripts\python.exe -m pytest -o addopts="" -q
# 122 passed
```

- [x] Every expected fact appears in at least one chunk (unions count
      `definition`, `steps`, and group coverage); zero missing core/support facts.
- [x] No fact is split: each target value is one `Value:`/label line in one chunk.
- [x] 100% of chunks have `source_url`, `fact_type`, `scraped_at`, and
      `scheme_slug` or `amc`.
- [x] No chunk exceeds 300 tokens (max 292).
- [x] Same `chunk_id` set on a second run; outputs byte-identical.
- [x] No citation-only (`hdfc_*`) document was read or chunked.

### Environment notes

- Phase 2 added no dependencies: stdlib + `pydantic` only.
- The report is a first-class artifact (counts, token buckets, gap list) so the
  Pipeline Inspector can prove chunking without re-running the pipeline.

---

## Phase 3 - Embed

**Date:** 2026-11-02
**Status:** complete

### What was built

| Area | Files |
|---|---|
| Embedder | `app/pipeline/embed.py` - chunk loader/validator, model identity, `content_sha256` cache, lazy SentenceTransformer load, L2 normalization, `EmbeddingManifest`, atomic writes |
| CLI | `app/cli.py` - real `embed [--rebuild]` command replacing the Phase 0 stub |
| Tests | `tests/test_embed.py` (14 tests) |

### Artifact layout

```text
data/03_embeddings/embeddings.npy       # float32 (N, 384), row order = chunks.jsonl
data/03_embeddings/embedding_ids.json   # chunk IDs in .npy row order
data/03_embeddings/manifest.json        # model, revision, count, hashes, timestamp
data/03_embeddings/embedding_cache.jsonl # content_sha256 -> base64 float32 vector
```

### Embedding rules implemented

| Rule | Detail |
|---|---|
| Model | `sentence-transformers/all-MiniLM-L6-v2`, revision `main`, CPU |
| Output | `(N, 384)` `float32`; L2-normalized so cosine = dot product |
| Batch | 32 (configurable) |
| Cache identity | `content_sha256` + `model_key` = SHA-256 of provider, model, revision, dimension, normalize, max sequence length, device, batch size |
| Cache pruning | rows for chunks no longer in `chunks.jsonl` are dropped |
| Ordering | `.npy` rows and `embedding_ids.json` follow the exact `chunks.jsonl` order |
| Integrity | every chunk's `content_sha256` is re-hashed before embedding |

- `embedding.normalize: false` and unsupported providers are rejected with a fix.
- Vectors are re-normalized after encoding even if the model already normalized,
  keeping the gate independent of the encoder implementation.
- A full cache hit preserves the manifest `created_at`, so a rerun is
  byte-identical. Any corpus or model change gets a new timestamp.
- Operational rule: inserting or altering chunks requires `embed` and then
  `index`; the vector DB is never allowed to drift from the manifest.

### Gate evidence

```powershell
.\.venv\Scripts\python.exe -m app.cli embed
# embedding model: sentence-transformers/all-MiniLM-L6-v2 (revision main)
# chunks: 54
# 54 embedded, 0 cached
# shape: (54, 384) float32, normalized=True, max |norm-1|=5.96e-08

.\.venv\Scripts\python.exe -m app.cli embed        # second run
# 0 embedded, 54 cached
# artifacts unchanged (cache hit)

# after the Phase 2 definition-classifier audit changed two chunks:
.\.venv\Scripts\python.exe -m app.cli embed
# 2 embedded, 52 cached
.\.venv\Scripts\python.exe -m app.cli embed
# 0 embedded, 54 cached

.\.venv\Scripts\python.exe -m pytest -o addopts="" -q
# 122 passed
```

Live manifest:

```json
{
  "chunks_sha256": "706895912782785fe848354bb9cfed118222cd3c014c9d6ca4d5b425218373c6",
  "config_sha256": "f69f5e13c2f9a5433448ea8d808855a7c4dc2207625d6fdbe50d7de5f22c7e22",
  "count": 54,
  "dimension": 384,
  "dtype": "float32",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "embeddings_sha256": "80fd8c5b52c7de5104b21fec57f6befa0223fb7b513273da512c1b6335cbfd28",
  "normalized": true,
  "revision": "main"
}
```

- [x] Shape `(54, 384)`, dtype `float32`, max norm error `5.96e-08`.
- [x] Count equals the 54 chunks.
- [x] Second run embeds zero chunks and leaves every artifact byte-identical.
- [x] Only the two changed chunks were re-embedded after the chunk audit.
- [x] Changing the model changes `config_sha256`, the cache namespace, and the manifest.

### Environment notes

- Phase 3 dependencies installed: `numpy 2.5.4`, `sentence-transformers 6.2.0`,
  `torch 2.15.0+cpu`. The MiniLM model downloads to the Hugging Face cache on first use.
- Hugging Face prints an unauthenticated-requests warning; it does not affect the
  artifacts and no token is required.

---

## Phase 4 - Store in the vector DB

**Date:** 2026-11-02
**Status:** complete

### What was built

| Area | Files |
|---|---|
| Indexer | `app/pipeline/index.py` - artifact guards, persistent Chroma client, model guard, stale-ID pruning, batch-100 upsert, flat metadata, five smoke queries, `collection_stats.json` |
| CLI | `app/cli.py` - real `index [--rebuild] [--skip-smoke]` command replacing the Phase 0 stub |
| Tests | `tests/test_index.py` (11 tests) and updated `tests/test_cli.py` (index help + missing-artifact behavior) |

### Artifact layout

```text
data/04_vector_db/chroma/                 # persistent Chroma collection
data/04_vector_db/collection_stats.json   # count, model, dimension, sample IDs, smoke results
```

### Collection rules implemented

| Rule | Detail |
|---|---|
| Provider | `chroma`, persistent client |
| Collection | `groww_mf_facts__minilm_l6_v2__v1` (configurable) |
| Distance | `cosine`; similarity = `1 - distance` |
| Metadata | flat scalars only; `None` and empty strings are dropped |
| Filterable | `scheme_slug`, `fact_type`, `doc_id`, `source_priority`, `source_date`, `amc` |
| Batch | 100 |
| Prune | `existing_ids - current_ids` is deleted in batches |
| Model guard | collection model/revision/dimension/distance must equal config |

- `index` refuses to run if `embeddings` are stale relative to `chunks.jsonl`,
  if the matrix shape/dtype/normalization is wrong, or if
  `embedding_ids.json` is not in chunk-file row order.
- A model change never mixes vectors: the guard raises
  `fix: python -m app.cli index --rebuild`.
- `--rebuild` deletes and recreates the collection, then re-upserts every chunk.
- `--skip-smoke` exists for offline/test runs; the default CLI path runs the gate.

### Gate evidence

```powershell
.\.venv\Scripts\python.exe -m app.cli index --skip-smoke
# vector db: chroma collection groww_mf_facts__minilm_l6_v2__v1 (cosine)
# chunks: 54
# upserted: 54, pruned: 0, count: 54

.\.venv\Scripts\python.exe -m app.cli index
# smoke queries: 5/5 passed

.\.venv\Scripts\python.exe -m pytest -o addopts="" -q
# 134 passed (at this phase; 182 after Phases 5-6)
```

Smoke results from `data/04_vector_db/collection_stats.json`:

| Query | Top result | Pass |
|---|---|---|
| `expense ratio hdfc large cap` | `groww_large_cap__expense_ratio__92f74d22` | yes |
| `exit load hdfc mid cap` | `groww_mid_cap__exit_load__2493eb2f` | yes |
| `minimum sip elss` | `groww_elss__minimum_sip__c90ec4cd` in top 3 | yes |
| `elss lock in` | `sebi_elss__lock_in__f5cf3d5c` in top 3 | yes |
| `download capital gains statement` | `groww_help_tax_proof__statement_download__7367d8cb` | yes |

- [x] Collection count equals the 54 chunks; IDs are unique.
- [x] All five smoke queries return the expected chunk in the top 3.
- [x] `where={"scheme_slug": ...}` narrows results (covered by `test_scheme_filter_narrows_results`).
- [x] Re-index is idempotent; deleting a chunk and re-indexing reproduces the new ID set and count.
- [x] Model change is refused until `index --rebuild`.

### Environment notes

- Chroma `1.5.9` was installed on Python 3.15.1 with `--no-deps` because its
  declared `onnxruntime` dependency has no cp315 wheel. The runtime dependencies
  listed in `requirements.txt` were installed explicitly.
- `onnxruntime` is only imported by Chroma's optional ONNX default embedding
  function. This pipeline always supplies its own MiniLM vectors, so the core
  persistent client, cosine HNSW index, filtering, pruning, and queries all work
  without it.
- Chroma telemetry is disabled in the client settings.

---

## Phase 5 - Retrieval logic

**Date:** 2026-11-02
**Status:** complete

### What was built

| Area | Files |
|---|---|
| Retrieval | `app/pipeline/retrieve.py` - normalization, intent classification, scheme resolution, filter selection, Chroma query, boosts, MMR, gates, context assembly, trace writer |
| Input gate | `app/guardrails/input.py` - deterministic refusals for empty input, PII, prompt injection, advice, performance, other-AMC, and unsupported HDFC schemes |
| Schemas | `app/schemas.py` - `RetrievalTrace`, `AnswerTrace`, `AnswerResult.scheme_slug` |
| CLI | `app/cli.py` - real `retrieve [--query/-q] [--scheme] [--debug]` replacing the Phase 0 stub |
| Tests | `tests/test_retrieve.py` (21), `tests/test_guardrails.py` (9), updated `tests/test_cli.py` |

### Pipeline implemented

```text
question
  -> normalize (lowercase, ER/SIP/ELSS/CAS expansion, punctuation strip)
  -> resolve scheme (alias -> fuzzy >=85 -> session -> clarify)
  -> classify fact type
  -> embed query (same MiniLM model as the index)
  -> Chroma query (candidate_k=20, optional where)
  -> adjust (+0.05 fact match, +0.02 priority-1 source)
  -> MMR (lambda=0.7) -> top_k=8
  -> gate (similarity >= 0.35 AND scheme support)
  -> context: top 5 blocks, deduped by content_sha256, <=1200 tokens
  -> data/05_retrieval/{request_id}.json
```

Resolution details:

- Registry aliases, scheme names, categories, and slug words are matched first.
- The fuzzy fallback strips fact/question words, then compares the remaining
  scheme phrase with `rapidfuzz.fuzz.token_sort_ratio >= 85`.  This keeps typo
  tolerance (`hdfc larg cap` -> Large Cap) without matching every HDFC scheme
  just because the query contains generic words.
- `session_last_scheme` is inherited for scheme-scoped facts.
- A lock-in question falls back to ELSS when no scheme is named.
- Other AMCs and unsupported HDFC categories are refused before any Chroma query.

Filter policy:

| Situation | `where` |
|---|---|
| Scheme resolved, scheme-scoped fact | `{"scheme_slug": slug}` |
| Statement / definition question | `{"fact_type": {"$in": [intent, "definition"]}}` |
| AMC question | `{"doc_id": "groww_amc_hdfc"}` |
| Scheme ambiguous or missing | no filter; return a clarifying question |

Gate outcomes:

| Condition | Result |
|---|---|
| Top similarity `< 0.35` | `not_found` + scheme link |
| No chunk from the resolved scheme | `not_found` + scheme link |
| Two schemes within `0.03` | `clarify` |
| `fact_type == other` and similarity `< 0.5` | `not_found` |

Traces are written for `ok`, `clarify`, `not_found`, and `out_of_scope`; the query
is redacted before it is stored.

### Gate evidence

```powershell
.\.venv\Scripts\python.exe -m app.cli retrieve `
  --query "What is the exit load of HDFC Mid Cap Fund Direct Growth?" --debug
# status: ok
# intent: exit_load   scheme: hdfc-mid-cap-direct-growth (alias)
# filters: {'scheme_slug': 'hdfc-mid-cap-direct-growth'}
# top similarity: 0.770487
# * 1  0.7705  0.8405  exit_load  groww_mid_cap__exit_load__2493eb2f
# * 2  0.7549  0.7749  scheme_basics  groww_mid_cap__scheme_basics__241c4ff3
# * 3  0.7324  0.7524  expense_ratio  groww_mid_cap__expense_ratio__f228cea0
# * 4  0.7168  0.7368  minimum_sip  groww_mid_cap__minimum_sip__db2efc34
# * 6  0.7092  0.7292  aum  groww_mid_cap__aum__7bf26fac
```

A one-off 26-query benchmark over the live collection produced:

```text
hit@3: 26/26 = 100.0%
scheme resolution: 20/20 = 100.0%
cross-scheme context contamination: 0
```

The benchmark covered expense ratio, exit load, minimum SIP, riskometer, and
benchmark across the four schemes (ELSS uses lock-in in place of the last two),
one typo query, a statement question, four definition questions, and the AMC
overview.  Hit means the expected chunk family appears in the top 3 selected
context chunks.

Missing-scheme clarification is proven separately:

```text
'HDFC fund expense ratio'  -> clarify  missing_scheme  filters={}
'Expense ratio?'           -> clarify  missing_scheme  filters={}
'hdfc fund exit load'      -> clarify  missing_scheme  filters={}
```

- [x] hit@3 `100%` on the 26-query set (target `>= 90%`).
- [x] Scheme resolution `100%` on 20 scheme-scoped queries (target `>= 95%`).
- [x] Zero cross-scheme chunks in the context of any resolved query.
- [x] Statement question selects a `groww_help_*` chunk (`groww_help_tax_proof`).
- [x] A scheme-less fact query asks for a scheme instead of guessing.
- [x] Retrieval works with no LLM key; nothing in Phase 5 calls an LLM.
- [x] Traces exist for ok, clarify, not-found, and out-of-scope outcomes.

### Environment notes

- `rapidfuzz 3.14.6` was installed for fuzzy scheme resolution.
- `_load_encoder` first loads the MiniLM model from the local Hugging Face cache
  with `local_files_only=True` and only falls back to the Hub on a fresh machine.
  Cached runs therefore do not depend on network access.

---

## Phase 6 - Answer path and Streamlit UI

**Date:** 2026-11-02
**Status:** complete (extractive mode verified; live LLM mode awaiting `GROQ_API_KEY`)

### What was built

| Area | Files |
|---|---|
| Answer path | `app/pipeline/answer.py` - `AnswerService`, prompt builder, tolerant JSON parse, retry, validation, extractive fallback, renderer, answer trace |
| LLM adapter | `app/llm/client.py` - OpenAI-compatible Groq/OpenAI/Mistral client with JSON mode and typed `LLMUnavailable`/`LLMError`; the answer path owns the retry |
| IDs | `app/ids.py` - dependency-free ULID-like `req_{26 chars}` request IDs |
| Ask view | `app/ui/streamlit_app.py` - welcome, three example buttons, one-question form, answer card, "Why this answer?" |
| Inspector | `app/ui/pages/2_Pipeline_Inspector.py` + `app/ui/artifacts.py` - eight read-only artifact tabs |
| CLI | `app/cli.py` - real `answer [--query/-q] [--scheme] [--debug]` |
| Tests | `tests/test_answer.py` (14), `tests/test_ui.py` (4) |

### Answer path implemented

```text
question
  -> input guardrails
  -> retrieval (Phase 5)
  -> prompt with [S1]..[S5] context blocks
  -> LLM (optional)
  -> tolerant JSON parse (one retry, then fallback model)
  -> validation: schema, citation, scheme, numbers, length, banned terms, PII echo
  -> mechanical extractive answer from the top fact card on any failure
  -> renderer (the only URL writer): body + one Source link + one date
  -> data/06_answers/{request_id}.json
```

Rules enforced by the answer path:

- The LLM is optional.  A missing key, unsupported provider, provider error, or
  invalid JSON never fails the request; it returns a cited sentence extracted
  from the top fact card with `mode=extractive`.
- The model may not add numbers that are absent from the cited chunk, switch to
  another scheme, include a URL, make a performance claim, or echo PII.
- The renderer resolves `source_id` through `config/sources.yaml`; refusals and
  clarifications use the same card shape (one link, one date).
- The answer trace stores the exact user prompt, prompt SHA-256, LLM metadata,
  raw response, validation report, and final `AnswerResult`.
- Request IDs are `req_` plus 26 Crockford-base32 characters, generated without
  an extra dependency.

### UI implemented

Ask view:

- Title, one welcome line, three example buttons, the input form, and
  `Facts-only. No investment advice.` visible on load.
- Example buttons fill the input through a callback; they do not submit.
- Session state is limited to `last_scheme` and `last_request_id` (plus
  Streamlit-managed widget keys).
- A PII refusal clears the input widget in the callback.
- The answer card re-reads `data/06_answers/{request_id}.json`; the expander shows
  the cited chunk, similarity, adjusted score, and candidate table.
- Footer shows the disclaimer, index refresh date, and the no-PII note.

Pipeline Inspector:

| Tab | Stored artifact |
|---|---|
| 1 Source | `01_raw/manifest.json` + `01_raw/{source_id}.json/.txt` |
| 2 Chunks | `02_chunks/chunks.jsonl` |
| 3 Embeddings | `03_embeddings/manifest.json`, `embedding_ids.json`, `embeddings.npy` |
| 4 Vector DB | `04_vector_db/collection_stats.json` + Chroma metadata + retrieval trace |
| 5 Prompt | `06_answers/{request_id}.json` |
| 6 LLM | `06_answers/{request_id}.json` |
| 7 Validation | `06_answers/{request_id}.json` |
| 8 Final | `06_answers/{request_id}.json` |

- Gated by `?debug=1` or `APP_ENV=local`; without either it shows the local-only
  notice and no data.  The config default `app.env: local` is deliberately **not**
  treated as the flag, so the gate is explicit.
- The inspector imports no answer service and embeds no query.  It only reads
  files and opens the Chroma collection for metadata.

### Gate evidence

```powershell
.\.venv\Scripts\python.exe -m app.cli answer `
  --query "What is the expense ratio of HDFC Large Cap Fund Direct Growth?"
# expense ratio   scheme: hdfc-large-cap-direct-growth (alias)
# Expense ratio for HDFC Large Cap Fund Direct Growth: 1.02%.
#
# Source: [Groww - HDFC Large Cap Fund Direct Growth](https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth)
# Last updated from sources: 2026-10-30
#
# status: answered   mode: extractive
# trace: data/06_answers/req_01M6EPP8SQS04B33Q7BJAZNXKC.json
```

Acceptance sweep over the live index (no LLM key, so `mode=extractive`):

| Question | Result | Source |
|---|---|---|
| Large Cap expense ratio | answered `1.02%` | `groww_large_cap` |
| Mid Cap exit load | answered `1% if redeemed within 1 year` | `groww_mid_cap` |
| ELSS minimum SIP | answered `₹500` | `groww_elss` |
| ELSS lock-in | answered `3 years` | `groww_elss` |
| Flexi Cap riskometer | answered `Very High Risk` | `groww_flexi_cap` |
| Large Cap benchmark | answered `NIFTY 100 Total Return Index` | `groww_large_cap` |
| Capital-gains statement | answered download steps | `groww_help_tax_proof` |
| What is a riskometer? | answered definition | `sebi_riskometer` |
| Should I buy HDFC Mid Cap? | refused `advice` | `groww_mid_cap` |
| 3-year return of HDFC Mid Cap? | refused `performance` | `hdfc_factsheets` |
| PAN in the question | refused `pii`, PAN not echoed | `groww_help_cas_what` |
| Expense ratio of SBI Bluechip | refused `out_of_scope` | `amfi_investor` |
| Expense ratio of HDFC Small Cap | refused `out_of_scope` | `groww_amc_hdfc` |

Warm latency over 14 answer calls (extractive mode):

```text
cold start (load settings + model + first answer): 19.72 s
median: 0.052 s
p95:    0.075 s
max:    0.128 s
```

Every call had zero URLs in the body, exactly one `Source:` link, and one
`Last updated from sources: YYYY-MM-DD` line.

Streamlit `AppTest` evidence:

```text
ask view          -> no exceptions, title, 4 buttons (3 examples + Ask)
example click     -> input filled, last_request_id still None
clarify question  -> clarification card with one AMC link
PII question      -> refusal, input cleared back to ''
inspector no flag -> local-only warning, 0 tabs, 0 metrics
inspector debug=1 -> 8 tabs, no exceptions
```

Full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -o addopts="" -q
# 182 passed, 2 warnings
```

- [x] Welcome line, three examples, and the facts-only disclaimer visible on load.
- [x] Every answer has exactly one link and one `Last updated from sources` line.
- [x] All eight inspector tabs populate from stored artifacts.
- [x] Warm p95 `0.075 s` (target `< 5 s`); cold start `19.72 s` (target `< 2 min`).
- [x] Inspector unreachable without `?debug=1` or `APP_ENV=local`.
- [x] PII is refused without echo, and the input is cleared.
- [x] Advice, performance, other-AMC, and unsupported-HDFC refusals each carry one
      relevant link.
- [x] Live LLM call reaches the provider. The supplied `ANTHROPIC_API_KEY`
      authenticates, but the account returns HTTP 400 `credit balance is too
      low`; the answer path then falls back to verified extractive mode with
      `llm_error` shown in the status. A funded key or a Groq key is still
      needed to capture a successful LLM-mode answer.

### Environment notes

- UI dependencies installed: `streamlit 1.66.1`, `pandas 3.1.0`, `pyarrow 26.0.0`
  (binary wheels only).
- The UI uses `width="stretch"` rather than the deprecated
  `use_container_width=True`.
- `.env` was created from `.env.example`; `GROQ_API_KEY` is intentionally blank.
  Without a key the app runs in verified extractive mode.
- Default generation config: Groq with `qwen/qwen3.8-27b`; the client also accepts
  OpenAI, Mistral, and Anthropic (Messages API). Gemini is refused by the
  adapter and falls back to the extractive path.
- Runtime retrieval opens Chroma through `_client(..., read_only=True)`, which
  copies the collection to a temp directory. Chroma rewrites its SQLite/HNSW
  files on open, so without the copy every query dirtied the committed index
  and a read-only deployment filesystem would fail.
- Tests ignore the developer's real `.env` by remapping
  `config.DEFAULT_ENV_PATH`; `tests/test_llm_client.py` covers the Anthropic
  request shape and error handling.
- Run the app with:

```powershell
streamlit run app/ui/streamlit_app.py
```

Open the inspector at `/2_Pipeline_Inspector?debug=1` (or set `APP_ENV=local`).

