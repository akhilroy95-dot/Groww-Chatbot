"""Source registry: load, validate, and query ``config/sources.yaml``.

The registry is the single place a URL may come from. The renderer resolves a
``source_id`` here and nothing else, so an LLM can never invent a link.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from .config import PROJECT_ROOT, ConfigError
from .schemas import SourceEntry, SourceRegistry

DEFAULT_SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"

ROLE_QUOTAS: dict[str, int] = {"core": 5, "support": 9, "citation_only": 7}

# Every core scheme page must expose these six facts. ELSS additionally has lock_in.
CORE_REQUIRED_FACTS: tuple[str, ...] = (
    "expense_ratio",
    "exit_load",
    "minimum_sip",
    "minimum_lumpsum",
    "riskometer",
    "benchmark",
)

CITATION_ONLY_NOTE = "citation-only: never fetched, never bypassed"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,60}$")


class RegistryError(ConfigError):
    """The source registry is missing, malformed, or internally inconsistent."""


# ------------------------------------------------------------------------ loading


def load_registry(
    path: str | Path | None = None,
    *,
    strict: bool = True,
) -> SourceRegistry:
    """Load and validate the registry.

    ``strict=True`` also enforces the 5/9/7 role quotas used by the demo. Tests
    that build a two-source registry pass ``strict=False``.
    """
    target = Path(path) if path is not None else DEFAULT_SOURCES_PATH
    if not target.is_absolute():
        target = PROJECT_ROOT / target

    if not target.exists():
        raise RegistryError(
            f"source registry not found: {target}\n"
            "fix: create config/sources.yaml (see implementation.md phase 1)"
        )

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"could not parse {target}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError(f"{target} must contain a mapping at the top level")

    try:
        registry = SourceRegistry(**raw)
    except ValidationError as exc:
        details = "\n".join(
            f"  - {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise RegistryError(f"invalid source registry {target}:\n{details}") from exc

    issues = validate_registry(registry, strict=strict)
    if issues:
        joined = "\n".join(f"  - {issue}" for issue in issues)
        raise RegistryError(f"invalid source registry {target}:\n{joined}")

    return registry


def validate_registry(registry: SourceRegistry, *, strict: bool = True) -> list[str]:
    """Return human-readable problems; an empty list means the registry is usable."""
    issues: list[str] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    role_counts = {role: 0 for role in ROLE_QUOTAS}

    for entry in registry.sources:
        role_counts[entry.role] = role_counts.get(entry.role, 0) + 1

        if entry.id in seen_ids:
            issues.append(f"duplicate source id: {entry.id}")
        seen_ids.add(entry.id)

        url = str(entry.url)
        if url in seen_urls:
            issues.append(f"duplicate source url: {url}")
        seen_urls.add(url)

        if not _ID_RE.match(entry.id):
            issues.append(f"source id must be snake_case: {entry.id!r}")

        if not is_url_allowed(url, registry.allowlist_domains):
            issues.append(f"{entry.id}: host is not on the allowlist: {url}")

        if entry.role == "core":
            missing_meta = [
                name
                for name in ("scheme_slug", "scheme_name")
                if not getattr(entry, name)
            ]
            if missing_meta and entry.doc_type != "groww_amc":
                issues.append(f"{entry.id}: core entry needs {', '.join(missing_meta)}")
            missing_facts = [fact for fact in CORE_REQUIRED_FACTS if fact not in entry.expected_facts]
            if missing_facts and entry.doc_type != "groww_amc" and entry.expected_facts:
                issues.append(f"{entry.id}: missing expected facts: {', '.join(missing_facts)}")
            if entry.doc_type == "groww_amc" and "amc_overview" not in entry.expected_facts:
                issues.append(f"{entry.id}: AMC core entry must expect amc_overview")
            if entry.expected_facts and "lock_in" in entry.expected_facts:
                if entry.scheme_slug and "elss" not in entry.scheme_slug:
                    issues.append(f"{entry.id}: lock_in is only expected for the ELSS scheme")
        elif entry.role == "support":
            if not entry.expected_facts:
                issues.append(f"{entry.id}: support entry needs at least one expected fact")
        else:  # citation_only
            if entry.fetch_method != "manual":
                issues.append(f"{entry.id}: citation-only entries must use fetch_method: manual")

    if strict:
        for role, expected in ROLE_QUOTAS.items():
            actual = role_counts.get(role, 0)
            if actual != expected:
                issues.append(f"role {role!r} has {actual} entries, expected {expected}")
        total = sum(role_counts.values())
        if total != sum(ROLE_QUOTAS.values()):
            issues.append(f"registry has {total} entries, expected {sum(ROLE_QUOTAS.values())}")

    core_slugs = [e.scheme_slug for e in registry.sources if e.role == "core" and e.scheme_slug]
    if len(core_slugs) != len(set(core_slugs)):
        issues.append("two core sources share a scheme_slug")

    if any("hdfcfund.com" in str(e.url) and e.role != "citation_only" for e in registry.sources):
        issues.append("hdfcfund.com pages must be citation-only (bot 403, never bypassed)")

    return issues


# ------------------------------------------------------------------------ queries


def is_url_allowed(url: str, allowlist_domains: Iterable[str]) -> bool:
    """Allow the listed domains and their subdomains, never lookalikes."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    for domain in allowlist_domains:
        clean = domain.lower().strip().lstrip(".").rstrip(".")
        if host == clean or host.endswith("." + clean):
            return True
    return False


def fetchable_sources(registry: SourceRegistry) -> list[SourceEntry]:
    return [entry for entry in registry.sources if entry.role in ("core", "support")]


def citation_only_sources(registry: SourceRegistry) -> list[SourceEntry]:
    return [entry for entry in registry.sources if entry.role == "citation_only"]


def find_source(registry: SourceRegistry, source_id: str) -> SourceEntry | None:
    return next((entry for entry in registry.sources if entry.id == source_id), None)


def find_scheme(registry: SourceRegistry, scheme_slug: str) -> SourceEntry | None:
    return next(
        (
            entry
            for entry in registry.sources
            if entry.scheme_slug == scheme_slug and entry.role == "core"
        ),
        None,
    )


def registry_summary(registry: SourceRegistry) -> dict[str, int]:
    summary = {role: 0 for role in ROLE_QUOTAS}
    for entry in registry.sources:
        summary[entry.role] = summary.get(entry.role, 0) + 1
    summary["total"] = len(registry.sources)
    summary["fetchable"] = len(fetchable_sources(registry))
    return summary
