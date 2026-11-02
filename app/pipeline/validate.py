"""Registry + reachability validation (``data/00_sources/validation_report.md``).

Run before every scrape. It proves the registry is internally consistent, every
URL is on the allowlist, and all 14 fetchable pages are reachable. Citation-only
pages are reported as skipped, never fetched, because fetching them would mean
fighting bot protection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import Settings
from ..sources import (
    CITATION_ONLY_NOTE,
    ROLE_QUOTAS,
    load_registry,
    registry_summary,
)
from .fetch import FetchError, RobotsCache, build_client, fetch_source


@dataclass
class ValidationRow:
    source_id: str
    title: str
    url: str
    role: str
    status: str
    http_status: int | None = None
    bytes: int | None = None
    notes: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "not_checked", "not_fetched"}


@dataclass
class ValidationSummary:
    report_path: Path
    rows: list[ValidationRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    offline: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures

    def describe(self) -> str:
        counts = self.counts
        lines = [
            f"registry: {counts.get('total', 0)} sources "
            f"(core {counts.get('core', 0)}, support {counts.get('support', 0)}, "
            f"citation-only {counts.get('citation_only', 0)})",
            f"fetchable: {counts.get('fetchable', 0)} indexed candidates",
            f"report: {self.report_path}",
        ]
        if self.offline:
            lines.append("network checks: skipped (--offline)")
        else:
            reachable = sum(1 for row in self.rows if row.status == "ok")
            lines.append(f"reachable: {reachable}/{counts.get('fetchable', 0)}")
        if self.failures:
            lines.append("failures:")
            lines.extend(f"  - {failure}" for failure in self.failures)
        return "\n".join(lines)


def run_validation(
    settings: Settings,
    *,
    offline: bool = False,
    client: httpx.Client | None = None,
    sleep=time.sleep,
) -> ValidationSummary:
    registry = load_registry(settings.sources_path)
    settings.ensure_data_dirs()

    report_path = settings.data_dir_path / "00_sources" / "validation_report.md"
    counts = registry_summary(registry)
    summary = ValidationSummary(report_path=report_path, counts=counts, offline=offline)

    created_client = client is None
    http_client = client or build_client(settings)
    robots = RobotsCache(
        http_client,
        settings.corpus.fetch.user_agent,
        max_retries=settings.corpus.fetch.max_retries,
        sleep=sleep,
    )
    network_calls = 0

    try:
        for entry in registry.sources:
            if entry.role == "citation_only":
                summary.rows.append(
                    ValidationRow(
                        source_id=entry.id,
                        title=entry.title,
                        url=str(entry.url),
                        role=entry.role,
                        status="not_fetched",
                        notes=entry.notes or CITATION_ONLY_NOTE,
                    )
                )
                continue

            if offline:
                summary.rows.append(
                    ValidationRow(
                        source_id=entry.id,
                        title=entry.title,
                        url=str(entry.url),
                        role=entry.role,
                        status="not_checked",
                        notes="offline mode: registry validated, network skipped",
                    )
                )
                continue

            if network_calls:
                sleep(settings.corpus.fetch.delay_seconds)
            network_calls += 1

            try:
                raw = fetch_source(
                    entry,
                    settings,
                    client=http_client,
                    allowlist_domains=registry.allowlist_domains,
                    robots=robots,
                    sleep=sleep,
                )
            except FetchError as exc:
                status = "expected_bot_block" if exc.status == 403 else "fail"
                summary.rows.append(
                    ValidationRow(
                        source_id=entry.id,
                        title=entry.title,
                        url=str(entry.url),
                        role=entry.role,
                        status=status,
                        http_status=exc.status,
                        notes=str(exc),
                    )
                )
                if status == "fail":
                    summary.failures.append(f"{entry.id}: {exc}")
                continue

            summary.rows.append(
                ValidationRow(
                    source_id=entry.id,
                    title=entry.title,
                    url=str(entry.url),
                    role=entry.role,
                    status="ok",
                    http_status=raw.status,
                    bytes=raw.size,
                    notes=f"sha256:{raw.sha256[:12]}",
                )
            )
    finally:
        if created_client:
            http_client.close()

    if not offline:
        visible_rows = [row for row in summary.rows if row.role in ("core", "support")]
        for role in ("core", "support"):
            expected = ROLE_QUOTAS[role]
            actual = sum(
                1 for row in visible_rows if row.role == role and row.status == "ok"
            )
            if actual != expected:
                summary.failures.append(f"{role}: {actual}/{expected} pages reachable")

    _write_report(summary)
    return summary


def _write_report(summary: ValidationSummary) -> None:
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    counts = summary.counts
    lines = [
        "# Source validation report",
        "",
        f"- Generated: {generated}",
        f"- Mode: {'offline (registry only)' if summary.offline else 'online'}",
        f"- Registry: {counts.get('total', 0)} sources",
        f"  - core: {counts.get('core', 0)} (expected {ROLE_QUOTAS['core']})",
        f"  - support: {counts.get('support', 0)} (expected {ROLE_QUOTAS['support']})",
        f"  - citation-only: {counts.get('citation_only', 0)} "
        f"(expected {ROLE_QUOTAS['citation_only']})",
        f"- Fetchable (core + support): {counts.get('fetchable', 0)}",
        "",
        "## Results",
        "",
        "| ID | Role | Status | HTTP | Bytes | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for row in summary.rows:
        notes = row.notes.replace("|", "\\|")
        lines.append(
            f"| {row.source_id} | {row.role} | {row.status} | "
            f"{row.http_status if row.http_status is not None else '-'} | "
            f"{row.bytes if row.bytes is not None else '-'} | {notes} |"
        )

    lines += [
        "",
        "## Policy",
        "",
        "- Citation-only pages are never fetched: they are link destinations only.",
        "- A 403 is a hard stop. Bot protection, CAPTCHAs, and authentication are never bypassed.",
        "- Every fetchable URL must stay on the registry allowlist, including after redirects.",
        "- `missing_expected` is checked per page by `python -m app.cli load`.",
        "",
    ]
    if summary.failures:
        lines += ["## Failures", ""] + [f"- {failure}" for failure in summary.failures] + [""]

    target = summary.report_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
