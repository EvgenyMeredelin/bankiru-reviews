"""One daily crawl + POST batch to the API.

This module orchestrates a single crawl run: it creates the HTTP client and
crawler, collects reviews within the configured date window, and POSTs the
batch to the API service. It is called by the APScheduler cron job in
__main__.py (via run_once) and can also be invoked manually for backfills.

The run_once() function is the public entry point. It:
  1. Determines the date window (default: yesterday only)
  2. Creates a BankiruClient (with warmup) and BankiruCrawler
  3. Crawls all products within the date window
  4. POSTs the collected reviews to the API's POST /reviews endpoint

The POST step includes resilience features:
  - Polls the API healthcheck before attempting the POST (handles API restarts)
  - Retries the POST indefinitely with exponential backoff (never loses data)

Connection to other modules:
  - bankiru.parser.__main__  — calls run_once() from the APScheduler cron job
  - bankiru.parser.client    — provides BankiruClient for HTTP requests
  - bankiru.parser.crawler   — provides BankiruCrawler for page iteration
  - bankiru.config           — provides PARSER_DAYS, CREATE_REVIEWS_ENDPOINT,
                               API_TOKEN settings
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import httpx
import logfire

from bankiru.config import get_settings
from bankiru.parser.client import BankiruClient
from bankiru.parser.crawler import BankiruCrawler

logger = logging.getLogger("bankiru.parser.runner")

# Healthcheck polling constants: the parser waits for the API to be ready
# before attempting the POST. This handles the case where the API container
# restarted during the crawl (which can take 30+ minutes).
# Total wait time: 30 × 5.0 = 150 seconds before giving up on healthcheck
# (the POST retry loop will handle any remaining unavailability).
_HEALTHZ_ATTEMPTS = 30
_HEALTHZ_INTERVAL = 5.0  # seconds between healthcheck polls


async def run_once(
    *,
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    """Crawl recent reviews and POST them to the API in one batch.

    Parameters
    ----------
    days:
        Number of past calendar days to collect (default: ``PARSER_DAYS``).
        Ignored when *start_date* is given.
    start_date:
        Explicit start of the date window, ``"YYYY-MM-DD"`` string.
        Parsed as midnight in ``PARSER_TIMEZONE``.
    end_date:
        Explicit end of the date window, ``"YYYY-MM-DD"`` string.
        Parsed as midnight in ``PARSER_TIMEZONE``.
        Defaults to today in ``PARSER_TIMEZONE`` when *start_date* is given
        without *end_date*.
    """
    settings = get_settings()
    if days is None and start_date is None:
        days = settings.PARSER_DAYS

    crawl_kwargs: dict = {}
    if start_date is not None:
        crawl_kwargs["start_date"] = dt.datetime.strptime(start_date, "%Y-%m-%d")
    if end_date is not None:
        crawl_kwargs["end_date"] = dt.datetime.strptime(end_date, "%Y-%m-%d")
    if days is not None:
        crawl_kwargs["days"] = days

    with logfire.span("parser.run_once"):
        try:
            async with BankiruClient() as client:
                crawler = BankiruCrawler(client)
                reviews = await crawler.crawl_all(**crawl_kwargs)
        except RuntimeError as exc:
            logfire.error("run aborted: {exc}", exc=str(exc))
            return

        logfire.info("crawl complete: {n} reviews", n=len(reviews))
        if not reviews:
            logger.warning("no reviews collected; skipping POST")
            return

        await _post_with_retry(settings.CREATE_REVIEWS_ENDPOINT, reviews, settings.API_TOKEN)


async def _wait_for_api(http: httpx.AsyncClient, endpoint: str) -> None:
    """Poll the API healthcheck until it responds 200.

    Derives the ``/healthz`` URL from the reviews *endpoint* (e.g.
    ``http://api:1706/reviews`` → ``http://api:1706/healthz``).  Waits up
    to ``_HEALTHZ_ATTEMPTS × _HEALTHZ_INTERVAL`` seconds (default 150 s)
    before giving up — the POST retry loop will handle any remaining
    unavailability.
    """
    base = endpoint.rsplit("/", 1)[0]  # "http://api:1706"
    healthz_url = f"{base}/healthz"

    for i in range(1, _HEALTHZ_ATTEMPTS + 1):
        try:
            resp = await http.get(healthz_url, timeout=5.0)
            if resp.status_code == 200:
                logfire.info("API ready (healthz ok on poll {i})", i=i)
                return
        except httpx.HTTPError:
            pass
        logfire.info(
            "waiting for API ({i}/{total})…",
            i=i, total=_HEALTHZ_ATTEMPTS,
        )
        await asyncio.sleep(_HEALTHZ_INTERVAL)

    logfire.warning(
        "API did not become ready after {n} polls; proceeding with POST retries",
        n=_HEALTHZ_ATTEMPTS,
    )


async def _post_with_retry(endpoint: str, reviews: list[dict], token: str) -> None:
    """POST the batch to the API with unlimited retries.

    Before the first POST attempt the function polls ``GET /healthz`` to
    wait for the API to be ready (handles the case where the API container
    restarted during the crawl).

    After spending hours crawling, losing the batch to a transient API outage
    would be wasteful.  The retry loop mirrors the crawl client's philosophy:
    keep trying with exponential back-off (capped at 60 s) until the POST
    succeeds.

    Args:
        endpoint: Full URL for POST /reviews (e.g. "http://api:1706/reviews")
        reviews: List of review dicts matching the API's Review schema
        token: API-Token header value for authentication
    """
    # The API-Token header authenticates the parser to the API's write
    # endpoints. The token value comes from the shared API_TOKEN env var.
    headers = {"API-Token": token}
    # 600s timeout: the API may take a long time to process a large batch
    # (bulk INSERT + embedding generation + S3 backup).
    async with httpx.AsyncClient(timeout=600.0) as http:
        # Wait for the API to be healthy before attempting the POST.
        await _wait_for_api(http, endpoint)

        # Unlimited retry loop with exponential backoff (capped at 60s).
        # After spending hours crawling, we never give up on delivering
        # the batch — the data is too valuable to lose.
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await http.post(endpoint, headers=headers, json=reviews)
                response.raise_for_status()
                logfire.info(
                    "POST {endpoint} -> {status}", endpoint=endpoint, status=response.status_code
                )
                return
            except httpx.HTTPError as exc:
                # Exponential backoff: 2, 4, 8, 16, 32, 60, 60, 60, ...
                delay = min(60, 2**attempt)
                logfire.warning(
                    "POST attempt={n} failed: {exc} (retry in {delay}s)",
                    n=attempt, exc=repr(exc), delay=delay,
                )
                await asyncio.sleep(delay)
