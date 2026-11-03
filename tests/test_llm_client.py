"""Anthropic adapter: request shape, response parsing, and the key guard."""

from __future__ import annotations

import httpx
import pytest

from app.config import load_settings
from app.llm.client import AnthropicClient, LLMError, LLMUnavailable, create_client


@pytest.fixture
def anthropic_settings(write_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return load_settings(env_file=write_env(""))


def test_anthropic_posts_messages_and_parses_blocks(
    anthropic_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {
                "content": [
                    {"type": "text", "text": '{"answer": '},
                    {"type": "text", "text": '"ok"}'},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            }

    class FakeClient:
        def __init__(self, timeout: float | None = None) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, url: str, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    client = create_client(anthropic_settings)
    assert isinstance(client, AnthropicClient)
    response = client.complete(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Expense ratio?"},
        ],
        temperature=0.0,
        max_tokens=220,
        timeout_s=30,
        json_mode=True,
    )

    assert len(calls) == 1
    call = calls[0]
    assert str(call["url"]).endswith("/v1/messages")
    assert call["headers"]["x-api-key"] == "test-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"

    payload = call["json"]
    assert payload["system"] == "You are terse."
    assert payload["messages"] == [{"role": "user", "content": "Expense ratio?"}]
    assert "response_format" not in payload

    assert response.text == '{"answer": "ok"}'
    assert response.provider == "anthropic"
    assert response.tokens_in == 12
    assert response.tokens_out == 3
    assert response.finish_reason == "end_turn"


def test_anthropic_error_status_becomes_llm_error(
    anthropic_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResponse:
        status_code = 400
        text = '{"error": {"message": "credit balance is too low"}}'

        def json(self) -> dict[str, object]:  # pragma: no cover - never called
            return {}

    class FakeClient:
        def __init__(self, timeout: float | None = None) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, url: str, headers=None, json=None):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(LLMError) as excinfo:
        create_client(anthropic_settings).complete(
            [{"role": "user", "content": "hi"}],
            temperature=0.0,
            max_tokens=16,
            timeout_s=5,
        )

    assert "HTTP 400" in str(excinfo.value)
    assert "credit balance" in str(excinfo.value)


def test_anthropic_without_a_key_is_unavailable(
    write_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    settings = load_settings(env_file=write_env(""))

    with pytest.raises(LLMUnavailable) as excinfo:
        create_client(settings)
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
