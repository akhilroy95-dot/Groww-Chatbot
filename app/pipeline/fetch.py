"""HTTP fetching for public pages, with robots.txt and allowlist enforcement.

Rules implemented here (implementation.md phase 1):

* one identifying user agent, 30 s timeout, two retries with backoff for 5xx;
* 2 s between network requests;
* robots.txt checked once per host and cached;
* redirects allowed only when the final host stays on the registry allowlist;
* 403 / CAPTCHA is a hard stop - the page is citation-only or verified manually;
* bytes are stored exactly as received, before parsing.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import Settings
from ..schemas import SourceEntry
from ..sources import is_url_allowed

ACCEPT_HEADER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ROBOTS_TIMEOUT_SECONDS = 15


class FetchError(RuntimeError):
    """A page could not be fetched without violating the fetch rules."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class RawDoc:
    source_id: str
    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str
    headers: dict[str, str]
    fetched_at: str
    sha256: str
    from_cache: bool = False

    @property
    def size(self) -> int:
        return len(self.content)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_client(settings: Settings) -> httpx.Client:
    fetch = settings.corpus.fetch
    return httpx.Client(
        follow_redirects=True,
        timeout=fetch.timeout_seconds,
        headers={
            "User-Agent": fetch.user_agent,
            "Accept": ACCEPT_HEADER,
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )


def _robots_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme or "https", parsed.netloc, "/robots.txt", "", "", ""))


class RobotsCache:
    """Fetch ``robots.txt`` once per host and cache the decision."""

    def __init__(
        self,
        client: httpx.Client,
        user_agent: str,
        *,
        timeout: float = ROBOTS_TIMEOUT_SECONDS,
        max_retries: int = 2,
        sleep=time.sleep,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._decisions: dict[str, tuple[bool, str]] = {}

    def allowed(self, url: str) -> tuple[bool, str]:
        host = urlparse(url).netloc.lower()
        if host in self._decisions:
            return self._decisions[host]

        decision = self._check(url)
        self._decisions[host] = decision
        return decision

    def _check(self, url: str) -> tuple[bool, str]:
        robots_url = _robots_url(url)
        last_error: str | None = None
        response: httpx.Response | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(
                    robots_url,
                    timeout=self._timeout,
                    headers={"User-Agent": self._user_agent, "Accept": "text/plain,*/*"},
                )
            except httpx.HTTPError as exc:
                response = None
                last_error = type(exc).__name__
            else:
                if response.status_code < 500:
                    break
                last_error = f"HTTP {response.status_code}"
            if attempt < self._max_retries:
                _sleep_backoff(attempt, self._sleep)

        if response is None:
            return False, f"robots.txt unreachable ({last_error}); skipping to stay polite"

        status = response.status_code
        if status == 200:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            if parser.can_fetch(self._user_agent, url):
                return True, "allowed by robots.txt"
            return False, "disallowed by robots.txt"
        if status in (401, 403):
            return False, f"robots.txt returned HTTP {status}; treating the host as disallowed"
        if 400 <= status < 500:
            # No robots.txt (404) means no restrictions per the robots exclusion standard.
            return True, f"no robots.txt (HTTP {status}); allowed"
        return False, f"robots.txt returned HTTP {status}; skipping to stay polite"


def _sleep_backoff(attempt: int, sleep) -> None:
    sleep(min(2.0 * (attempt + 1), 8.0))


def _request_with_retries(
    client: httpx.Client,
    url: str,
    *,
    user_agent: str,
    timeout: float,
    max_retries: int,
    sleep,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = client.get(
                url,
                timeout=timeout,
                headers={"User-Agent": user_agent, "Accept": ACCEPT_HEADER},
            )
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            _sleep_backoff(attempt, sleep)
            continue

        if response.status_code >= 500 and attempt < max_retries:
            _sleep_backoff(attempt, sleep)
            continue
        return response

    raise FetchError(f"network error after {max_retries + 1} attempt(s): {last_error}")


def fetch_source(
    entry: SourceEntry,
    settings: Settings,
    *,
    client: httpx.Client,
    allowlist_domains: list[str],
    robots: RobotsCache | None = None,
    sleep=time.sleep,
) -> RawDoc:
    """Fetch one source. Never retries a 403 and never leaves the allowlist."""
    fetch = settings.corpus.fetch
    url = str(entry.url)
    robots = robots or RobotsCache(client, fetch.user_agent)

    allowed, reason = robots.allowed(url)
    if not allowed:
        raise FetchError(f"{entry.id}: {reason}: {url}")

    response = _request_with_retries(
        client,
        url,
        user_agent=fetch.user_agent,
        timeout=fetch.timeout_seconds,
        max_retries=fetch.max_retries,
        sleep=sleep,
    )

    final_url = str(response.url)
    if not is_url_allowed(final_url, allowlist_domains):
        raise FetchError(
            f"{entry.id}: redirect left the allowlist ({final_url}); refusing to use the page",
            status=response.status_code,
        )

    if response.status_code == 403:
        raise FetchError(
            f"{entry.id}: HTTP 403 - bot protection is never bypassed; mark the page citation-only",
            status=403,
        )
    if response.status_code != 200:
        raise FetchError(f"{entry.id}: HTTP {response.status_code} for {url}", status=response.status_code)

    content_type = response.headers.get("content-type", "")
    if content_type and "html" not in content_type.lower() and "xml" not in content_type.lower():
        raise FetchError(
            f"{entry.id}: expected HTML, got {content_type!r}", status=response.status_code
        )

    content = response.content
    if not content:
        raise FetchError(f"{entry.id}: empty response body", status=response.status_code)

    return RawDoc(
        source_id=entry.id,
        url=url,
        final_url=final_url,
        status=response.status_code,
        content=content,
        content_type=content_type,
        headers={key.lower(): value for key, value in response.headers.items()},
        fetched_at=utc_now(),
        sha256=sha256_bytes(content),
    )
