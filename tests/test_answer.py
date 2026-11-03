"""Phase 6 gate: guarded answers, tolerant parsing, validation, and fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import load_settings
from app.llm.client import LLMError, LLMResponse
from app.pipeline.answer import AnswerService, enforce_length, load_answer_trace, sanitize_body
from app.sources import load_registry
from test_retrieve import FakeEncoder, prepare

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LARGE_CAP = "hdfc-large-cap-direct-growth"


class FakeLLM:
    name = "fake"
    model = "fake-model"

    def __init__(self, payload: object | str | Exception) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        text = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload)
        )
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=3,
            tokens_in=11,
            tokens_out=22,
            finish_reason="stop",
        )


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "answered",
        "reason": None,
        "answer": "The expense ratio for HDFC Large Cap Fund Direct Growth is 1.02%.",
        "fact_type": "expense_ratio",
        "scheme_slug": LARGE_CAP,
        "source_id": "S1",
        "confidence": 0.92,
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def settings(tmp_path):
    return prepare(tmp_path)


@pytest.fixture()
def registry():
    return load_registry(PROJECT_ROOT / "config" / "sources.yaml")


def service(settings, registry, llm: FakeLLM | None = None) -> AnswerService:
    return AnswerService(
        settings,
        registry=registry,
        llm_client=llm,
        encoder=FakeEncoder(),
    )


def test_llm_answer_is_rendered_with_one_link_and_one_date(settings, registry) -> None:
    summary = service(settings, registry, FakeLLM(valid_payload())).answer_with_trace(
        "expense ratio of HDFC Large Cap"
    )

    assert summary.result.status == "answered"
    assert summary.result.mode == "llm"
    assert "1.02%" in summary.result.body
    assert "http" not in summary.result.body
    assert summary.result.citation_url.startswith("https://groww.in/")
    assert summary.result.last_updated_from_sources == "2026-10-30"
    assert summary.validation is not None
    assert summary.validation.json_ok
    assert summary.validation.citation_ok
    assert summary.validation.scheme_ok
    assert summary.validation.numbers_grounded
    assert summary.trace_path.exists()
    assert summary.retrieval_path is not None and Path(summary.retrieval_path).exists()

    trace = load_answer_trace(settings, summary.result.request_id)
    assert trace is not None
    assert trace.final == summary.result
    assert trace.llm["called"] is True


def test_ungrounded_number_falls_back_to_the_fact_card(settings, registry) -> None:
    summary = service(
        settings,
        registry,
        FakeLLM(valid_payload(answer="The expense ratio is 1.20%.")),
    ).answer_with_trace("expense ratio of HDFC Large Cap")

    assert summary.result.status == "answered"
    assert summary.result.mode == "extractive"
    assert "1.02%" in summary.result.body
    assert "1.20%" not in summary.result.body


def test_wrong_scheme_falls_back_to_the_resolved_scheme(settings, registry) -> None:
    summary = service(
        settings,
        registry,
        FakeLLM(
            valid_payload(
                answer="The expense ratio for HDFC Mid Cap Fund Direct Growth is 1.02%.",
                scheme_slug="hdfc-mid-cap-direct-growth",
            )
        ),
    ).answer_with_trace("expense ratio of HDFC Large Cap")

    assert summary.result.mode == "extractive"
    assert summary.result.scheme_slug == LARGE_CAP
    assert "HDFC Large Cap" in summary.result.body


def test_invalid_json_falls_back_to_extractive(settings, registry) -> None:
    summary = service(settings, registry, FakeLLM("not json at all")).answer_with_trace(
        "expense ratio of HDFC Large Cap"
    )

    assert summary.result.status == "answered"
    assert summary.result.mode == "extractive"
    assert "1.02%" in summary.result.body


def test_provider_error_falls_back_to_extractive(settings, registry) -> None:
    summary = service(settings, registry, FakeLLM(LLMError("boom"))).answer_with_trace(
        "expense ratio of HDFC Large Cap"
    )

    assert summary.result.mode == "extractive"
    assert summary.result.status == "answered"
    assert summary.result.reason == "llm_error"
    assert summary.llm.get("error_kind") == "provider"


def test_no_llm_client_uses_extractive_mode(settings, registry) -> None:
    summary = service(settings, registry).answer_with_trace(
        "expense ratio of HDFC Large Cap"
    )

    assert summary.result.mode == "extractive"
    assert "1.02%" in summary.result.body


def test_missing_scheme_clarifies_with_one_link(settings, registry) -> None:
    result = service(settings, registry).answer("Expense ratio?")

    assert result.status == "clarify"
    assert result.reason == "missing_scheme"
    assert result.citation_url.startswith("https://")
    assert result.last_updated_from_sources


def test_other_amc_is_refused(settings, registry) -> None:
    result = service(settings, registry).answer("Expense ratio of SBI Bluechip Fund")

    assert result.status == "refused"
    assert result.reason == "out_of_scope"
    assert "SBI" not in result.body


def test_pii_is_refused_and_not_stored_in_traces(settings, registry) -> None:
    summary = service(settings, registry).answer_with_trace(
        "My PAN is ABCDE1234F, what is the expense ratio?"
    )

    assert summary.result.status == "refused"
    assert summary.result.reason == "pii"
    assert "ABCDE1234F" not in summary.result.body

    retrieval_payload = json.loads(Path(summary.retrieval_path).read_text(encoding="utf-8"))
    answer_payload = json.loads(summary.trace_path.read_text(encoding="utf-8"))
    assert retrieval_payload["query_redacted"] is None
    assert "ABCDE1234F" not in json.dumps(answer_payload)


def test_advice_refusal_uses_one_educational_link(settings, registry) -> None:
    result = service(settings, registry).answer("Should I buy HDFC Mid Cap Fund?")

    assert result.status == "refused"
    assert result.reason == "advice"
    assert result.citation_url.startswith("https://groww.in/")


def test_definition_question_uses_definition_chunks(settings, registry) -> None:
    # The temp corpus has no definition chunk, so this must withhold an answer
    # rather than invent one; the real corpus answers from AMFI/SEBI chunks.
    result = service(settings, registry).answer("What is a riskometer?")

    assert result.status in {"not_found", "refused"}
    assert "riskometer" in result.body.lower() or result.reason


# --------------------------------------------------------------------- helpers


def test_sanitize_body_removes_urls_markdown_and_tables() -> None:
    dirty = "See https://example.com and [link](https://x.test) | **bold** <b>x</b>"
    cleaned = sanitize_body(dirty)

    assert "http" not in cleaned
    assert "[link]" not in cleaned
    assert "|" not in cleaned
    assert "<b>" not in cleaned


def test_enforce_length_keeps_at_most_three_sentences_and_seventy_words() -> None:
    text = "One two. Three four. Five six. Seven eight."
    trimmed = enforce_length(text)

    assert trimmed == "One two. Three four. Five six."

    long_sentence = " ".join(["word"] * 100) + "."
    trimmed_long = enforce_length(long_sentence)
    assert len(trimmed_long.split()) <= 70


def test_configured_settings_still_load_without_a_key(tmp_path) -> None:
    # Load the shipped config only: a developer's .env may select another
    # provider, and that is not what this test is about.
    settings = load_settings(
        PROJECT_ROOT / "config" / "config.yaml",
        env_file=tmp_path / ".env",
    )
    assert settings.llm_provider == "groq"
    assert settings.generation.model
