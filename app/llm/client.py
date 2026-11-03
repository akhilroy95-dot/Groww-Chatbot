"""Provider-agnostic LLM client.

OpenAI-compatible ``/chat/completions`` transports (Groq, OpenAI, Mistral) and
the Anthropic Messages API are implemented here.  The answer path treats every
provider failure as a signal to use the extractive fallback, so an unsupported
adapter can never turn into an unsourced answer.

No pipeline module imports a vendor SDK: this file is the only place that talks
to a provider over HTTP.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Settings, ConfigError

_DEFAULT_BASE_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "mistral": "https://api.mistral.ai/v1",
}


class LLMError(RuntimeError):
    """The provider could not produce a usable response."""


class LLMUnavailable(LLMError):
    """No key or no supported adapter; callers should fall back."""


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    finish_reason: str | None = None


class LLMClient(Protocol):
    name: str
    model: str

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
        json_mode: bool = True,
    ) -> LLMResponse: ...


class OpenAICompatibleClient:
    """Minimal JSON-mode chat client for OpenAI-compatible APIs."""

    def __init__(self, settings: Settings, *, model: str | None = None) -> None:
        generation = settings.generation
        self.name = generation.provider
        self.model = model or generation.model
        self._settings = settings
        configured = generation.providers.get(self.name)
        self._api_key_env = (
            configured.api_key_env if configured is not None else settings.api_key_env
        )
        self._api_key = os.environ.get(self._api_key_env)
        base_url = (configured.base_url if configured is not None else None) or _DEFAULT_BASE_URLS.get(
            self.name
        )
        if not base_url:
            raise LLMUnavailable(
                f"provider {self.name!r} has no OpenAI-compatible base URL\n"
                "fix: set LLM_PROVIDER to groq, openai, or mistral"
            )
        self._base_url = base_url.rstrip("/")
        if not self._api_key:
            raise LLMUnavailable(
                f"{self._api_key_env} is not set\n"
                f"fix: add {self._api_key_env}=... to .env (answers fall back to extractive mode)"
            )

    @property
    def api_key_env(self) -> str:
        return self._api_key_env

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
        json_mode: bool = True,
    ) -> LLMResponse:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_s) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                # Some OpenAI-compatible providers reject response_format; retry
                # once without it rather than losing the answer.
                if response.status_code >= 400 and json_mode:
                    payload.pop("response_format", None)
                    response = client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name} request failed: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise LLMError(
                f"{self.name} returned HTTP {response.status_code}: {detail}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name} returned an unexpected response shape") from exc

        usage = body.get("usage") or {}
        return LLMResponse(
            text=str(text),
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            tokens_in=_optional_int(usage.get("prompt_tokens")),
            tokens_out=_optional_int(usage.get("completion_tokens")),
            finish_reason=choice.get("finish_reason"),
        )


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class AnthropicClient:
    """Minimal Anthropic Messages API client.

    Anthropic takes the system prompt as a top-level field and returns a list
    of content blocks instead of OpenAI's ``choices``.  There is no
    ``response_format`` switch, so ``json_mode`` is enforced by the prompt.
    """

    def __init__(self, settings: Settings, *, model: str | None = None) -> None:
        generation = settings.generation
        self.name = "anthropic"
        self.model = model or generation.model
        configured = generation.providers.get(self.name)
        self._api_key_env = (
            configured.api_key_env if configured is not None else settings.api_key_env
        )
        self._api_key = os.environ.get(self._api_key_env)
        if not self._api_key:
            raise LLMUnavailable(
                f"{self._api_key_env} is not set\n"
                f"fix: add {self._api_key_env}=... to .env (answers fall back to extractive mode)"
            )
        base_url = (configured.base_url if configured is not None else None) or (
            "https://api.anthropic.com"
        )
        self._base_url = base_url.rstrip("/")

    @property
    def api_key_env(self) -> str:
        return self._api_key_env

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
        json_mode: bool = True,
    ) -> LLMResponse:
        system = "\n\n".join(
            message.get("content", "")
            for message in messages
            if message.get("role") == "system"
        )
        turns = [
            {"role": message["role"], "content": message.get("content", "")}
            for message in messages
            if message.get("role") != "system"
        ]
        payload: dict[str, object] = {
            "model": self.model,
            "messages": turns,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_s) as client:
                response = client.post(
                    f"{self._base_url}/v1/messages",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name} request failed: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise LLMError(
                f"{self.name} returned HTTP {response.status_code}: {detail}"
            )

        try:
            body = response.json()
            blocks = body["content"]
            text = "".join(
                block.get("text", "")
                for block in blocks
                if block.get("type") == "text"
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"{self.name} returned an unexpected response shape") from exc

        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            tokens_in=_optional_int(usage.get("input_tokens")),
            tokens_out=_optional_int(usage.get("output_tokens")),
            finish_reason=body.get("stop_reason"),
        )


def create_client(settings: Settings, *, model: str | None = None) -> LLMClient:
    """Build the active provider client or raise :class:`LLMUnavailable`."""
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings, model=model)
    if settings.llm_provider == "gemini":
        raise LLMUnavailable(
            f"provider {settings.llm_provider!r} is configured but not wired for this demo\n"
            "fix: set LLM_PROVIDER to groq, openai, mistral, or anthropic in .env"
        )
    if settings.llm_provider not in _DEFAULT_BASE_URLS:
        raise ConfigError(
            f"unknown LLM_PROVIDER {settings.llm_provider!r}\n"
            "fix: set LLM_PROVIDER to groq, openai, mistral, or anthropic"
        )
    return OpenAICompatibleClient(settings, model=model)
