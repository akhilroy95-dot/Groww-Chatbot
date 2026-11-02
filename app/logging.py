"""JSONL application logger with PII and secret redaction.

Everything the app writes to ``data/logs/app.jsonl`` passes through :func:`redact`
first, so a query containing a PAN, Aadhaar number, account number, OTP, email,
phone number, or an API key can never be replayed from the logs.

Two different protections are used:

* **Secrets** (``api_key``, ``authorization``, ``password``, ``token`` ...) are
  dropped entirely — the field value becomes ``[REDACTED]``.
* **PII inside free text** is replaced span-by-span with ``[REDACTED:PAN]`` etc.
  Use :func:`fingerprint` when a trace needs to correlate two events without
  storing the original value.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import PROJECT_ROOT

REDACTED = "[REDACTED]"
LOGGER_ROOT = "groww_mf"
DEFAULT_LOG_DIR = PROJECT_ROOT / "data"

# Order matters: contextual patterns run before the bare-number patterns.
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "ACCOUNT": re.compile(
        r"\b(?:a/?c|account)\s*(?:no\.?|number|#)?\s*[:#-]?\s*\d{9,18}\b", re.IGNORECASE
    ),
    "OTP": re.compile(
        r"\b(?:otp|o\.t\.p\.?)\b\s*(?:is|code|:|=|-)?\s*\d{4,6}\b", re.IGNORECASE
    ),
    "AADHAAR": re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"),
    "PAN": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{5}[0-9]{4}[A-Za-z](?![A-Za-z0-9])"),
    "IFSC": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{4}0[A-Za-z0-9]{6}(?![A-Za-z0-9])"),
    "EMAIL": re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+"),
    "PHONE": re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"),
}

_SENSITIVE_TOKENS = {
    "pan",
    "aadhaar",
    "aadhar",
    "otp",
    "email",
    "phone",
    "mobile",
    "password",
    "passwd",
    "secret",
    "authorization",
    "cookie",
    "account",
}

_HASH_SALT = secrets.token_hex(16)

_handlers: dict[tuple[str, Path], logging.FileHandler] = {}


# ----------------------------------------------------------------------- redaction


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def is_sensitive_key(key: str) -> bool:
    """True when a field name is a secret or carries PII.

    ``token_count`` is deliberately *not* sensitive: only a trailing ``_token``
    (an actual credential) is redacted.
    """
    norm = _normalize_key(key)
    if not norm:
        return False
    if "api_key" in norm or "apikey" in norm:
        return True
    tokens = set(norm.split("_"))
    if tokens & _SENSITIVE_TOKENS:
        return True
    return norm == "token" or norm.endswith("_token")


def find_pii(text: str) -> dict[str, list[str]]:
    """Return the PII spans found in ``text``, grouped by kind."""
    found: dict[str, list[str]] = {}
    for kind, pattern in PII_PATTERNS.items():
        matches = [match.group(0) for match in pattern.finditer(text)]
        if matches:
            found[kind] = matches
    return found


def redact_text(text: str) -> str:
    """Replace every PII span in ``text`` with a typed placeholder."""
    redacted = text
    for kind, pattern in PII_PATTERNS.items():
        redacted = pattern.sub(f"[REDACTED:{kind}]", redacted)
    return redacted


def redact(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Recursively redact a JSON-ish structure so it is safe to log."""
    if _depth > 12:
        return "[TRUNCATED]"
    if key is not None and is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def fingerprint(value: str, length: int = 8) -> str:
    """Salted, non-reversible short hash for correlating redacted values.

    The salt is random per process, so hashes from two runs do not link up.
    """
    digest = hashlib.sha256((_HASH_SALT + value).encode("utf-8")).hexdigest()
    return digest[:length]


# ------------------------------------------------------------------------- logger


class JsonlFormatter(logging.Formatter):
    """One JSON object per line, redacted, with a UTC timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {}
        if isinstance(record.msg, dict):
            payload.update(record.msg)
        else:
            payload["event"] = str(record.msg)
        payload["ts"] = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        payload["level"] = record.levelname
        payload["logger"] = record.name
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        safe = redact(payload)
        return json.dumps(safe, ensure_ascii=False, default=str)


def log_file_path(data_dir: str | Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else DEFAULT_LOG_DIR
    return base / "logs" / "app.jsonl"


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_ROOT}.{name}" if name else LOGGER_ROOT)


def _ensure_file_handler(logger: logging.Logger, path: Path) -> logging.FileHandler:
    resolved = path.resolve()
    cache_key = (logger.name, resolved)
    existing = _handlers.get(cache_key)
    if existing is not None:
        return existing
    resolved.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(resolved, encoding="utf-8", delay=True)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(JsonlFormatter())
    handler._groww_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    _handlers[cache_key] = handler
    return handler


def configure_logging(
    data_dir: str | Path | None = None,
    level: str = "INFO",
    *,
    console: bool = False,
) -> logging.Logger:
    """Attach a JSONL file handler (idempotent for the same path)."""
    logger = get_logger()
    _ensure_file_handler(logger, log_file_path(data_dir))
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if console and not any(getattr(h, "_groww_console", False) for h in logger.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.INFO)
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        stream._groww_console = True  # type: ignore[attr-defined]
        logger.addHandler(stream)
    return logger


def log_event(
    event: str,
    /,
    *,
    data_dir: str | Path | None = None,
    level: str = "info",
    **fields: Any,
) -> None:
    """Write one redacted JSONL record. Never raises for logging problems."""
    logger = get_logger()
    try:
        _ensure_file_handler(logger, log_file_path(data_dir))
        logger.log(getattr(logging, level.upper(), logging.INFO), {"event": event, **fields})
    except OSError as exc:  # disk full / read-only checkout: never break a request
        logger.debug("log write failed: %s", exc)


def reset_logging(handlers: Iterable[logging.Handler] | None = None) -> None:
    """Close and detach every handler this module created (used by tests)."""
    global _handlers
    targets = list(handlers) if handlers is not None else [
        handler for handler in logging.getLogger(LOGGER_ROOT).handlers
        if getattr(handler, "_groww_handler", False) or getattr(handler, "_groww_console", False)
    ]
    logger = logging.getLogger(LOGGER_ROOT)
    for handler in targets:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best effort on teardown
            pass
    _handlers = {key: value for key, value in _handlers.items() if value not in targets}
