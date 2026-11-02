"""Phase 1 gate: the 21-source registry and its allowlist rules."""

from __future__ import annotations

import pytest

from app.schemas import SourceEntry, SourceRegistry
from app.sources import (
    CORE_REQUIRED_FACTS,
    RegistryError,
    citation_only_sources,
    fetchable_sources,
    is_url_allowed,
    load_registry,
    registry_summary,
    validate_registry,
)


@pytest.fixture(scope="module")
def registry() -> SourceRegistry:
    return load_registry()


def test_registry_has_the_agreed_role_quotas(registry: SourceRegistry) -> None:
    assert registry_summary(registry) == {
        "core": 5,
        "support": 9,
        "citation_only": 7,
        "total": 21,
        "fetchable": 14,
    }


def test_every_source_is_on_the_allowlist(registry: SourceRegistry) -> None:
    for entry in registry.sources:
        assert is_url_allowed(str(entry.url), registry.allowlist_domains), entry.id


def test_core_schemes_expect_the_six_facts(registry: SourceRegistry) -> None:
    schemes = [e for e in registry.sources if e.doc_type == "groww_scheme"]
    assert len(schemes) == 4
    for entry in schemes:
        assert set(CORE_REQUIRED_FACTS) <= set(entry.expected_facts), entry.id
    elss = next(e for e in schemes if e.id == "groww_elss")
    assert "lock_in" in elss.expected_facts


def test_only_fourteen_sources_are_fetchable(registry: SourceRegistry) -> None:
    assert len(fetchable_sources(registry)) == 14
    assert len(citation_only_sources(registry)) == 7
    assert {e.role for e in fetchable_sources(registry)} == {"core", "support"}


def test_citation_only_sources_are_manual(registry: SourceRegistry) -> None:
    for entry in citation_only_sources(registry):
        assert entry.fetch_method == "manual", entry.id


def test_hdfc_pages_are_never_fetchable(registry: SourceRegistry) -> None:
    hdfc = [e for e in registry.sources if "hdfcfund.com" in str(e.url)]
    assert hdfc, "the registry must cite HDFC pages"
    assert all(entry.role == "citation_only" for entry in hdfc)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth", True),
        ("https://help.groww.in/faq", True),
        ("https://www.amfiindia.com/investor", True),
        ("https://investor.sebi.gov.in/riskometer.html", True),
        ("https://groww.in.evil.example/phish", False),
        ("https://notgroww.in/page", False),
        ("https://evil-hdfcfund.com/page", False),
        ("https://groww.in@evil.example/page", False),
        ("https://example.com/page", False),
        ("not a url", False),
    ],
)
def test_allowlist_never_matches_lookalike_hosts(
    url: str, allowed: bool, registry: SourceRegistry
) -> None:
    assert is_url_allowed(url, registry.allowlist_domains) is allowed


def _entry(
    source_id: str,
    url: str,
    *,
    role: str = "citation_only",
    doc_type: str = "hdfc_page",
    fetch_method: str = "manual",
    expected_facts: list[str] | None = None,
    scheme_slug: str | None = None,
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
        expected_facts=expected_facts or [],
        scheme_slug=scheme_slug,
        scheme_name=scheme_slug,
    )


def _registry(*sources: SourceEntry) -> SourceRegistry:
    return SourceRegistry(
        allowlist_domains=["groww.in", "hdfcfund.com"],
        sources=list(sources),
    )


def test_duplicate_ids_and_offallowlist_hosts_are_reported() -> None:
    first = _entry("dupe", "https://groww.in/a")
    second = _entry("dupe", "https://evil.example/b")
    issues = validate_registry(_registry(first, second), strict=False)

    assert any("duplicate source id" in issue for issue in issues)
    assert any("not on the allowlist" in issue for issue in issues)


def test_core_missing_expected_facts_are_reported() -> None:
    core = _entry(
        "groww_large_cap",
        "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
        role="core",
        doc_type="groww_scheme",
        fetch_method="httpx",
        expected_facts=["expense_ratio"],
        scheme_slug="hdfc-large-cap-direct-growth",
    )
    issues = validate_registry(_registry(core), strict=False)

    assert any("missing expected facts" in issue for issue in issues)


def test_a_missing_registry_file_explains_how_to_fix(tmp_path) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load_registry(tmp_path / "missing.yaml")

    assert "fix:" in str(excinfo.value)
