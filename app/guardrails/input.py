"""G1 input gate: length, PII, injection, advice, and performance.

This module is intentionally model-free.  It returns a terminal decision for
unsafe or out-of-scope input before retrieval or generation runs, which is what
makes refusals deterministic and testable with no network access.

The full Phase 7 matrix lives in ``implementation.md``; the checks here are the
ones the answer path and UI need today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..config import Settings
from ..logging import find_pii
from ..schemas import SourceRegistry
from ..pipeline.retrieve import Resolution, resolve_scheme, scheme_entry

InputStatus = Literal["ok", "refused", "clarify"]

_ADVICE = re.compile(
    r"\b("
    r"should i|should we|"
    r"buy|sell|hold|"
    r"recommend|recommendation|suggest|"
    r"better|best|worst|"
    r"suitable for me|safe for me|good for me|"
    r"guarantee|guaranteed|"
    r"portfolio|"
    r"financial adviser|financial advisor|"
    r"you are now"
    r")\b",
    re.IGNORECASE,
)

_PERFORMANCE = re.compile(
    r"\b("
    r"returns?|cagr|annualised|annualized|"
    r"outperform|compare|comparison|highest|lowest|"
    r"past performance|will it grow|double my money|multiply my money|"
    r"alpha|beta|sharpe|"
    r"nav history|performance"
    r")\b",
    re.IGNORECASE,
)

_INJECTION = re.compile(
    r"("
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above)?\s*instructions?"
    r"|show\s+(me\s+)?(your|the)\s+system\s+prompt"
    r"|reveal\s+(your|the)\s+(system\s+)?prompt"
    r"|answer\s+without\s+a\s+source"
    r")",
    re.IGNORECASE,
)

_OTHER_AMC = re.compile(
    r"\b("
    r"sbi|axis|icici|kotak|aditya birla|nippon|tata|canara|idfc|bandhan|"
    r"motilal|quant|parag parikh|franklin|dsp|mirae|hsbc|invesco|edelweiss|"
    r"sundaram|whiteoak|navi|pgim|bajaj"
    r")\b",
    re.IGNORECASE,
)

_UNSUPPORTED_HDFC = re.compile(
    r"\b("
    r"small cap|multi cap|multicap|focused|value fund|contra|dividend yield|"
    r"arbitrage|balanced advantage|dynamic asset|liquid|overnight|money market|"
    r"ultra short|short duration|corporate bond|credit risk|gilt|gold|silver|"
    r"index fund|sectoral|thematic|retirement|children"
    r")\b",
    re.IGNORECASE,
)

PII_SOURCE_ID = "groww_help_cas_what"
AMC_SOURCE_ID = "groww_amc_hdfc"
PERFORMANCE_SOURCE_ID = "hdfc_factsheets"
OUT_OF_SCOPE_SOURCE_ID = "amfi_investor"


@dataclass(frozen=True)
class InputDecision:
    status: InputStatus
    reason: str | None = None
    message: str = ""
    source_id: str | None = None
    pii: bool = False
    scheme_slug: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status != "ok"


def _scheme_source(question: str, registry: SourceRegistry) -> tuple[str, str | None]:
    """Pick a scheme page when one is named, otherwise the AMC page."""
    resolution: Resolution = resolve_scheme(question, registry)
    if resolution.status == "resolved":
        entry = scheme_entry(registry, resolution.slug)
        if entry is not None:
            return entry.id, entry.scheme_slug
    return AMC_SOURCE_ID, resolution.slug


def check_input(
    question: str,
    *,
    settings: Settings,
    registry: SourceRegistry,
) -> InputDecision:
    """Return the first terminal input decision, or ``ok``."""
    text = (question or "").strip()
    limit = settings.guardrails.max_input_chars

    if not text:
        return InputDecision(
            "clarify",
            reason="empty_query",
            message="Please ask a question about an HDFC mutual fund scheme.",
            source_id=AMC_SOURCE_ID,
        )

    if len(text) > limit:
        return InputDecision(
            "clarify",
            reason="too_long",
            message=f"Please shorten your question to {limit} characters or fewer.",
            source_id=AMC_SOURCE_ID,
        )

    if settings.guardrails.pii.enabled and find_pii(text):
        return InputDecision(
            "refused",
            reason="pii",
            message=(
                "I cannot process personal or account information. Please remove "
                "PAN, Aadhaar, account, OTP, email, or phone details and ask a "
                "factual question about an HDFC scheme."
            ),
            source_id=PII_SOURCE_ID,
            pii=True,
        )

    if settings.guardrails.prompt_injection.enabled and _INJECTION.search(text):
        return InputDecision(
            "refused",
            reason="out_of_scope",
            message=(
                "I can only answer factual questions about HDFC mutual fund "
                "schemes using the indexed public sources."
            ),
            source_id=OUT_OF_SCOPE_SOURCE_ID,
        )

    if settings.guardrails.advice.enabled and _ADVICE.search(text):
        source_id, scheme_slug = _scheme_source(text, registry)
        return InputDecision(
            "refused",
            reason="advice",
            message=(
                "I cannot give investment advice or recommendations. I can share "
                "factual details such as expense ratio, exit load, minimum SIP, "
                "riskometer, or benchmark."
            ),
            source_id=source_id,
            scheme_slug=scheme_slug,
        )

    if settings.guardrails.performance.enabled and _PERFORMANCE.search(text):
        source_id, scheme_slug = _scheme_source(text, registry)
        if not settings.guardrails.performance.link_to_factsheet:
            source_id = AMC_SOURCE_ID
        return InputDecision(
            "refused",
            reason="performance",
            message=(
                "I cannot discuss returns or compare performance. Please see the "
                "official HDFC factsheet for performance data."
            ),
            source_id=PERFORMANCE_SOURCE_ID if source_id != AMC_SOURCE_ID else source_id,
            scheme_slug=scheme_slug,
        )

    # Scope: another AMC or an HDFC category outside the four indexed schemes.
    if _OTHER_AMC.search(text) and not re.search(r"\bhdfc\b", text, re.IGNORECASE):
        return InputDecision(
            "refused",
            reason="out_of_scope",
            message=(
                "I can only answer for the four HDFC schemes indexed in this demo: "
                "Large Cap, Flexi Cap, ELSS, and Mid Cap direct-growth plans."
            ),
            source_id=OUT_OF_SCOPE_SOURCE_ID,
        )

    if re.search(r"\bhdfc\b", text, re.IGNORECASE) and _UNSUPPORTED_HDFC.search(text):
        resolution = resolve_scheme(text, registry)
        if resolution.status != "resolved":
            return InputDecision(
                "refused",
                reason="out_of_scope",
                message=(
                    "That HDFC scheme is not in this demo's index. I can answer for "
                    "HDFC Large Cap, Flexi Cap, ELSS, and Mid Cap direct-growth plans."
                ),
                source_id=AMC_SOURCE_ID,
            )

    return InputDecision("ok")


def pii_kinds(question: str) -> list[str]:
    """Return PII categories only, never the matched values."""
    return sorted(kind.lower() for kind in find_pii(question))
