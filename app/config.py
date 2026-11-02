"""Configuration loader.

Precedence: code defaults < ``config/config.yaml`` < ``.env`` < process environment.

Secrets never live in the YAML file. They are read from the environment only, and
this module never logs or returns them. Provider credentials are validated only by
commands that actually call a model (``require_provider=True``); the data pipeline
and retrieval must run on a machine with no LLM key at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_API_KEY_ENV: dict[str, str] = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
KNOWN_PROVIDERS: tuple[str, ...] = tuple(DEFAULT_API_KEY_ENV)

DATA_SUBDIRS: tuple[str, ...] = (
    "00_sources",
    "01_raw",
    "02_chunks",
    "03_embeddings",
    "04_vector_db",
    "05_retrieval",
    "06_answers",
    "07_eval",
    "logs",
)

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Configuration is missing, malformed, or unsafe to use."""


class StrictModel(BaseModel):
    """Reject unknown keys so a typo in the YAML fails loudly instead of silently."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- models


class AppSettings(StrictModel):
    name: str = "Groww MF Facts Assistant"
    env: str = "local"
    data_dir: str = "./data"
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"


class FetchSettings(StrictModel):
    user_agent: str = "GrowwMFFactsDemo/0.1 (public-pages demo)"
    timeout_seconds: int = 30
    delay_seconds: float = 2.0
    max_retries: int = 2
    respect_robots_txt: bool = True
    render_js: Literal["auto", "always", "never"] = "auto"
    cache_raw_html: bool = True


class RefreshSettings(StrictModel):
    mode: Literal["manual", "daily"] = "manual"
    stale_after_days: int = 7


class CorpusSettings(StrictModel):
    sources_file: str = "./config/sources.yaml"
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    refresh: RefreshSettings = Field(default_factory=RefreshSettings)


class ChunkingSettings(StrictModel):
    strategy: str = "structure_aware_fact_cards_v1"
    max_tokens: int = 250
    min_tokens: int = 25
    narrative_max_tokens: int = 300
    overlap_tokens: int = 40
    add_context_header: bool = True
    drop_sections: list[str] = Field(
        default_factory=lambda: [
            "returns",
            "holdings",
            "fund_management",
            "compare",
            "calculators",
            "ratings",
            "why_invest",
        ]
    )


class EmbeddingSettings(StrictModel):
    provider: str = "sentence_transformers"
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    revision: str = "main"
    device: str = "cpu"
    batch_size: int = 32
    max_seq_length: int = 256
    normalize: bool = True
    dimension: int = 384
    show_progress: bool = True


class HNSWSettings(StrictModel):
    m: int = 16
    construction_ef: int = 100
    search_ef: int = 50


class VectorDBSettings(StrictModel):
    provider: str = "chroma"
    persist_directory: str = "./data/04_vector_db/chroma"
    collection: str = "groww_mf_facts__minilm_l6_v2__v1"
    distance: str = "cosine"
    hnsw: HNSWSettings = Field(default_factory=HNSWSettings)


class MMRSettings(StrictModel):
    enabled: bool = True
    lambda_: float = Field(default=0.7, alias="lambda")


class RetrievalSettings(StrictModel):
    top_k: int = 8
    candidate_k: int = 20
    min_similarity: float = 0.35
    mmr: MMRSettings = Field(default_factory=MMRSettings)
    scheme_filter: Literal["auto", "always", "never"] = "auto"
    fact_type_boost: float = 0.05
    source_priority_bonus: float = 0.02
    context_max_tokens: int = 1200


class ProviderConfig(StrictModel):
    base_url: str | None = None
    api_key_env: str


class GenerationSettings(StrictModel):
    provider: str = "groq"
    model: str = "qwen/qwen3.8-27b"
    temperature: float = 0.0
    max_tokens: int = 220
    timeout_seconds: int = 30
    max_retries: int = 1
    fallback_model: str | None = "openai/gpt-oss-120b"
    fallback_to_extractive: bool = True
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        return value.strip()


class PIIGuardSettings(StrictModel):
    enabled: bool = True
    action: Literal["refuse_and_redact", "refuse", "redact"] = "refuse_and_redact"


class AdviceGuardSettings(StrictModel):
    enabled: bool = True


class PerformanceGuardSettings(StrictModel):
    enabled: bool = True
    link_to_factsheet: bool = True


class PromptInjectionGuardSettings(StrictModel):
    enabled: bool = True


class GuardrailSettings(StrictModel):
    max_input_chars: int = 500
    max_sentences: int = 3
    max_words: int = 70
    require_citation: bool = True
    citation_allowlist_only: bool = True
    pii: PIIGuardSettings = Field(default_factory=PIIGuardSettings)
    advice: AdviceGuardSettings = Field(default_factory=AdviceGuardSettings)
    performance: PerformanceGuardSettings = Field(default_factory=PerformanceGuardSettings)
    prompt_injection: PromptInjectionGuardSettings = Field(
        default_factory=PromptInjectionGuardSettings
    )
    banned_output_terms: list[str] = Field(default_factory=list)


class UISettings(StrictModel):
    title: str = "Groww MF Facts Assistant"
    welcome: str = "Ask a fact about HDFC mutual fund schemes."
    examples: list[str] = Field(default_factory=list)
    disclaimer_short: str = "Facts-only. No investment advice."
    show_pipeline_inspector: bool = True


class EvaluationSettings(StrictModel):
    golden_set: str = "./data/07_eval/golden_set.jsonl"
    min_hit_at_3: float = 0.90
    min_fact_accuracy: float = 0.90
    min_refusal_accuracy: float = 0.95


class Settings(StrictModel):
    app: AppSettings = Field(default_factory=AppSettings)
    corpus: CorpusSettings = Field(default_factory=CorpusSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    vector_db: VectorDBSettings = Field(default_factory=VectorDBSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    guardrails: GuardrailSettings = Field(default_factory=GuardrailSettings)
    ui: UISettings = Field(default_factory=UISettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)

    # -- convenience -------------------------------------------------------

    @property
    def llm_provider(self) -> str:
        return self.generation.provider

    @property
    def llm_model(self) -> str:
        return self.generation.model

    @property
    def api_key_env(self) -> str:
        configured = self.generation.providers.get(self.generation.provider)
        if configured is not None:
            return configured.api_key_env
        return DEFAULT_API_KEY_ENV.get(self.generation.provider, "LLM_API_KEY")

    @property
    def data_dir_path(self) -> Path:
        path = Path(self.app.data_dir)
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    @property
    def sources_path(self) -> Path:
        path = Path(self.corpus.sources_file)
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    @property
    def config_sha256(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ensure_data_dirs(self) -> list[Path]:
        """Create the artifact tree. Safe to call repeatedly."""
        base = self.data_dir_path
        created: list[Path] = []
        for sub in DATA_SUBDIRS:
            target = base / sub
            target.mkdir(parents=True, exist_ok=True)
            created.append(target)
        return created


# ------------------------------------------------------------------ env expansion


def _expand_env(value: Any) -> Any:
    """Expand ``${NAME}`` / ``${NAME:-default}`` recursively."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(
                f"environment variable {name} is not set (referenced in config)\n"
                f"fix: add {name}=... to .env"
            )

        return _ENV_PLACEHOLDER.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _apply_env_overrides(data: dict[str, Any]) -> None:
    generation = data.setdefault("generation", {})
    if isinstance(generation, dict):
        if os.environ.get("LLM_PROVIDER"):
            generation["provider"] = os.environ["LLM_PROVIDER"]
        if os.environ.get("LLM_MODEL"):
            generation["model"] = os.environ["LLM_MODEL"]
    app = data.setdefault("app", {})
    if isinstance(app, dict) and os.environ.get("LOG_LEVEL"):
        app["log_level"] = os.environ["LOG_LEVEL"]


def _format_validation(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def _validate_provider(settings: Settings, *, require_provider: bool) -> None:
    provider = settings.generation.provider
    if provider not in KNOWN_PROVIDERS:
        raise ConfigError(
            f"unknown LLM_PROVIDER {provider!r}\n"
            f"fix: set LLM_PROVIDER to one of: {', '.join(KNOWN_PROVIDERS)}"
        )
    if not settings.generation.model:
        raise ConfigError(
            "LLM_MODEL is empty\n"
            "fix: set LLM_MODEL in .env (for example LLM_MODEL=qwen/qwen3.8-27b)"
        )
    if not require_provider:
        return
    env_name = settings.api_key_env
    if not os.environ.get(env_name):
        raise ConfigError(
            f"missing API key for provider {provider!r}: {env_name} is not set\n"
            f"fix: add {env_name}=... to .env (copy .env.example if you have not yet)"
        )


# ------------------------------------------------------------------------ loader


def load_settings(
    config_path: str | Path | None = None,
    *,
    env_file: str | Path | None = None,
    require_provider: bool = False,
) -> Settings:
    """Load, expand, validate, and return the effective settings.

    ``require_provider=True`` additionally asserts that the active provider's API
    key is present in the environment. Use it from model-calling commands only.
    """
    cfg_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / cfg_path

    env_path = Path(env_file) if env_file is not None else DEFAULT_ENV_PATH
    if not env_path.is_absolute():
        env_path = PROJECT_ROOT / env_path

    if env_path.exists():
        # override=False: a real process environment always wins over .env
        load_dotenv(env_path, override=False)

    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found: {cfg_path}\n"
            "fix: create config/config.yaml (see implementation.md phase 0)"
        )

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {cfg_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path} must contain a mapping at the top level")

    data = _expand_env(raw)
    _apply_env_overrides(data)

    try:
        settings = Settings(**data)
    except ValidationError as exc:
        raise ConfigError(
            f"invalid configuration in {cfg_path}:\n{_format_validation(exc)}"
        ) from exc

    _validate_provider(settings, require_provider=require_provider)
    return settings
