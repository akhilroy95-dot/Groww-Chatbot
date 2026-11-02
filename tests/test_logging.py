"""Phase 0 gate: nothing sensitive can be written to the logs."""

from __future__ import annotations

import json

import pytest

from app.logging import (
    REDACTED,
    configure_logging,
    find_pii,
    fingerprint,
    is_sensitive_key,
    log_event,
    log_file_path,
    redact,
    redact_text,
    reset_logging,
)


@pytest.fixture(autouse=True)
def clean_handlers():
    reset_logging()
    yield
    reset_logging()


@pytest.mark.parametrize(
    "text, secret",
    [
        ("My PAN is ABCDE1234F, check my folio", "ABCDE1234F"),
        ("Aadhaar 1234 5678 9012", "1234 5678 9012"),
        ("account number 123456789012 for HDFC", "123456789012"),
        ("OTP is 482913, verify my SIP", "482913"),
        ("mail me at rahul@example.com", "rahul@example.com"),
        ("call me on 9876543210", "9876543210"),
        ("IFSC HDFC0001234", "HDFC0001234"),
    ],
)
def test_pii_is_redacted(text: str, secret: str) -> None:
    redacted = redact_text(text)

    assert secret not in redacted
    assert "REDACTED" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "expense ratio is 1.02%",
        "minimum SIP is Rs 500",
        "ELSS lock-in is 3 years",
        "benchmark NIFTY 500 TRI",
        "HDFC Large Cap Fund Direct Growth",
        "exit load 1% if redeemed within 1 year",
        "How do I download my capital gains statement?",
    ],
)
def test_fact_text_is_not_mistaken_for_pii(text: str) -> None:
    assert redact_text(text) == text
    assert find_pii(text) == {}


def test_find_pii_groups_by_kind() -> None:
    found = find_pii("PAN ABCDE1234F and email a@b.co")

    assert found["PAN"] == ["ABCDE1234F"]
    assert found["EMAIL"] == ["a@b.co"]


def test_sensitive_keys_are_identified_without_hitting_token_count() -> None:
    for key in ("api_key", "apiKey", "Authorization", "password", "access_token", "pan", "user_pan"):
        assert is_sensitive_key(key), key
    assert not is_sensitive_key("token_count")
    assert not is_sensitive_key("event")
    assert not is_sensitive_key("scheme_slug")


def test_nested_redaction_drops_secrets_but_keeps_metrics() -> None:
    payload = {
        "api_key": "sk-live-123",
        "Authorization": "Bearer abc",
        "pan": "ABCDE1234F",
        "token_count": 42,
        "nested": {"email": "a@b.co", "scheme": "hdfc-large-cap"},
        "list": ["9876543210", "1.02%"],
    }

    safe = redact(payload)

    assert safe["api_key"] == REDACTED
    assert safe["Authorization"] == REDACTED
    assert safe["pan"] == REDACTED
    assert safe["token_count"] == 42
    assert safe["nested"]["email"] == REDACTED
    assert safe["nested"]["scheme"] == "hdfc-large-cap"
    assert safe["list"][0] == "[REDACTED:PHONE]"
    assert safe["list"][1] == "1.02%"


def test_log_event_writes_one_redacted_json_line(tmp_path) -> None:
    log_event(
        "query_received",
        data_dir=tmp_path,
        query="my PAN is ABCDE1234F",
        api_key="sk-secret-value",
        token_count=7,
    )

    path = log_file_path(tmp_path)
    assert path == tmp_path / "logs" / "app.jsonl"
    raw = path.read_text(encoding="utf-8")

    assert "ABCDE1234F" not in raw
    assert "sk-secret-value" not in raw
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "query_received"
    assert record["level"] == "INFO"
    assert record["api_key"] == REDACTED
    assert record["query"] == "my PAN is [REDACTED:PAN]"
    assert record["token_count"] == 7
    assert record["ts"].endswith("Z")


def test_configure_logging_is_idempotent(tmp_path) -> None:
    import logging

    logger = configure_logging(tmp_path)
    configure_logging(tmp_path)

    own = [h for h in logger.handlers if getattr(h, "_groww_handler", False)]
    assert len(own) == 1
    assert isinstance(own[0], logging.FileHandler)


def test_fingerprint_is_stable_short_and_non_reversible() -> None:
    first = fingerprint("ABCDE1234F")
    second = fingerprint("ABCDE1234F")

    assert first == second
    assert len(first) == 8
    assert first != fingerprint("ABCDE1234E")
    assert "ABCDE1234F" not in first
