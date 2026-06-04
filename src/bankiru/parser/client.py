"""Async HTTP client for banki.ru scraping.

Ban-avoidance strategy
----------------------
Every request (including the first attempt) sleeps a **random** interval
drawn from ``uniform(PARSER_SLEEP_MIN, PARSER_SLEEP_MAX)`` (default 10–20 s).
The unpredictable timing makes the traffic pattern look human and keeps the
average request rate at ~4 req/min — well below the threshold that triggers
banki.ru's WAF.

Retry policy
------------
- **Connect errors** (``ConnectTimeout``, ``ConnectError``) signal a probable
  temporary IP ban.  The client backs off with exponential pauses up to
  ``PARSER_BAN_PAUSE_MAX`` (default 5 min) and retries **indefinitely** until
  the connection succeeds.  This guarantees that a transient ban never causes
  data loss — the crawl simply pauses and resumes once the ban lifts.
- **Other errors** (HTTP status ≠ 200, read timeouts, etc.) are retried up to
  ``PARSER_MAX_RETRIES`` times, then the request is abandoned (returns
  ``None``).

Other improvements over the original parser:
- async/await (non-blocking sleep and I/O)
- split connect / read timeouts (fail fast on unreachable hosts)
- rotating User-Agent and Accept-Language per request
- cookie jar preserved across requests (populated by warmup)
- Logfire spans for observability
- warmup aborts the run early if the origin is unreachable

Keepalive is disabled (fresh TCP connection per request) to match the
original parser's behaviour and avoid server-side throttling on
long-lived connections.
"""

from __future__ import annotations

import asyncio
import random
from types import TracebackType

import httpx
import logfire

from bankiru.config import get_settings
from bankiru.parser.settings import (
    ACCEPT_LANGUAGES,
    BASE_HEADERS,
    BASE_URL,
    USER_AGENTS,
)


def _is_connect_error(exc: BaseException) -> bool:
    """Return True if the exception signals a TCP-level connect failure."""
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))


class BankiruClient:
    """Sequential async HTTP client with randomised pacing and unlimited
    retry on connect errors.

    This client is the single point of HTTP access for the parser. It wraps
    httpx.AsyncClient with:
      - Randomised sleep before every request (ban avoidance)
      - Rotating User-Agent and Accept-Language headers (fingerprint evasion)
      - Unlimited retry with exponential backoff on connect errors (ban recovery)
      - Limited retry on other errors (read timeouts, non-200 status codes)
      - Cookie jar persistence across requests (session continuity)

    Used by BankiruCrawler (crawler.py) for both listing and detail page fetches.
    """

    def __init__(self) -> None:
        """Initialise the HTTP client with settings from config.py.

        The httpx client is configured for sequential, single-connection
        operation with no keepalive — each request opens a fresh TCP
        connection to avoid server-side throttling on long-lived connections.
        """
        s = get_settings()
        # Cache pacing settings as instance attributes for fast access
        # in the hot loop (avoid repeated get_settings() calls).
        self._sleep_min = s.PARSER_SLEEP_MIN
        self._sleep_max = s.PARSER_SLEEP_MAX
        self._max_retries = s.PARSER_MAX_RETRIES
        self._ban_pause_max = s.PARSER_BAN_PAUSE_MAX
        self._client = httpx.AsyncClient(
            # HTTP/2 disabled: banki.ru's CDN sometimes behaves differently
            # with h2; HTTP/1.1 is more predictable for scraping.
            http2=False,
            # Split timeouts: connect timeout is short (fail fast if blocked),
            # read timeout is longer (pages can be slow to render).
            timeout=httpx.Timeout(
                connect=s.PARSER_CONNECT_TIMEOUT,
                read=s.PARSER_READ_TIMEOUT,
                write=10.0,
                pool=5.0,
            ),
            # Follow redirects automatically (banki.ru sometimes 301/302s).
            follow_redirects=True,
            # Single connection, no keepalive: each request opens a fresh
            # TCP connection. This matches the original parser's behaviour
            # and avoids server-side throttling on persistent connections.
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=0,
                keepalive_expiry=0,
            ),
            # Base headers (Accept, Accept-Encoding, Sec-Fetch-*) are shared
            # across all requests. User-Agent and Accept-Language are rotated
            # per-request in the get() method.
            headers=BASE_HEADERS,
        )

    def _random_sleep(self) -> float:
        """Return a random sleep duration in [sleep_min, sleep_max]."""
        return random.uniform(self._sleep_min, self._sleep_max)

    async def __aenter__(self) -> "BankiruClient":
        """Async context manager entry: warm up the session before crawling.

        The warmup() call seeds the cookie jar by visiting the banki.ru
        homepage. This is important because banki.ru sets session cookies
        that are required for subsequent page requests to succeed.
        """
        await self.warmup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Async context manager exit: close the underlying httpx client."""
        await self._client.aclose()

    async def warmup(self) -> None:
        """Seed the cookie jar by visiting the banki.ru homepage.

        This initial request serves two purposes:
          1. Populates the httpx cookie jar with session cookies that
             banki.ru requires for subsequent requests.
          2. Acts as a connectivity check — if the origin is unreachable,
             the entire crawl run is aborted early (via RuntimeError)
             rather than failing on the first real page fetch.

        Raises RuntimeError if the origin is unreachable.
        """
        with logfire.span("warmup banki.ru session"):
            await asyncio.sleep(self._random_sleep())
            try:
                resp = await self._client.get(
                    BASE_URL + "/",
                    headers={
                        "User-Agent": random.choice(USER_AGENTS),
                        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
                    },
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                raise RuntimeError(f"warmup failed — origin unreachable: {exc!r}") from exc
            if resp.status_code >= 400:
                logfire.warning("warmup status={status}", status=resp.status_code)

    async def get(
        self,
        url: str,
        *,
        product: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        """Sleep a random interval, then GET.

        **Connect errors** are retried indefinitely with exponential back-off
        (up to ``PARSER_BAN_PAUSE_MAX``).  **Other errors** are retried up to
        ``PARSER_MAX_RETRIES`` times with the normal randomised sleep, then
        the request is abandoned (returns ``None``).
        """
        headers = extra_headers or {}
        # Track non-connect failures separately from connect errors.
        # Connect errors (probable bans) are retried indefinitely;
        # other errors have a finite retry budget.
        non_connect_failures = 0
        ban_streak = 0  # consecutive connect errors (for backoff calculation)

        attempt = 0
        while True:
            attempt += 1

            # ── Delay calculation ────────────────────────────────────
            # Two modes: normal pacing vs ban-recovery backoff.
            if ban_streak > 0:
                # We're in a connect-error streak — use exponential backoff.
                # Formula: sleep_max * 2^(streak-1), capped at ban_pause_max,
                # with random jitter ×[0.5, 1.5) to avoid synchronised retries.
                base = min(
                    self._sleep_max * 2 ** (ban_streak - 1),
                    self._ban_pause_max,
                )
                delay = min(
                    base * (0.5 + random.random()),  # jitter ×[0.5, 1.5)
                    self._ban_pause_max,
                )
            else:
                # Normal pacing — random sleep in [min, max].
                # The randomised interval makes traffic look human.
                delay = self._random_sleep()

            await asyncio.sleep(delay)

            try:
                # Rotate User-Agent and Accept-Language on every request
                # to avoid trivial fingerprinting by the WAF.
                response = await self._client.get(
                    url,
                    headers={
                        "User-Agent": random.choice(USER_AGENTS),
                        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
                        **headers,
                    },
                )
                logfire.info(
                    "parse {product} {url} attempt={attempt} status={status}",
                    product=product, url=url, attempt=attempt, status=response.status_code,
                )
                if response.status_code == 200:
                    return response

                # Non-200 status — count as a non-connect failure.
                ban_streak = 0
                non_connect_failures += 1

            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                if _is_connect_error(exc):
                    # Probable WAF ban — log and retry indefinitely.
                    ban_streak += 1
                    logfire.warning(
                        "parse {product} {url} attempt={attempt} connect_error={error} "
                        "ban_streak={streak} (backing off)",
                        product=product, url=url, attempt=attempt,
                        error=repr(exc), streak=ban_streak,
                    )
                    # Reset non-connect counter: the ban is a separate issue.
                    non_connect_failures = 0
                    continue

                # Non-connect error (read timeout, reset, etc.)
                ban_streak = 0
                logfire.info(
                    "parse {product} {url} attempt={attempt} error={error}",
                    product=product, url=url, attempt=attempt, error=repr(exc),
                )
                non_connect_failures += 1

            # Check if we've exhausted the budget for non-connect failures.
            if non_connect_failures > self._max_retries:
                logfire.info(
                    "parse {product} {url}: giving up after {n} non-connect failures",
                    product=product, url=url, n=non_connect_failures,
                )
                return None
