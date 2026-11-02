"""Phase 0 gate: config loading, precedence, validation, and error messages."""

from __future__ import annotations

import pytest

from app.config import (
    DATA_SUBDIRS,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    load_settings,
)


def test_default_config_loads_without_any_provider_key() -> None:
    settings = load_settings()

    assert settings.llm_provider == "groq"
    assert settings.llm_model
    assert settings.api_key_env == "GROQ_API_KEY"
    assert settings.embedding.model_name == "sentence-transformers/all-MiniLM-L6-v2"
    assert settings.embedding.dimension == 384
    assert settings.vector_db.collection == "groww_mf_facts__minilm_l6_v2__v1"
    assert settings.retrieval.min_similarity == pytest.approx(0.35)
    assert settings.guardrails.max_words == 70
    assert len(settings.ui.examples) == 3
    assert DEFAULT_CONFIG_PATH.exists()


def test_environment_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")

    settings = load_settings()

    assert settings.llm_provider == "openai"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.api_key_env == "OPENAI_API_KEY"


def test_dotenv_file_supplies_provider_and_model(write_env) -> None:
    env_file = write_env(
        """
        LLM_PROVIDER=gemini
        LLM_MODEL=gemini-2.0-flash
        GEMINI_API_KEY=test-key
        """
    )

    settings = load_settings(env_file=env_file, require_provider=True)

    assert settings.llm_provider == "gemini"
    assert settings.llm_model == "gemini-2.0-flash"
    assert settings.api_key_env == "GEMINI_API_KEY"


def test_real_environment_beats_dotenv(write_env, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env("LLM_MODEL=model-from-dotenv\n")
    monkeypatch.setenv("LLM_MODEL", "model-from-env")

    settings = load_settings(env_file=env_file)

    assert settings.llm_model == "model-from-env"


def test_unknown_provider_names_the_value_and_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "sbi")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "sbi" in message
    assert "fix:" in message
    assert "groq" in message


def test_empty_model_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "LLM_MODEL" in message
    assert "fix:" in message


def test_missing_provider_key_only_fails_model_calling_commands() -> None:
    # Pipeline and retrieval work with no key at all.
    assert load_settings(require_provider=False).llm_provider == "groq"

    with pytest.raises(ConfigError) as excinfo:
        load_settings(require_provider=True)

    message = str(excinfo.value)
    assert "GROQ_API_KEY" in message
    assert "fix:" in message


def test_present_provider_key_satisfies_require_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_placeholder")

    settings = load_settings(require_provider=True)

    assert settings.api_key_env == "GROQ_API_KEY"


def test_config_sha256_is_stable_and_model_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    first = load_settings()
    second = load_settings()
    assert first.config_sha256 == second.config_sha256
    assert len(first.config_sha256) == 64

    monkeypatch.setenv("LLM_MODEL", "some/other-model")
    third = load_settings()

    assert third.config_sha256 != first.config_sha256


def test_env_placeholder_default_is_used(write_config) -> None:
    path = write_config(
        """
        app:
          name: "${NOT_SET_FOR_TEST:-Fallback Name}"
        """
    )

    settings = load_settings(config_path=path)

    assert settings.app.name == "Fallback Name"


def test_env_placeholder_without_default_raises(write_config) -> None:
    path = write_config(
        """
        app:
          name: "${NOT_SET_FOR_TEST}"
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    message = str(excinfo.value)
    assert "NOT_SET_FOR_TEST" in message
    assert "fix:" in message


def test_unknown_yaml_key_is_rejected(write_config) -> None:
    path = write_config(
        """
        app:
          bogus_setting: true
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    message = str(excinfo.value)
    assert "app.bogus_setting" in message
    assert "invalid configuration" in message


def test_invalid_enum_value_is_reported_with_its_path(write_config) -> None:
    path = write_config(
        """
        corpus:
          fetch:
            render_js: "sometimes"
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    assert "corpus.fetch.render_js" in str(excinfo.value)


def test_missing_config_file_fails_with_a_fix(tmp_path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=tmp_path / "nope.yaml")

    message = str(excinfo.value)
    assert "not found" in message
    assert "fix:" in message


def test_non_mapping_config_is_rejected(write_config) -> None:
    path = write_config(
        """
        - app
        - corpus
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    assert "mapping" in str(excinfo.value)


def test_broken_yaml_is_rejected(write_config) -> None:
    path = write_config("app: [unclosed\n")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    assert "could not parse" in str(excinfo.value)


def test_ensure_data_dirs_creates_the_full_tree(write_config, tmp_path) -> None:
    data_dir = tmp_path / "artifacts"
    path = write_config(
        f"""
        app:
          data_dir: "{data_dir.as_posix()}"
        """
    )

    settings = load_settings(config_path=path)
    created = settings.ensure_data_dirs()
    created_again = settings.ensure_data_dirs()

    assert settings.data_dir_path == data_dir
    assert len(created) == len(DATA_SUBDIRS) == 9
    assert len(created_again) == 9
    for sub in DATA_SUBDIRS:
        assert (data_dir / sub).is_dir(), sub


def test_relative_data_dir_resolves_against_project_root() -> None:
    settings = load_settings()

    assert settings.data_dir_path.is_absolute()
    assert settings.data_dir_path.name == "data"
    assert settings.data_dir_path.exists()
