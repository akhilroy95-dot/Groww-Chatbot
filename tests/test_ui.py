"""Phase 6 UI gate: the ask view shape, example fill, and inspector gating."""

from __future__ import annotations

from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASK_VIEW = PROJECT_ROOT / "app" / "ui" / "streamlit_app.py"
INSPECTOR = PROJECT_ROOT / "app" / "ui" / "pages" / "2_Pipeline_Inspector.py"


def test_ask_view_has_welcome_examples_and_disclaimer() -> None:
    app = AppTest.from_file(str(ASK_VIEW), default_timeout=120)
    app.run()

    assert not app.exception, [str(error.value) for error in app.exception]
    assert [title.value for title in app.title] == ["Groww MF Facts Assistant"]
    labels = [button.label for button in app.button]
    assert len(labels) == 4
    assert labels[-1] == "Ask"
    assert any("Facts-only. No investment advice." in caption.value for caption in app.caption)


def test_example_button_fills_the_input_without_submitting() -> None:
    app = AppTest.from_file(str(ASK_VIEW), default_timeout=120)
    app.run()

    app.button[0].click().run()

    assert not app.exception, [str(error.value) for error in app.exception]
    assert app.text_input[0].value.startswith("What is the expense ratio")
    assert app.session_state["last_request_id"] is None


def test_inspector_is_hidden_without_the_debug_flag(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    app = AppTest.from_file(str(INSPECTOR), default_timeout=120)
    app.run()

    assert not app.exception, [str(error.value) for error in app.exception]
    assert any("local-only" in warning.value for warning in app.warning)
    assert list(app.tabs) == []
    assert list(app.metric) == []


def test_inspector_opens_all_eight_tabs_with_debug_flag(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    app = AppTest.from_file(str(INSPECTOR), default_timeout=120)
    app.query_params["debug"] = "1"
    app.run()

    assert not app.exception, [str(error.value) for error in app.exception]
    assert [tab.label for tab in app.tabs] == [
        "1 Source",
        "2 Chunks",
        "3 Embeddings",
        "4 Vector DB",
        "5 Prompt",
        "6 LLM",
        "7 Validation",
        "8 Final",
    ]
