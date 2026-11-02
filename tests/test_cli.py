"""Phase 0 gate: the command surface exists and runs without an API key."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_CLEANED_ENV_KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MISTRAL_API_KEY",
)


def run_cli(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in _CLEANED_ENV_KEYS}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_help_lists_every_command() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    for command in (
        "sources",
        "load",
        "chunk",
        "embed",
        "index",
        "retrieve",
        "answer",
        "eval",
        "doctor",
    ):
        assert command in result.stdout, command


def test_sources_help_lists_validate() -> None:
    result = run_cli("sources", "--help")

    assert result.returncode == 0
    assert "validate" in result.stdout


def test_doctor_reports_phase_zero_state() -> None:
    result = run_cli("doctor")

    assert result.returncode == 0, result.stderr
    assert "doctor" in result.stdout
    assert "provider" in result.stdout
    assert "GROQ_API_KEY" in result.stdout
    assert "subdirs" in result.stdout
    assert "phase 0: ok" in result.stdout


def test_doctor_fix_creates_directories(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f'app:\n  data_dir: "{(tmp_path / "artifacts").as_posix()}"\n', encoding="utf-8"
    )

    result = run_cli("--config", str(config), "doctor", "--fix")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "artifacts" / "01_raw").is_dir()
    assert "phase 0: ok" in result.stdout


def test_index_help_exposes_rebuild_and_skip_smoke() -> None:
    result = run_cli("index", "--help")

    assert result.returncode == 0, result.stderr
    assert "--rebuild" in result.stdout
    assert "--skip-smoke" in result.stdout


def test_index_without_chunks_exits_one(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f'app:\n  data_dir: "{(tmp_path / "artifacts").as_posix()}"\n', encoding="utf-8"
    )

    result = run_cli("--config", str(config), "index")

    assert result.returncode == 1
    assert "run python -m app.cli chunk" in result.stderr


def test_embed_help_exposes_rebuild() -> None:
    result = run_cli("embed", "--help")

    assert result.returncode == 0, result.stderr
    assert "--rebuild" in result.stdout


def test_embed_without_chunks_exits_one(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f'app:\n  data_dir: "{(tmp_path / "artifacts").as_posix()}"\n', encoding="utf-8"
    )

    result = run_cli("--config", str(config), "embed")

    assert result.returncode == 1
    assert "run python -m app.cli chunk" in result.stderr


def test_chunk_help_exposes_force() -> None:
    result = run_cli("chunk", "--help")

    assert result.returncode == 0, result.stderr
    assert "--force" in result.stdout


def test_chunk_without_loaded_documents_exits_one(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f'app:\n  data_dir: "{(tmp_path / "artifacts").as_posix()}"\n', encoding="utf-8"
    )

    result = run_cli("--config", str(config), "chunk")

    assert result.returncode == 1
    assert "run python -m app.cli load" in result.stderr


def test_sources_validate_offline_checks_the_registry(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f'app:\n  data_dir: "{(tmp_path / "artifacts").as_posix()}"\n', encoding="utf-8"
    )

    result = run_cli("--config", str(config), "sources", "validate", "--offline")

    assert result.returncode == 0, result.stderr
    assert "21 sources" in result.stdout
    assert "core 5" in result.stdout
    assert "support 9" in result.stdout
    assert "citation-only 7" in result.stdout
    assert "network checks: skipped" in result.stdout
    assert (tmp_path / "artifacts" / "00_sources" / "validation_report.md").exists()


def test_load_rejects_an_unknown_source_without_network() -> None:
    result = run_cli("load", "--only", "not_a_source")

    assert result.returncode == 1
    assert "fix:" in result.stderr
    assert "not_a_source" in result.stderr


def test_retrieve_and_answer_accept_a_query() -> None:
    retrieved = run_cli("retrieve", "--query", "expense ratio hdfc large cap")
    assert retrieved.returncode == 0, retrieved.stderr
    assert "status: ok" in retrieved.stdout
    assert "intent: expense_ratio" in retrieved.stdout
    assert "hdfc-large-cap-direct-growth" in retrieved.stdout
    assert "trace:" in retrieved.stdout

    answered = run_cli("answer", "--query", "expense ratio hdfc large cap")
    assert answered.returncode == 0, answered.stderr
    assert "1.02%" in answered.stdout
    assert "Source: [" in answered.stdout
    assert "Last updated from sources:" in answered.stdout
    assert "mode: extractive" in answered.stdout


def test_retrieve_requires_query() -> None:
    result = run_cli("retrieve")

    assert result.returncode == 2
    assert "required" in result.stderr.lower()


def test_eval_stub_runs() -> None:
    result = run_cli("eval", "--limit", "1")

    assert result.returncode == 0, result.stderr
    assert "not implemented yet" in result.stdout


def test_invalid_config_exits_one_with_a_fix(tmp_path) -> None:
    result = run_cli("--config", str(tmp_path / "missing.yaml"), "doctor")

    assert result.returncode == 1
    assert "fix:" in result.stderr
    assert "not found" in result.stderr


def test_unknown_command_exits_two() -> None:
    result = run_cli("does-not-exist")

    assert result.returncode == 2
