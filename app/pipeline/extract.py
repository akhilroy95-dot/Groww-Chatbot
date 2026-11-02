"""Typed extraction for public mutual-fund pages.

Two families of extractor live here:

* ``groww_scheme_v1`` / ``groww_amc_v1`` read Groww's server-rendered
  ``__NEXT_DATA__`` JSON and copy an explicit allowlist of fields. Returns,
  ratings, holdings, fund-manager data, and historical loads are never read.
* ``article_v1`` reads the visible article container for Groww Help, AMFI, and
  SEBI pages, drops navigation boilerplate, and removes sentences that talk about
  returns, ratings, holdings, performance, or advice.

Extraction output is a plain dict (see ``docs`` shape below) that is written to
``data/01_raw/{id}.json`` and consumed by the chunker:

```text
{source_id, url, final_url, doc_type, extractor_version, title, fetched_at,
 source_date, scheme, facts, sections, warnings}
```
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Iterable

from bs4 import BeautifulSoup
from bs4.element import Tag

from ..schemas import SourceEntry

EXTRACTOR_VERSION_SCHEME = "groww_scheme_v1"
EXTRACTOR_VERSION_AMC = "groww_amc_v1"
EXTRACTOR_VERSION_ARTICLE = "article_v1"

_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y")

# Sentences containing these terms never reach the chunker.
_NARRATIVE_BANNED = re.compile(
    r"\b(?:returns?|cagr|annuali[sz]ed|outperform\w*|guarantee\w*|alpha|beta|"
    r"ratings?|holdings?|portfolio|fund manager|performance|ideal for|"
    r"suitable for|you should|recommend\w*)\b",
    re.IGNORECASE,
)

_POLICY_PATTERN = re.compile(r"\b(?:returns|cagr|holdings|ratings?)\b", re.IGNORECASE)

_BOILERPLATE = {
    "/",
    "home",
    "en",
    "hi",
    "हि",
    "help and support",
    "content",
    "was the answer helpful?",
    "loading...",
    "reports",
    "contact us",
    "hide menu",
    "go",
    "financial year",
    "month",
    "fund type",
    "mutual fund",
    "amfi",
    "investor corner",
    "knowledge center",
    "online center",
    "personal finance and investment",
    "securities market investment",
    "related tools",
    "portfolio disclosure",
    "check-out unclaimed dividend amount",
}

_BREADCRUMB_PREFIXES = ("home /", "home »")

_CONTENT_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li")



class ExtractionError(RuntimeError):
    """A page did not contain the structure its extractor requires."""


# ------------------------------------------------------------------------ helpers


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = html_lib.unescape(str(value))
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\[email protected\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _number(value: object) -> str | None:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        text = clean_text(value)
        return text or None
    return f"{number:g}"


def _parse_date(value: object) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _source_date_from_headers(headers: dict[str, str]) -> str | None:
    raw = headers.get("last-modified") or headers.get("Last-Modified")
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        return None


def _source_date_from_jsonld(soup: BeautifulSoup) -> str | None:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ("dateModified", "datePublished"):
                parsed = _parse_date(item.get(key))
                if parsed:
                    return parsed
    return None


def _sanitize_narrative(text: str) -> str:
    """Drop individual sentences that mention performance, ratings, or advice."""
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    kept = [sentence for sentence in sentences if sentence and not _NARRATIVE_BANNED.search(sentence)]
    return clean_text(" ".join(kept))


def _format_inr(value: object) -> str | None:
    text = _number(value)
    if text is None:
        return None
    try:
        amount = float(text)
    except ValueError:
        return text
    if amount.is_integer():
        return f"₹{int(amount):,}"
    return f"₹{amount:,.2f}"


def _format_aum(value: object) -> str | None:
    text = _number(value)
    if text is None:
        return None
    try:
        amount = float(text)
    except ValueError:
        return text
    return f"₹{amount:,.2f} Cr"


def _format_lockin(lock: object) -> str | None:
    if not isinstance(lock, dict):
        return None
    parts: list[str] = []
    for key, label in (("years", "year"), ("months", "month"), ("days", "day")):
        value = lock.get(key)
        if value:
            parts.append(f"{int(value)} {label}{'s' if int(value) != 1 else ''}")
    if not parts:
        return None
    return " ".join(parts)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] or "section"


def _next_data(soup: BeautifulSoup) -> dict:
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None or not tag.string:
        raise ExtractionError("page has no __NEXT_DATA__ payload")
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc
    page = data.get("props", {}).get("pageProps", {})
    if not isinstance(page, dict):
        raise ExtractionError("__NEXT_DATA__.props.pageProps is not an object")
    return page


def _fact(
    label: str,
    value: str,
    *,
    section: str,
    text: str | None = None,
    unit: str | None = None,
) -> dict:
    return {
        "label": label,
        "value": value,
        "unit": unit,
        "section": section,
        "text": text or f"{label}: {value}",
    }


def _section(name: str, *, heading: str | None = None) -> dict:
    return {"name": name, "heading": heading, "paragraphs": [], "items": []}


def _add_section(sections: list[dict], section: dict) -> None:
    if section["paragraphs"] or section["items"] or section["heading"]:
        sections.append(section)


def _has_text_descendant(tag: Tag) -> bool:
    for child in tag.find_all(list(_CONTENT_TAGS) + ["div"]):
        if clean_text(child.get_text(" ", strip=True)):
            return True
    return False


def _blocks(container: Tag) -> list[tuple[str, str]]:
    """Linearise a container into (kind, text) blocks, de-duplicated.

    Groww Help wraps some intro copy in bare ``div``s instead of ``<p>``, so a
    ``div`` is accepted when it is a text leaf (no nested p/li/heading of its
    own). That keeps the intro sentence without duplicating nested content.
    """
    blocks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag in container.find_all(list(_CONTENT_TAGS) + ["div"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if not text or text.lower() in _BOILERPLATE:
            continue
        if text.lower().startswith(_BREADCRUMB_PREFIXES):
            continue
        if tag.name == "li" and tag.find("li") is not None:
            continue
        if tag.name == "div" and _has_text_descendant(tag):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        if tag.name.startswith("h"):
            kind = "heading"
        elif tag.name == "li":
            kind = "item"
        else:
            kind = "paragraph"
        blocks.append((kind, text))
    return blocks


def _generic_sections(
    blocks: list[tuple[str, str]],
    *,
    primary_name: str,
) -> tuple[list[dict], list[str]]:
    sections: list[dict] = []
    warnings: list[str] = []
    current: dict | None = None

    for kind, text in blocks:
        if kind == "heading":
            heading = _sanitize_narrative(text)
            if not heading:
                warnings.append("dropped a heading with performance/advice wording")
                current = None
                continue
            current = _section(primary_name, heading=heading)
            sections.append(current)
            continue
        if current is None:
            current = _section(primary_name)
            sections.append(current)
        if kind == "item":
            cleaned = _sanitize_narrative(text)
            if cleaned:
                current["items"].append(cleaned)
            elif text:
                warnings.append("dropped an item with performance/advice wording")
        else:
            cleaned = _sanitize_narrative(text)
            if cleaned:
                current["paragraphs"].append(cleaned)
            elif text:
                warnings.append("dropped a paragraph with performance/advice wording")
    return [section for section in sections if section["paragraphs"] or section["items"]], warnings


# ------------------------------------------------------------------ scheme pages


def extract_groww_scheme(soup: BeautifulSoup, entry: SourceEntry) -> tuple[dict, str | None]:
    page = _next_data(soup)
    mf = page.get("mfServerSideData")
    if not isinstance(mf, dict):
        raise ExtractionError("groww scheme page has no mfServerSideData")

    facts: dict[str, dict] = {}
    sections: list[dict] = []
    warnings: list[str] = []

    expense_ratio = _number(mf.get("expense_ratio"))
    if expense_ratio:
        facts["expense_ratio"] = _fact(
            "Expense ratio", expense_ratio, unit="%", section="key_stats",
            text=f"Expense ratio: {expense_ratio}%",
        )

    raw_exit_load = clean_text(mf.get("exit_load"))
    if raw_exit_load:
        normalized = re.sub(r"^exit load of\s+", "", raw_exit_load, flags=re.IGNORECASE)
        normalized = normalized.rstrip(" .")
        facts["exit_load"] = _fact(
            "Exit load", normalized, section="exit_load", text=f"Exit load: {normalized}",
        )

    minimum_sip = _number(mf.get("min_sip_investment"))
    if minimum_sip:
        facts["minimum_sip"] = _fact(
            "Minimum SIP", minimum_sip, unit="INR", section="minimum_investments",
            text=f"Minimum SIP: {_format_inr(minimum_sip)}",
        )

    minimum_lumpsum = _number(mf.get("min_investment_amount"))
    if minimum_lumpsum:
        facts["minimum_lumpsum"] = _fact(
            "Minimum lumpsum investment", minimum_lumpsum, unit="INR",
            section="minimum_investments",
            text=f"Minimum lumpsum investment: {_format_inr(minimum_lumpsum)}",
        )

    return_stats = mf.get("return_stats")
    risk = None
    if isinstance(return_stats, list) and return_stats and isinstance(return_stats[0], dict):
        risk = clean_text(return_stats[0].get("risk")) or None
    if risk:
        display = risk if "risk" in risk.lower() else f"{risk} Risk"
        facts["riskometer"] = _fact(
            "Riskometer level", risk, section="key_stats",
            text=f"Riskometer level: {display}",
        )

    benchmark = clean_text(mf.get("benchmark_name")) or clean_text(mf.get("benchmark"))
    if benchmark:
        facts["benchmark"] = _fact("Benchmark", benchmark, section="benchmark")

    lockin = _format_lockin(mf.get("lock_in"))
    if lockin:
        facts["lock_in"] = _fact("Lock-in period", lockin, section="lock_in")

    # -- scheme basics (identity only, no performance) -----------------------
    scheme_name = clean_text(mf.get("scheme_name")) or entry.title
    basics = _section("scheme_basics", heading="Scheme basics")
    basics_items = [
        f"Scheme name: {scheme_name}",
        f"AMC: {clean_text(mf.get('fund_house')) or clean_text(mf.get('amc'))}",
        f"Category: {clean_text(mf.get('sub_category')) or clean_text(mf.get('category'))}",
        f"Plan: {clean_text(mf.get('plan_type'))}",
        f"Option: {clean_text(mf.get('scheme_type'))}",
    ]
    launch_date = _parse_date(mf.get("launch_date")) or _parse_date(mf.get("allotment_date"))
    if launch_date:
        basics_items.append(f"Launch date: {launch_date}")
    if clean_text(mf.get("isin")):
        basics_items.append(f"ISIN: {clean_text(mf.get('isin'))}")
    if clean_text(mf.get("registrar_agent")):
        basics_items.append(f"Registrar: {clean_text(mf.get('registrar_agent'))}")
    basics["items"] = [item for item in basics_items if not item.endswith(": ")]
    _add_section(sections, basics)

    # -- key stats (context only; AUM and NAV are not answerable facts) ------
    key_stats = _section("key_stats", heading="Key stats")
    if expense_ratio:
        key_stats["items"].append(f"Expense ratio: {expense_ratio}%")
    aum = _format_aum(mf.get("aum"))
    if aum:
        key_stats["items"].append(f"Fund size (AUM): {aum}")
    nav = _format_inr(mf.get("nav"))
    nav_date = _parse_date(mf.get("nav_date"))
    if nav:
        key_stats["items"].append(f"NAV: {nav}" + (f" (as of {nav_date})" if nav_date else ""))
    _add_section(sections, key_stats)

    minimums = _section("minimum_investments", heading="Minimum investments")
    if minimum_sip:
        minimums["items"].append(f"Minimum SIP: {_format_inr(minimum_sip)}")
    if minimum_lumpsum:
        minimums["items"].append(f"Minimum lumpsum investment: {_format_inr(minimum_lumpsum)}")
    additional = _format_inr(mf.get("mini_additional_investment"))
    if additional:
        minimums["items"].append(f"Minimum additional investment: {additional}")
    _add_section(sections, minimums)

    if "exit_load" in facts:
        exit_section = _section("exit_load", heading="Exit load")
        exit_section["paragraphs"].append(facts["exit_load"]["text"])
        _add_section(sections, exit_section)

    if "lock_in" in facts:
        lock_section = _section("lock_in", heading="Lock-in")
        lock_section["paragraphs"].append(facts["lock_in"]["text"])
        _add_section(sections, lock_section)

    if "benchmark" in facts:
        bench_section = _section("benchmark", heading="Benchmark")
        bench_section["paragraphs"].append(facts["benchmark"]["text"])
        _add_section(sections, bench_section)

    description = _sanitize_narrative(mf.get("description") or "")
    if description:
        about = _section("about", heading="Investment objective")
        about["paragraphs"].append(description)
        _add_section(sections, about)
    elif mf.get("description"):
        warnings.append("investment objective dropped: performance or advice wording")

    for fact_name in entry.expected_facts:
        if fact_name not in facts and fact_name not in ("scheme_basics",):
            warnings.append(f"missing expected fact: {fact_name}")

    document = _document(
        entry,
        title=scheme_name,
        source_date=_parse_date(mf.get("nav_date")),
        scheme={
            "scheme_slug": entry.scheme_slug,
            "scheme_name": scheme_name,
            "amc": clean_text(mf.get("fund_house")) or clean_text(mf.get("amc")),
            "category": clean_text(mf.get("sub_category")) or clean_text(mf.get("category")),
            "plan": clean_text(mf.get("plan_type")),
            "option": clean_text(mf.get("scheme_type")),
        },
        facts=facts,
        sections=sections,
        warnings=warnings,
        extractor_version=EXTRACTOR_VERSION_SCHEME,
    )
    return document, None


# --------------------------------------------------------------------- AMC page


def extract_groww_amc(soup: BeautifulSoup, entry: SourceEntry) -> tuple[dict, str | None]:
    page = _next_data(soup)
    blocks = page.get("amcMainPageData")
    if not isinstance(blocks, list):
        raise ExtractionError("groww AMC page has no amcMainPageData")

    target = next(
        (
            item
            for item in blocks
            if isinstance(item, dict) and item.get("search_id") == "hdfc-mutual-funds"
        ),
        None,
    )
    if target is None:
        raise ExtractionError("AMC payload does not contain hdfc-mutual-funds")

    key = target.get("key_information") or {}
    fund_house = clean_text(key.get("fund_house")) or "HDFC Mutual Fund"
    incorporation = _parse_date(key.get("amc_incorporation_date"))
    total_aum = _format_aum(key.get("total_aum"))
    sponsors = key.get("sponsor_name") or []
    if isinstance(sponsors, str):
        sponsors = [sponsors]
    sponsor_text = clean_text(", ".join(clean_text(s) for s in sponsors if s))

    items = [f"Fund house: {fund_house}"]
    if incorporation:
        items.append(f"Incorporated: {incorporation}")
    if total_aum:
        items.append(f"Total AUM: {total_aum}")
    if sponsor_text:
        items.append(f"Sponsor: {sponsor_text}")

    facts = {
        "amc_overview": _fact(
            "AMC overview",
            fund_house,
            section="amc_overview",
            text=f"{fund_house}. " + "; ".join(items[1:]) + "." if items[1:] else fund_house,
        )
    }

    sections = [_section("amc_overview", heading="AMC overview")]
    sections[0]["items"] = items

    content_list = target.get("contentList") or []
    if content_list and isinstance(content_list[0], dict):
        about_html = str(content_list[0].get("content") or "")
        about_soup = BeautifulSoup(about_html, "lxml")
        paragraphs = []
        for paragraph in about_soup.find_all(["p", "li"]):
            cleaned = _sanitize_narrative(paragraph.get_text(" ", strip=True))
            if cleaned:
                paragraphs.append(cleaned)
        if paragraphs:
            about = _section("about", heading="About the AMC")
            about["paragraphs"] = paragraphs
            _add_section(sections, about)

    document = _document(
        entry,
        title=fund_house,
        source_date=None,
        scheme=None,
        facts=facts,
        sections=sections,
        warnings=[],
        extractor_version=EXTRACTOR_VERSION_AMC,
    )
    return document, None


# ------------------------------------------------------------------- help pages


def extract_groww_help(soup: BeautifulSoup, entry: SourceEntry) -> tuple[dict, str | None]:
    container = soup.select_one("[class*=contentCard]") or soup.find("main")
    if container is None:
        raise ExtractionError("groww help page has no content container")

    blocks = _blocks(container)
    if not blocks:
        raise ExtractionError("groww help page has no readable content")

    question = next(
        (
            text
            for kind, text in blocks
            if kind == "heading" and text.rstrip().endswith("?")
        ),
        None,
    )
    if question is None:
        heading = next((text for kind, text in blocks if kind == "heading"), entry.title)
        question = heading

    steps: list[str] = []
    notes: list[str] = []
    answer: list[str] = []
    for kind, text in blocks:
        if text == question:
            continue
        if kind == "item":
            cleaned = _sanitize_narrative(text)
            if cleaned:
                steps.append(cleaned)
        elif text.lower().startswith("note"):
            cleaned = _sanitize_narrative(text)
            if cleaned:
                notes.append(cleaned)
        elif kind == "paragraph":
            cleaned = _sanitize_narrative(text)
            if cleaned:
                answer.append(cleaned)

    wants_definition = "definition" in entry.expected_facts
    wants_steps = "statement_download" in entry.expected_facts

    sections: list[dict] = []
    if wants_definition:
        definition_section = _section("definition", heading=question)
        definition_section["paragraphs"] = answer
        definition_section["items"] = steps
        _add_section(sections, definition_section)
    else:
        if answer:
            answer_section = _section("answer", heading=question)
            answer_section["paragraphs"] = answer
            _add_section(sections, answer_section)
        if steps:
            steps_section = _section("statement_steps", heading="Steps")
            steps_section["items"] = steps
            _add_section(sections, steps_section)
    if notes:
        note_section = _section("note", heading="Note")
        note_section["paragraphs"] = notes
        _add_section(sections, note_section)

    facts: dict[str, dict] = {}
    if wants_definition:
        summary_text = answer[0] if answer else (steps[0] if steps else "")
        if summary_text:
            facts["definition"] = _fact(
                "Definition", summary_text, section="definition"
            )
    elif wants_steps and steps:
        facts["statement_download"] = _fact(
            "Statement download",
            f"{len(steps)} steps",
            section="statement_steps",
            text="Steps: " + " ".join(f"{i}. {step}" for i, step in enumerate(steps, 1)),
        )

    document = _document(
        entry,
        title=question,
        source_date=None,
        scheme=None,
        facts=facts,
        sections=sections,
        warnings=[],
        extractor_version=EXTRACTOR_VERSION_ARTICLE,
    )
    return document, None


# ---------------------------------------------------------- AMFI / SEBI articles


def extract_article(soup: BeautifulSoup, entry: SourceEntry) -> tuple[dict, str | None]:
    candidates = [
        node
        for selector in (".page-content-wrapper", "main", "article")
        if (node := soup.select_one(selector)) is not None
    ]
    if not candidates:
        body = soup.body
        if body is None:
            raise ExtractionError("page has no article container")
        candidates = [body]
    container = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)))

    blocks = _blocks(container)
    if not blocks:
        raise ExtractionError("page has no readable article content")

    wants_steps = "statement_download" in entry.expected_facts
    has_items = any(kind == "item" for kind, _ in blocks)
    if wants_steps:
        primary_name = "statement_steps" if has_items else "statement_download"
    else:
        primary_name = "definition"
    sections, warnings = _generic_sections(blocks, primary_name=primary_name)

    facts: dict[str, dict] = {}
    if "lock_in" in entry.expected_facts:
        joined = " ".join(
            text
            for section in sections
            for text in [*section["paragraphs"], *section["items"]]
        )
        match = re.search(
            r"lock[- ]in (?:period )?(?:of )?([a-z0-9 .-]{1,20}?(?:years?|months?|days?))",
            joined,
            re.IGNORECASE,
        )
        if match:
            value = clean_text(match.group(1)).rstrip(".")
            facts["lock_in"] = _fact(
                "Lock-in period",
                value,
                section="definition",
                text=f"Lock-in period: {value}",
            )
            sections.append(
                {
                    "name": "lock_in",
                    "heading": "Lock-in",
                    "paragraphs": [facts["lock_in"]["text"]],
                    "items": [],
                }
            )

    document = _document(
        entry,
        title=entry.title,
        source_date=None,
        scheme=None,
        facts=facts,
        sections=sections,
        warnings=warnings,
        extractor_version=EXTRACTOR_VERSION_ARTICLE,
    )
    return document, None


# ---------------------------------------------------------------------- dispatch


def _document(
    entry: SourceEntry,
    *,
    title: str,
    source_date: str | None,
    scheme: dict | None,
    facts: dict,
    sections: list[dict],
    warnings: list[str],
    extractor_version: str,
) -> dict:
    return {
        "schema_version": 1,
        "source_id": entry.id,
        "url": str(entry.url),
        "final_url": str(entry.url),
        "doc_type": entry.doc_type,
        "role": entry.role,
        "source_priority": entry.source_priority,
        "extractor_version": extractor_version,
        "title": clean_text(title),
        "fetched_at": None,
        "source_date": source_date,
        "scheme": scheme,
        "facts": facts,
        "sections": sections,
        "warnings": warnings,
    }


def extract_document(
    entry: SourceEntry,
    content: bytes | str,
    *,
    fetched_at: str,
    headers: dict[str, str] | None = None,
    final_url: str | None = None,
) -> dict:
    """Extract one fetched page into the artifact shape used by later phases."""
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = content
    soup = BeautifulSoup(text, "lxml")

    if entry.doc_type == "groww_scheme":
        document, _ = extract_groww_scheme(soup, entry)
    elif entry.doc_type == "groww_amc":
        document, _ = extract_groww_amc(soup, entry)
    elif entry.doc_type == "groww_help":
        document, _ = extract_groww_help(soup, entry)
    else:
        document, _ = extract_article(soup, entry)

    document["fetched_at"] = fetched_at
    document["final_url"] = final_url or str(entry.url)
    document["source_date"] = (
        document.get("source_date")
        or _source_date_from_headers(headers or {})
        or _source_date_from_jsonld(soup)
        or fetched_at[:10]
    )

    violations = policy_violations(document)
    if violations:
        raise ExtractionError(
            f"{entry.id}: extracted document failed the content policy: "
            + "; ".join(violations)
        )
    return document


def covered_facts(document: dict) -> set[str]:
    """Facts the document can actually support, including section-based ones."""
    found = set(document.get("facts", {}))
    for section in document.get("sections", []):
        items = section.get("items") or []
        paragraphs = section.get("paragraphs") or []
        name = section.get("name", "")
        if name in ("statement_steps", "statement_download") and (items or paragraphs):
            found.add("statement_download")
        if name == "definition" and (paragraphs or items):
            found.add("definition")
        if name == "amc_overview" and items:
            found.add("amc_overview")

    # Support pages explain concepts rather than listing them under a fact key.
    # Coverage is therefore derived from the sanitized body text, never from
    # scheme numbers (those only come from the allowlisted scheme fields).
    if document.get("role") == "support":
        text = " ".join(
            [section.get("heading") or "" for section in document.get("sections", [])]
            + [
                entry
                for section in document.get("sections", [])
                for entry in [*section.get("paragraphs", []), *section.get("items", [])]
            ]
        ).lower()
        if "riskometer" in text:
            found.add("riskometer")
        if re.search(r"\b(?:total expense ratio|expense ratio|ter)\b", text):
            found.add("expense_ratio")
    return found


def policy_violations(document: dict) -> list[str]:
    """Return the banned terms present anywhere in the artifact.

    Matching is word-based: ``generating`` contains ``rating`` as a substring but
    is not a rating claim, while ``Return Index`` (a benchmark name) must stay.
    """
    blob = json.dumps(document, ensure_ascii=False, sort_keys=True)
    return sorted({match.group(0).lower() for match in _POLICY_PATTERN.finditer(blob)})


def render_plain_text(document: dict) -> str:
    """Readable sibling of the JSON, used by the inspector and `.txt` artifact."""
    lines = [document.get("title") or document["source_id"], f"Source: {document['url']}"]
    if document.get("source_date"):
        lines.append(f"Source date: {document['source_date']}")
    lines.append(f"Fetched: {document.get('fetched_at') or 'unknown'}")
    lines.append("")

    if document.get("scheme"):
        scheme = document["scheme"]
        lines.append(
            "Scheme: "
            + " | ".join(
                f"{key}={value}" for key, value in scheme.items() if value
            )
        )
        lines.append("")

    for section in document.get("sections", []):
        heading = section.get("heading") or section.get("name")
        lines.append(f"## {heading}")
        for paragraph in section.get("paragraphs", []):
            lines.append(paragraph)
        for index, item in enumerate(section.get("items", []), 1):
            lines.append(f"{index}. {item}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def iter_texts(document: dict) -> Iterable[str]:
    for section in document.get("sections", []):
        yield from section.get("paragraphs", [])
        yield from section.get("items", [])
