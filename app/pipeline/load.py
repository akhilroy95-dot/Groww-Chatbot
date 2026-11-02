"""Phase 1 orchestration: registry → fetch → extract → ``data/01_raw``.

Artifacts per source (``data/01_raw/``):

* ``{id}.html`` - byte-exact response body, written before parsing;
* ``{id}.json`` - allowlisted, typed sections (never full-page text);
* ``{id}.txt``  - human-readable sibling for the inspector;
* ``manifest.json`` - status, sha256, section list, missing facts, source date.

A cached HTML file plus a matching manifest entry means zero network calls on a
second run, so the extracted JSON is byte-identical (``fetched_at`` included).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import ConfigError, Settings
from ..sources import fetchable_sources, load_registry
from .extract import (
    ExtractionError,
    covered_facts,
    extract_document,
    render_plain_text,
)
from .fetch import (
    FetchError,
    RawDoc,
    RobotsCache,
    build_client,
    fetch_source,
    sha256_bytes,
    utc_now,
)


@dataclass
class LoadSummary:
    total_fetchable: int = 0
    indexed: int = 0
    fetched: int = 0
    cached: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    missing_expected: dict[str, list[str]] = field(default_factory=dict)
    core_missing: dict[str, list[str]] = field(default_factory=dict)
    manifest_path: Path | None = None

    @property
    def ok(self) -> bool:
        """Only fetch/extraction failures and missing *core* facts are fatal."""
        return not self.failed and not self.core_missing

    def describe(self) -> str:
        lines = [
            f"fetchable sources: {self.total_fetchable}",
            f"indexed: {self.indexed}  (network {self.fetched}, cache {self.cached})",
        ]
        if self.core_missing:
            lines.append("missing expected facts (core, fatal):")
            for source_id, facts in sorted(self.core_missing.items()):
                lines.append(f"  - {source_id}: {', '.join(facts)}")
        support_missing = {
            source_id: facts
            for source_id, facts in self.missing_expected.items()
            if source_id not in self.core_missing
        }
        if support_missing:
            lines.append("missing expected facts (support, warning):")
            for source_id, facts in sorted(support_missing.items()):
                lines.append(f"  - {source_id}: {', '.join(facts)}")
        if self.failed:
            lines.append("failed:")
            for source_id, error in self.failed:
                lines.append(f"  - {source_id}: {error}")
        if self.manifest_path:
            lines.append(f"manifest: {self.manifest_path}")
        return "\n".join(lines)


def run_load(
    settings: Settings,
    *,
    force: bool = False,
    only: str | None = None,
    client: httpx.Client | None = None,
    sleep=time.sleep,
) -> LoadSummary:
    registry = load_registry(settings.sources_path)
    settings.ensure_data_dirs()

    raw_dir = settings.data_dir_path / "01_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_dir / "manifest.json"
    previous = _read_manifest(manifest_path)

    entries = fetchable_sources(registry)
    if only:
        selected = [entry for entry in entries if entry.id == only]
        if not selected:
            known = ", ".join(entry.id for entry in entries)
            raise ConfigError(
                f"source {only!r} is not a fetchable source\nfix: choose one of: {known}"
            )
        entries = selected

    summary = LoadSummary(total_fetchable=len(entries), manifest_path=manifest_path)
    records = dict(previous)
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
        for entry in entries:
            raw_path = raw_dir / f"{entry.id}.html"
            cached = None if force else _cached_raw(entry, raw_path, previous.get(entry.id))

            if cached is not None:
                raw = cached
            else:
                if network_calls:
                    sleep(settings.corpus.fetch.delay_seconds)
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
                    summary.failed.append((entry.id, str(exc)))
                    continue
                network_calls += 1
                _write_bytes(raw_path, raw.content)

            try:
                document = extract_document(
                    entry,
                    raw.content,
                    fetched_at=raw.fetched_at,
                    headers=raw.headers,
                    final_url=raw.final_url,
                )
            except ExtractionError as exc:
                summary.failed.append((entry.id, str(exc)))
                records.pop(entry.id, None)
                continue

            covered = covered_facts(document)
            missing = [fact for fact in entry.expected_facts if fact not in covered]
            if missing:
                summary.missing_expected[entry.id] = missing
                if entry.role == "core":
                    summary.core_missing[entry.id] = missing

            _write_text(raw_dir / f"{entry.id}.txt", render_plain_text(document))
            _write_json(raw_dir / f"{entry.id}.json", document)

            records[entry.id] = {
                "source_id": entry.id,
                "url": str(entry.url),
                "final_url": raw.final_url,
                "role": entry.role,
                "http_status": raw.status,
                "fetched_at": raw.fetched_at,
                "bytes": raw.size,
                "sha256": raw.sha256,
                "content_type": raw.content_type,
                "last_modified": raw.headers.get("last-modified"),
                "extractor_version": document["extractor_version"],
                "sections_found": [section["name"] for section in document["sections"]],
                "missing_expected": missing,
                "source_date": document["source_date"],
            }
            summary.indexed += 1
            if raw.from_cache:
                summary.cached += 1
            else:
                summary.fetched += 1
    finally:
        if created_client:
            http_client.close()

    registry_ids = {entry.id for entry in fetchable_sources(registry)}
    ordered = {key: records[key] for key in sorted(records) if key in registry_ids}
    _write_json(manifest_path, {"version": 1, "sources": [ordered[key] for key in sorted(ordered)]})

    return summary


# ------------------------------------------------------------------------- cache


def _cached_raw(entry, path: Path, record: dict | None) -> RawDoc | None:
    if not record or not path.exists():
        return None
    if record.get("url") != str(entry.url) or record.get("http_status") != 200:
        return None
    content = path.read_bytes()
    digest = sha256_bytes(content)
    if digest != record.get("sha256"):
        return None
    headers = {}
    if record.get("last_modified"):
        headers["last-modified"] = record["last_modified"]
    return RawDoc(
        source_id=entry.id,
        url=str(entry.url),
        final_url=record.get("final_url") or str(entry.url),
        status=200,
        content=content,
        content_type=record.get("content_type") or "text/html",
        headers=headers,
        fetched_at=record.get("fetched_at") or utc_now(),
        sha256=digest,
        from_cache=True,
    )


# ----------------------------------------------------------------------- files


def _read_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    records = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return {}
    return {
        record["source_id"]: record
        for record in records
        if isinstance(record, dict) and record.get("source_id")
    }


def _write_json(path: Path, payload: dict) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)
