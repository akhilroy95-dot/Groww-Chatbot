"""Phase 1 gate: fetch rules (robots, retries, redirects, byte fidelity)."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from app.config import Settings
from app.pipeline.fetch import FetchError, RobotsCache, fetch_source
from app.schemas import SourceEntry

ALLOWLIST = ["groww.in"]
ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
PAGE = b"<html><body><h1>Facts</h1></body></html>"


def entry(url: str) -> SourceEntry:
    return SourceEntry(
        id="test_source",
        title="Test source",
        url=url,
        owner="Test",
        doc_type="groww_help",
        role="support",
        source_priority=2,
        fetch_method="httpx",
    )


def run(
    handler,
    url: str = "https://groww.in/help/facts",
    *,
    sleeps: list[float] | None = None,
):
    delays = sleeps if sleeps is not None else []
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    robots = RobotsCache(client, "test-agent", sleep=delays.append)
    try:
        return fetch_source(
            entry(url),
            Settings(),
            client=client,
            allowlist_domains=ALLOWLIST,
            robots=robots,
            sleep=delays.append,
        )
    finally:
        client.close()


def test_403_is_a_hard_stop_without_retry() -> None:
    page_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        page_requests.append(str(request.url))
        return httpx.Response(403, text="bot protection")

    with pytest.raises(FetchError) as excinfo:
        run(handler)

    assert excinfo.value.status == 403
    assert "never bypassed" in str(excinfo.value)
    assert len(page_requests) == 1


def test_5xx_is_retried_with_backoff() -> None:
    page_attempts: list[str] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        page_attempts.append(str(request.url))
        if len(page_attempts) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    raw = run(handler, sleeps=delays)

    assert raw.status == 200
    assert len(page_attempts) == 3
    assert delays == [2.0, 4.0]


def test_redirect_off_the_allowlist_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        if request.url.host == "evil.example":
            return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})
        return httpx.Response(
            302, headers={"location": "https://evil.example/collected"}
        )

    with pytest.raises(FetchError, match="left the allowlist"):
        run(handler)


def test_robots_disallow_means_no_page_request() -> None:
    page_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        page_requests.append(str(request.url))
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    with pytest.raises(FetchError, match="disallowed by robots.txt"):
        run(handler, url="https://groww.in/private/report")

    assert page_requests == []


def test_missing_robots_txt_allows_the_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    raw = run(handler)

    assert raw.status == 200
    assert raw.content == PAGE


def test_robots_timeout_is_retried() -> None:
    attempts: list[str] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            attempts.append(str(request.url))
            if len(attempts) == 1:
                raise httpx.ConnectTimeout("slow host", request=request)
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    raw = run(handler, sleeps=delays)

    assert raw.status == 200
    assert len(attempts) == 2
    assert 2.0 in delays


def test_bytes_and_sha256_are_exact() -> None:
    body = b"<html><body>caf\xc3\xa9 \xe2\x82\xb9</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    raw = run(handler)

    assert raw.content == body
    assert raw.sha256 == hashlib.sha256(body).hexdigest()
    assert raw.final_url == "https://groww.in/help/facts"
    assert raw.from_cache is False


def test_non_html_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(
            200, content=b"{}", headers={"content-type": "application/json"}
        )

    with pytest.raises(FetchError, match="expected HTML"):
        run(handler)
