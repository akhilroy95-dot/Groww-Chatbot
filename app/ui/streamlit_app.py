"""Streamlit Ask view: one welcome, three examples, one factual answer.

Run with::

    streamlit run app/ui/streamlit_app.py

The page owns almost no state.  ``last_scheme`` and ``last_request_id`` are the
only application values persisted in ``st.session_state``; the rendered answer
is re-read from ``data/06_answers/{request_id}.json`` so what the user sees is
exactly what the pipeline recorded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ``streamlit run`` puts app/ui on sys.path, not the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.pipeline.answer import AnswerService, load_answer_trace  # noqa: E402
from app.pipeline.retrieve import trace_for  # noqa: E402
from app.schemas import AnswerResult  # noqa: E402

INPUT_KEY = "question_text"


@st.cache_resource(show_spinner=False)
def _settings():
    return load_settings()


@st.cache_resource(show_spinner=False)
def _service():
    return AnswerService(_settings())


def _initialise_state() -> None:
    st.session_state.setdefault("last_scheme", None)
    st.session_state.setdefault("last_request_id", None)


def _fill(example: str) -> None:
    """Example buttons fill the input; they never submit the form."""
    st.session_state[INPUT_KEY] = example


def _submit() -> None:
    question = str(st.session_state.get(INPUT_KEY, "")).strip()
    if not question:
        return
    with st.spinner("Searching indexed sources…"):
        try:
            summary = _service().answer_with_trace(
                question,
                last_scheme=st.session_state.get("last_scheme"),
            )
        except Exception as exc:  # pragma: no cover - surfaced in the demo UI
            st.session_state["last_request_id"] = None
            st.error(f"Could not answer that question: {exc}")
            return

    st.session_state["last_request_id"] = summary.result.request_id
    if summary.result.status == "answered" and summary.result.scheme_slug:
        st.session_state["last_scheme"] = summary.result.scheme_slug
    if summary.result.reason == "pii":
        # Clear the widget value in the callback, before it is instantiated.
        st.session_state[INPUT_KEY] = ""


def _footer() -> None:
    settings = _settings()
    try:
        manifest = json.loads(
            (settings.data_dir_path / "03_embeddings" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        refreshed = str(manifest.get("created_at", ""))[:10]
    except Exception:
        refreshed = "unknown"
    st.caption(
        f"{settings.ui.disclaimer_short} · index refreshed {refreshed or 'unknown'} · "
        "Do not paste PAN, Aadhaar, account numbers, OTPs, email, or phone numbers."
    )


def _render_answer(result: AnswerResult) -> None:
    status_line = f"**{result.status.replace('_', ' ').title()}**"
    if result.reason:
        status_line += f" · `{result.reason}`"
    if result.scheme_slug:
        status_line += f" · `{result.scheme_slug}`"
    st.markdown(status_line)

    st.markdown(result.body)
    st.markdown(
        f"Source: [{result.citation_label}]({result.citation_url})"
    )
    st.caption(f"Last updated from sources: {result.last_updated_from_sources}")

    if result.mode == "extractive":
        st.caption(
            "Answer extracted mechanically from the top indexed fact card "
            "(no LLM key configured or LLM output rejected)."
        )

    with st.expander("Why this answer?"):
        retrieval = trace_for(_settings(), result.request_id)
        if retrieval is None:
            st.write("No retrieval trace was stored for this request.")
        else:
            st.write(
                {
                    "intent": retrieval.intent,
                    "scheme": retrieval.scheme_slug,
                    "scheme_source": retrieval.scheme_source,
                    "filters": retrieval.filters,
                    "gate_passed": retrieval.gate_passed,
                    "top_similarity": retrieval.top_similarity,
                    "status": retrieval.status,
                    "reason": retrieval.reason,
                }
            )
            if result.evidence:
                st.markdown("**Cited chunk**")
                st.code(str(result.evidence.get("chunk_id")), language="text")
                st.caption(
                    f"similarity {result.evidence.get('similarity')} · "
                    f"adjusted {result.evidence.get('adjusted')}"
                )
                st.write(result.evidence.get("snippet", ""))
            if retrieval.candidates:
                st.markdown("**Top candidates before the gate**")
                st.dataframe(
                    [
                        {
                            "chunk_id": candidate.chunk_id,
                            "similarity": round(candidate.similarity, 4),
                            "adjusted": round(candidate.adjusted, 4),
                            "selected": candidate.chunk_id in retrieval.selected,
                        }
                        for candidate in retrieval.candidates[:10]
                    ],
                    hide_index=True,
                    width="stretch",
                )
        st.caption(f"trace: data/06_answers/{result.request_id}.json")


def main() -> None:
    settings = _settings()
    st.set_page_config(page_title=settings.ui.title, page_icon="📄", layout="centered")
    _initialise_state()

    st.title(settings.ui.title)
    st.write(settings.ui.welcome)

    columns = st.columns(len(settings.ui.examples) or 1)
    for column, example in zip(columns, settings.ui.examples):
        with column:
            st.button(
                example,
                on_click=_fill,
                args=(example,),
                width="stretch",
            )

    st.divider()
    with st.form("ask_form", clear_on_submit=False):
        st.text_input(
            "Question",
            key=INPUT_KEY,
            max_chars=settings.guardrails.max_input_chars,
            placeholder="e.g. What is the exit load of HDFC Mid Cap Fund Direct Growth?",
        )
        st.form_submit_button("Ask", type="primary", on_click=_submit)

    st.caption(f"⚠ {settings.ui.disclaimer_short}")
    st.divider()

    request_id = st.session_state.get("last_request_id")
    if request_id:
        trace = load_answer_trace(settings, request_id)
        if trace is not None:
            _render_answer(trace.final)
        else:
            st.info("That request trace is no longer on disk. Ask another question.")

    _footer()


main()
