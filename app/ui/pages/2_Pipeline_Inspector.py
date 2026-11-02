"""Pipeline Inspector: read-only evidence for stages 1-6.

This page never imports ``AnswerService`` and never embeds a query.  It opens the
files the pipeline wrote (raw manifest, chunks, embeddings, Chroma metadata,
retrieval/answer traces) and renders them in pipeline order.

Gated by ``?debug=1`` or ``APP_ENV=local``; otherwise it shows a local-only
notice and no data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import streamlit as st  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.pipeline.chunk import estimate_tokens  # noqa: E402
from app.ui.artifacts import ArtifactReader  # noqa: E402

_VALIDATORS = {
    "json_ok": "Response parsed as JSON with the required keys",
    "citation_ok": "Cited source exists in the retrieved context and body has no URL",
    "scheme_ok": "Cited chunk belongs to the resolved scheme",
    "numbers_grounded": "Every number appears verbatim in the cited chunk and the length limits hold",
    "pii_echo": "No PII echoed in the answer body",
    "performance_claim": "No performance claim or banned term in the answer body",
}


@st.cache_resource(show_spinner=False)
def _settings():
    return load_settings()


@st.cache_resource(show_spinner=False)
def _reader():
    return ArtifactReader(_settings())


def _debug_allowed() -> bool:
    debug = str(st.query_params.get("debug", "")).lower()
    environment = os.environ.get("APP_ENV", "").lower()
    return debug in {"1", "true", "yes"} or environment == "local"


def _request_selector(reader: ArtifactReader) -> str | None:
    ids = reader.request_ids()
    if not ids:
        st.info(
            "No request artifacts yet. Ask a question in the Ask view, then come back. "
            "The inspector only shows what the pipeline stored."
        )
        return None

    session_request = st.session_state.get("last_request_id")
    query_request = st.query_params.get("request_id")
    if isinstance(query_request, list):
        query_request = query_request[0] if query_request else None
    default = next(
        (value for value in (query_request, session_request) if value in ids),
        ids[0],
    )
    index = ids.index(default) if default in ids else 0
    return st.selectbox(
        "Request",
        ids,
        index=index,
        help="Newest first. Every tab reads the same request_id.",
    )


def _header(reader: ArtifactReader) -> None:
    stats = reader.collection_stats()
    manifest = reader.embedding_manifest()
    columns = st.columns(4)
    columns[0].metric("sources indexed", len(reader.raw_sources()))
    columns[1].metric("chunks", len(reader.chunks()))
    columns[2].metric("vectors", int(manifest.get("count", 0)))
    columns[3].metric("collection rows", int(stats.get("count", 0)))
    st.caption(
        f"model {manifest.get('embedding_model', 'unknown')} Â· "
        f"dim {manifest.get('dimension', '?')} Â· "
        f"collection {stats.get('collection', 'unknown')} Â· "
        f"chunks sha256 {str(stats.get('chunks_sha256', ''))[:12]}"
    )


def _source_tab(reader: ArtifactReader) -> None:
    st.subheader("1 Â· Source")
    st.caption("Reads data/01_raw/manifest.json and the extracted page JSON.")
    entries = reader.raw_sources()
    if not entries:
        st.warning("No raw manifest. Run: python -m app.cli load")
        return

    ids = [str(entry.get("source_id")) for entry in entries]
    source_id = st.selectbox("Source", ids, key="source_id")
    entry = next((row for row in entries if row.get("source_id") == source_id), {})
    raw = reader.raw_source(source_id) or {}

    left, right = st.columns(2)
    with left:
        st.write(f"**URL** Â· {entry.get('url', '')}")
        st.write(f"**HTTP** Â· {entry.get('http_status', '?')}")
        st.write(f"**Fetched at** Â· {entry.get('fetched_at', '?')}")
        st.write(f"**Source date** Â· {entry.get('source_date', '?')}")
        st.write(f"**Bytes** Â· {entry.get('bytes', '?')}")
        st.write(f"**Extractor** Â· {entry.get('extractor_version', '?')}")
    with right:
        st.write(f"**SHA-256** Â· `{entry.get('sha256', '')}`")
        st.write(f"**Sections found** Â· {entry.get('sections_found', [])}")
        st.write(f"**Missing expected** Â· {entry.get('missing_expected', [])}")
        st.write(f"**Role** Â· {entry.get('role', '?')}")

    st.markdown("**Extracted facts**")
    facts = raw.get("facts") if isinstance(raw, dict) else None
    if facts:
        st.json(facts)
    else:
        st.caption("No typed facts in this page (narrative support page).")

    st.markdown("**Raw text excerpt**")
    text = reader.raw_text(source_id)
    st.text_area("Cleaned text (first 4000 chars)", text, height=260, disabled=True)


def _chunks_tab(reader: ArtifactReader) -> None:
    st.subheader("2 Â· Chunks")
    st.caption("Reads data/02_chunks/chunks.jsonl. Filtering never rewrites the file.")
    chunks = reader.chunks()
    if not chunks:
        st.warning("No chunks. Run: python -m app.cli chunk")
        return

    left, right = st.columns(2)
    doc_id = left.selectbox("Source", ["all"] + reader.doc_ids(), key="chunk_doc")
    fact_type = right.selectbox("Fact type", ["all"] + reader.fact_types(), key="chunk_fact")

    filtered = [
        chunk
        for chunk in chunks
        if (doc_id == "all" or chunk.doc_id == doc_id)
        and (fact_type == "all" or chunk.fact_type == fact_type)
    ]
    st.write(f"{len(filtered)} of {len(chunks)} chunks")
    st.dataframe(
        [
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "fact_type": chunk.fact_type,
                "chunk_type": chunk.chunk_type,
                "tokens": chunk.token_count,
                "scheme": chunk.scheme_slug,
                "priority": chunk.source_priority,
            }
            for chunk in filtered
        ],
        hide_index=True,
        width="stretch",
    )
    if filtered:
        chunk_id = st.selectbox(
            "Inspect chunk", [chunk.chunk_id for chunk in filtered], key="chunk_id"
        )
        chunk = reader.chunk(chunk_id)
        if chunk is not None:
            st.code(chunk.text, language="text")
            st.json(chunk.model_dump(mode="json"))


def _embeddings_tab(reader: ArtifactReader) -> None:
    st.subheader("3 Â· Embeddings")
    st.caption("Reads data/03_embeddings/*. No model is loaded here.")
    manifest = reader.embedding_manifest()
    if not manifest:
        st.warning("No embedding manifest. Run: python -m app.cli embed")
        return

    st.json(manifest)
    ids = reader.embedding_ids()
    if not ids:
        return
    chunk_id = st.selectbox("Chunk row", ids, key="embedding_chunk")
    vector, index = reader.embedding_row(chunk_id)
    if vector is None:
        st.warning("That chunk id has no vector row.")
        return
    norm = float(np.linalg.norm(vector))
    st.write(f"row {index} Â· dtype float32 Â· dim {vector.shape[0]} Â· L2 norm {norm:.8f}")
    st.write("first 8 values")
    st.code(", ".join(f"{value:.6f}" for value in vector[:8].tolist()), language="text")
    st.caption(
        "Unit norm (max |norm-1| well under 1e-6) is what makes cosine distance "
        "and dot-product MMR interchangeable."
    )


def _vector_db_tab(reader: ArtifactReader, request_id: str | None) -> None:
    st.subheader("4 Â· Vector DB")
    st.caption("Reads Chroma metadata and the stored retrieval trace. Never embeds a query.")
    stats = reader.collection_stats()
    st.json(stats)
    sample = reader.collection_sample()
    if "error" in sample:
        st.error(sample["error"])
    else:
        st.write(
            f"collection `{sample['name']}` Â· rows {sample['count']} Â· "
            f"metadata {sample['metadata']}"
        )
        st.markdown("**First rows**")
        st.dataframe(
            [
                {
                    "id": sample["ids"][position] if position < len(sample["ids"]) else "",
                    "fact_type": (sample["metadatas"][position] or {}).get("fact_type", "")
                    if position < len(sample["metadatas"])
                    else "",
                    "doc_id": (sample["metadatas"][position] or {}).get("doc_id", "")
                    if position < len(sample["metadatas"])
                    else "",
                    "text": str(sample["documents"][position] if position < len(sample["documents"]) else "")[:120],
                }
                for position in range(min(10, len(sample["ids"])))
            ],
            hide_index=True,
            width="stretch",
        )

    trace = reader.retrieval_trace(request_id) if request_id else None
    st.markdown("**Stored retrieval result for this request**")
    if trace is None:
        st.info("No retrieval trace for the selected request.")
        return
    st.write(
        {
            "status": trace.status,
            "intent": trace.intent,
            "scheme": trace.scheme_slug,
            "scheme_source": trace.scheme_source,
            "filters": trace.filters,
            "top_similarity": trace.top_similarity,
            "gate_passed": trace.gate_passed,
            "context_tokens": trace.context_tokens,
        }
    )
    st.dataframe(
        [
            {
                "rank": rank,
                "chunk_id": candidate.chunk_id,
                "similarity": round(candidate.similarity, 4),
                "adjusted": round(candidate.adjusted, 4),
                "selected": candidate.chunk_id in trace.selected,
                "preview": candidate.text_preview[:120],
            }
            for rank, candidate in enumerate(trace.candidates, start=1)
        ],
        hide_index=True,
        width="stretch",
    )


def _prompt_tab(reader: ArtifactReader, trace) -> None:
    st.subheader("5 Â· Prompt")
    st.caption("Reads data/06_answers/{request_id}.json. The exact prompt is stored.")
    if trace is None:
        st.info("No answer trace for the selected request.")
        return
    if not trace.prompt:
        st.info(
            "This request never reached generation: the input gate or retrieval gate "
            "returned a terminal answer, so there is no prompt."
        )
        return
    st.write(
        {
            "prompt_sha256": trace.prompt_sha256,
            "estimated_tokens": estimate_tokens(trace.prompt),
            "chars": len(trace.prompt),
        }
    )
    st.code(trace.prompt, language="text")


def _llm_tab(reader: ArtifactReader, trace) -> None:
    st.subheader("6 Â· LLM")
    st.caption("Reads data/06_answers/{request_id}.json. No provider is called here.")
    if trace is None:
        st.info("No answer trace for the selected request.")
        return
    st.json(trace.llm)
    if trace.raw_response:
        st.markdown("**Raw response**")
        st.code(trace.raw_response, language="text")
    else:
        st.info("No raw response: the provider was not called or returned nothing.")


def _validation_tab(reader: ArtifactReader, trace) -> None:
    st.subheader("7 Â· Validation")
    st.caption("Reads the ValidationReport stored with the answer.")
    if trace is None:
        st.info("No answer trace for the selected request.")
        return
    report = trace.validation
    rows = []
    for name, label in _VALIDATORS.items():
        value = getattr(report, name, None)
        if name in {"pii_echo", "performance_claim"}:
            passed = value is False
        else:
            passed = bool(value)
        rows.append(
            {
                "check": name,
                "result": "pass" if passed else "fail",
                "detail": label,
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")
    st.write({"sentences": report.sentences, "words": report.words})
    if trace.llm.get("error"):
        st.warning(f"LLM error: {trace.llm['error']}")
    st.caption(
        "A failed validation never ships model prose: the answer path falls back to "
        "a mechanical extraction from the top fact card."
    )


def _final_tab(reader: ArtifactReader, trace) -> None:
    st.subheader("8 Â· Final")
    st.caption("Reads the rendered AnswerResult. This is what the Ask view prints.")
    if trace is None:
        st.info("No answer trace for the selected request.")
        return
    final = trace.final
    st.write(
        {
            "request_id": final.request_id,
            "status": final.status,
            "reason": final.reason,
            "mode": final.mode,
            "scheme_slug": final.scheme_slug,
            "source_id": final.source_id,
        }
    )
    st.markdown(final.body)
    st.markdown(f"Source: [{final.citation_label}]({final.citation_url})")
    st.caption(f"Last updated from sources: {final.last_updated_from_sources}")
    if trace.retrieval_path:
        st.caption(f"retrieval trace: {trace.retrieval_path}")
    st.caption(f"answer trace: data/06_answers/{final.request_id}.json")
    if final.evidence:
        st.json(final.evidence)


def main() -> None:
    st.set_page_config(page_title="Pipeline Inspector", page_icon="ðŸ”Ž", layout="wide")
    st.title("Pipeline Inspector")
    st.caption("Read-only artifacts from loading â†’ chunking â†’ embedding â†’ vector DB â†’ retrieval â†’ LLM.")

    if not _debug_allowed():
        st.warning(
            "This inspector is local-only. Add ?debug=1 to the URL or set "
            "APP_ENV=local to view pipeline artifacts."
        )
        return

    reader = _reader()
    _header(reader)
    st.divider()
    request_id = _request_selector(reader)
    trace = reader.answer_trace(request_id) if request_id else None
    if trace is not None:
        st.caption(
            f"request {request_id} Â· status {trace.final.status} Â· mode {trace.final.mode} Â· "
            f"prompt {trace.prompt_sha256[:12] if trace.prompt_sha256 else 'none'}"
        )

    tabs = st.tabs(
        [
            "1 Source",
            "2 Chunks",
            "3 Embeddings",
            "4 Vector DB",
            "5 Prompt",
            "6 LLM",
            "7 Validation",
            "8 Final",
        ]
    )
    with tabs[0]:
        _source_tab(reader)
    with tabs[1]:
        _chunks_tab(reader)
    with tabs[2]:
        _embeddings_tab(reader)
    with tabs[3]:
        _vector_db_tab(reader, request_id)
    with tabs[4]:
        _prompt_tab(reader, trace)
    with tabs[5]:
        _llm_tab(reader, trace)
    with tabs[6]:
        _validation_tab(reader, trace)
    with tabs[7]:
        _final_tab(reader, trace)


main()
