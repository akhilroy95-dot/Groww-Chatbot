"""Test fixtures.

Every test starts with a clean LLM environment so a developer's real ``.env``
can never make a test pass (or fail) by accident.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from app import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LLM_ENV_KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MISTRAL_API_KEY",
    "LOG_LEVEL",
    "LOG_HASH_SALT",
    "NOT_SET_FOR_TEST",
)


@pytest.fixture(autouse=True)
def isolate_llm_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove provider/model/key variables, then restore them afterwards."""
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Also ignore the developer's real .env: without this, a local
    # LLM_PROVIDER=anthropic would silently change what these tests assert.
    # Tests that need a dotenv file write and pass their own.
    monkeypatch.setattr(config, "DEFAULT_ENV_PATH", tmp_path / ".env")


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a YAML file under ``tmp_path`` and return its path."""

    def _write(body: str, name: str = "config.yaml") -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def write_env(tmp_path: Path):
    """Write a ``.env`` file under ``tmp_path`` and return its path."""

    def _write(body: str, name: str = ".env") -> Path:
        path = tmp_path / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    return _write
