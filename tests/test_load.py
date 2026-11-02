"""Phase 1 gate: cache determinism, manifest, and fatal-vs-warning semantics."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import AppSettings, CorpusSettings, FetchSettings, Settings
from app.pipeline import load as load_module
from app.pipeline.load import run_load
from app.schemas import SourceEntry, SourceRegistry
from app.sources import CORE_REQUIRED_FACTS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phase1"
ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"


def source(
    source_id: str,
    url: str,
    *,
    role: str,
    doc_type: str,
    expected_facts: list[str],
    fetch_method: str = "httpx",
) -> SourceEntry:
    return SourceEntry(
        id=source_id,
        title=source_id.replace("_", " ").title(),
        url=url,
        owner="Test",
        doc_type=doc_type,
        role=role,
        source_priority=1 if role == "core" else 3,
        fetch_method=fetch_method,
        expected_facts=expected_facts,
        scheme_slug="hdfc-large-cap-direct-growth" if doc_type == "groww_scheme" else None,
        scheme_name="HDFC Large Cap Fund Direct Growth" if doc_type == "groww_scheme" else None,
    )


def registry(*sources: SourceEntry) -> SourceRegistry:
    return SourceRegistry(
        allowlist_domains=["groww.in", "hdfcfund.com"],
        sources=list(sources),
    )


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        app=AppSettings(data_dir=str(tmp_path / "data")),
        corpus=CorpusSettings(
            sources_file=str(tmp_path / "sources.yaml"),
            fetch=FetchSettings(delay_seconds=0),
        ),
    )


def default_sources() -> list[SourceEntry]:
    return [
        source(
            "core_scheme",
            "https://groww.in/core",
            role="core",
            doc_type="groww_scheme",
            expected_facts=list(CORE_REQUIRED_FACTS),
        ),
        source(
            "help_page",
            "https://groww.in/help",
            role="support",
            doc_type="groww_help",
            expected_facts=["statement_download"],
        ),
        source(
            "cited_page",
            "https://www.hdfcfund.com/cited",
            role="citation_only",
            doc_type="hdfc_page",
            expected_facts=["statement_download"],
            fetch_method="manual",
        ),
    ]


def make_client(requests: list[str], *, fail_core: bool = False) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        if request.url.path == "/core":
            if fail_core:
                return httpx.Response(
                    200, content=b"<html><body>no data</body></html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                200,
                content=(FIXTURES / "scheme_large_cap.html").read_bytes(),
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/help":
            return httpx.Response(
                200,
                content=(FIXTURES / "help_steps.html").read_bytes(),
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404, text="unexpected request")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_two_runs_produce_identical_artifacts_without_network(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(load_module, "load_registry", lambda *a, **k: registry(*default_sources()))
    settings = settings_for(tmp_path)

    first_requests: list[str] = []
    with make_client(first_requests) as client:
        first = run_load(settings, client=client, sleep=lambda _: None)

    assert first.ok, first.describe()
    assert first.indexed == 2
    assert first.fetched == 2
    assert first.cached == 0

    raw = settings.data_dir_path / "01_raw"
    first_bytes = {p.name: p.read_bytes() for p in raw.iterdir() if p.is_file()}

    second_requests: list[str] = []
    with make_client(second_requests) as client:
        second = run_load(settings, client=client, sleep=lambda _: None)

    assert second.ok, second.describe()
    assert second.indexed == 2
    assert second.fetched == 0
    assert second.cached == 2
    assert second_requests == []

    second_bytes = {p.name: p.read_bytes() for p in raw.iterdir() if p.is_file()}
    assert first_bytes == second_bytes

    assert not (raw / "cited_page.html").exists()
    assert not (raw / "cited_page.json").exists()

    manifest = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))
    recorded = {record["source_id"]: record for record in manifest["sources"]}
    assert set(recorded) == {"core_scheme", "help_page"}
    assert all(record["http_status"] == 200 for record in recorded.values())
    assert all(record["sha256"] for record in recorded.values())


def test_missing_support_fact_is_a_warning(tmp_path, monkeypatch) -> None:
    sources = default_sources()
    sources[1] = source(
        "help_page",
        "https://groww.in/help",
        role="support",
        doc_type="groww_help",
        expected_facts=["statement_download", "riskometer"],
    )
    monkeypatch.setattr(load_module, "load_registry", lambda *a, **k: registry(*sources))
    settings = settings_for(tmp_path)

    with make_client([]) as client:
        summary = run_load(settings, client=client, sleep=lambda _: None)

    assert summary.ok, summary.describe()
    assert summary.core_missing == {}
    assert summary.missing_expected == {"help_page": ["riskometer"]}
    assert "support, warning" in summary.describe()


def test_missing_core_fact_is_fatal(tmp_path, monkeypatch) -> None:
    sources = default_sources()
    sources[0] = source(
        "core_scheme",
        "https://groww.in/core",
        role="core",
        doc_type="groww_scheme",
        expected_facts=["lock_in"],
    )
    monkeypatch.setattr(load_module, "load_registry", lambda *a, **k: registry(*sources))
    settings = settings_for(tmp_path)

    with make_client([]) as client:
        summary = run_load(settings, client=client, sleep=lambda _: None)

    assert not summary.ok
    assert summary.core_missing == {"core_scheme": ["lock_in"]}
    assert "fatal" in summary.describe()


def test_core_extraction_failure_is_fatal_and_writes_no_json(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(load_module, "load_registry", lambda *a, **k: registry(*default_sources()))
    settings = settings_for(tmp_path)

    with make_client([], fail_core=True) as client:
        summary = run_load(settings, client=client, sleep=lambda _: None)

    assert not summary.ok
    assert summary.indexed == 1
    assert [source_id for source_id, _ in summary.failed] == ["core_scheme"]
    raw = settings.data_dir_path / "01_raw"
    assert not (raw / "core_scheme.json").exists()
    assert (raw / "help_page.json").exists()
