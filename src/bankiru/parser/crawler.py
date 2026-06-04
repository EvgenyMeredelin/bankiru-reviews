"""Fully sequential crawler on top of `BankiruClient`.

Products are crawled one by one. Within each product, listing pages are
fetched one by one. For each listing page, detail pages are fetched one by
one. Every request goes through the same back-off client — there is no
concurrency anywhere. This mirrors the behaviour of the original parser that
ran reliably without triggering WAF bans.

Connect-level errors (WAF bans) are handled transparently by the client:
it backs off and retries indefinitely until the connection succeeds, so the
crawler never loses data due to a temporary ban.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from zoneinfo import ZoneInfo

import logfire
import pandas as pd
from dateutil.relativedelta import relativedelta
from dateutil.utils import today

from bankiru.config import get_settings
from bankiru.parser.client import BankiruClient
from bankiru.parser.settings import (
    BASE_URL,
    LOC_PATTERN,
    PAGE_URL,
    PRODUCTS,
    REVIEW_CONTENT_PATTERN,
    REVIEW_URL_PATTERN,
)
from bankiru.parser.tools import clean_text_pipe


class BankiruCrawler:
    """Sequential crawler. One instance per run; pass it a fresh `BankiruClient`.

    The crawler iterates through all banking products defined in PRODUCTS
    (settings.py), fetching listing pages and detail pages one at a time.
    For each product, it paginates through listing pages until it either:
      - Finds a review older than start_date (hit_left_boundary)
      - Finds a page with no review markup (past the last page)

    Collected records are deduplicated by (reviewBody, product) before
    being returned to the caller (runner.py), which POSTs them to the API.
    """

    def __init__(self, client: BankiruClient) -> None:
        """Create a crawler with the given HTTP client.

        Args:
            client: A warmed-up BankiruClient instance (cookie jar seeded).
        """
        self.client = client
        # Accumulator for all review records across all products.
        # Each record is a dict matching the API's Review schema.
        self.records: list[dict] = []

    async def crawl_all(
        self,
        *,
        start_date: dt.datetime | None = None,
        end_date: dt.datetime | None = None,
        days: int = 1,
    ) -> list[dict]:
        """Crawl every product sequentially; return deduplicated records.

        Parameters
        ----------
        start_date:
            Explicit start of the date window (inclusive).
            When ``None``, computed as ``today(tz) - relativedelta(days=days)``.
        end_date:
            Explicit end of the date window (exclusive).
            When ``None``, defaults to ``today(tz)`` (midnight today).
        days:
            Fallback day offset used only when *start_date* is ``None``.
        """
        tz = ZoneInfo(get_settings().PARSER_TIMEZONE)
        start_date = start_date or (today(tz) - relativedelta(days=days))
        end_date = end_date or today(tz)

        # Ensure both boundaries are naive so they can be compared with
        # date_published values parsed from banki.ru HTML (always naive,
        # implicitly in PARSER_TIMEZONE).
        start_date = start_date.replace(tzinfo=None)
        end_date = end_date.replace(tzinfo=None)

        with logfire.span(
            "crawl_all start={start} end={end}",
            start=start_date.isoformat(), end=end_date.isoformat(),
        ):
            for product in PRODUCTS:
                await self._crawl_product(product, start_date, end_date)

        return self._deduplicated()

    def _deduplicated(self) -> list[dict]:
        """Remove duplicate reviews by (reviewBody, product) combination.

        Duplicates can arise because:
          - banki.ru uses both "corporate" and "legal" slugs for the same
            product category (see settings.py PRODUCTS dict)
          - A review may appear on multiple listing pages during pagination

        Uses pandas for efficient deduplication on large record sets.
        """
        if not self.records:
            return []
        return (
            pd.DataFrame.from_records(self.records)
            .drop_duplicates(subset=["reviewBody", "product"])
            .to_dict(orient="records")
        )

    # ── per-product loop ────────────────────────────────────────────────
    async def _crawl_product(
        self,
        product: str,
        start_date: dt.datetime,
        end_date: dt.datetime,
    ) -> None:
        """Crawl all listing pages for a single product within the date window.

        Pagination logic:
          - Iterate pages 1, 2, 3, ... (infinite counter via itertools.count)
          - For each page, extract review candidates within [start_date, end_date)
          - For each candidate, fetch the detail page to get the author's city
          - Stop when:
            a) hit_left_boundary: found a review older than start_date
               (reviews are sorted newest-first on banki.ru)
            b) not any_matched: page has no review markup (past last page)
            c) response is None: all retries exhausted for this page

        Args:
            product: URL slug for the product (e.g. "creditcards")
            start_date: Inclusive start of the date window (naive datetime)
            end_date: Exclusive end of the date window (naive datetime)
        """
        # Look up the human-readable product label (e.g. "Кредитная карта")
        # for use in the final review record.
        product_label = PRODUCTS[product]

        with logfire.span("crawl product={product}", product=product):
            for page in itertools.count(1):
                # Build the listing page URL with filters: type=all, rate=1&2
                # (only negative reviews with ratings 1 or 2 out of 5).
                url = PAGE_URL.format(base=BASE_URL, product=product, page=page)
                response = await self.client.get(url, product=product)
                if response is None:
                    # All retries exhausted for this page — skip this product.
                    return

                # Normalise whitespace and remove backslashes from the HTML.
                # This simplifies regex matching in _extract_candidates().
                page_text = " ".join(response.text.replace("\\", "").split())
                candidates, hit_left_boundary, any_matched = self._extract_candidates(
                    page_text, product, product_label, url, start_date, end_date,
                )

                # Fetch the detail page for each candidate to extract the
                # author's city (location). This is done sequentially — one
                # request at a time through the same pacing client.
                for candidate in candidates:
                    await self._enrich_one(candidate)

                if hit_left_boundary:
                    # Found a review older than start_date — we've passed the
                    # left boundary of our date window. No need to paginate further.
                    return

                if not any_matched:
                    # No review markup found on this page — we've gone past
                    # the last listing page for this product.
                    return
                # any_matched=True but candidates=[] means all reviews on this
                # page are newer than end_date; paginate forward to find the
                # window reviews on the next page.

    # ── extraction ──────────────────────────────────────────────────────
    def _extract_candidates(
        self,
        page_text: str,
        product_key: str,
        product_label: str,
        listing_url: str,
        start_date: dt.datetime,
        end_date: dt.datetime,
    ) -> tuple[list[dict], bool, bool]:
        """Parse review candidates from a listing page.

        Returns (candidates, hit_left_boundary, any_matched).

        `any_matched` is False only when the page contains no review markup at
        all — the reliable signal that we have gone past the last listing page.
        Callers must not stop pagination merely because `candidates` is empty:
        that can happen when all reviews on the page are newer than `end_date`
        (e.g. a burst of today-reviews fills page 1) and the window reviews
        are on the next page.
        """
        candidates: list[dict] = []
        hit_left_boundary = False
        any_matched = False

        # Pair up content matches (JSON-LD structured data) with URL matches
        # (review detail page links). Both regexes match in the same order
        # on the page, so zip() correctly pairs them.
        # Materialise iterators so we can compare counts before zipping.
        # A mismatch means the page HTML changed or a regex missed a review;
        # zip() would silently drop the extra matches without this check.
        content_matches = list(REVIEW_CONTENT_PATTERN.finditer(page_text))
        url_matches = list(REVIEW_URL_PATTERN.finditer(page_text))
        if len(content_matches) != len(url_matches):
            logfire.warning(
                "Listing page match count mismatch: {content} content vs {url} url",
                content=len(content_matches),
                url=len(url_matches),
            )
        match_pairs = zip(content_matches, url_matches)

        for content_match, url_match in match_pairs:
            any_matched = True
            # Reconstruct the JSON-LD object from the regex capture groups.
            # The regex captures only the parts we need (datePublished,
            # reviewBody, itemReviewed.name), skipping author/rating fields.
            # Malformed JSON on one review must not abort the whole page.
            try:
                raw = json.loads("".join(content_match.groups()))
            except json.JSONDecodeError as exc:
                logfire.warning(
                    "Skipping malformed review JSON-LD on listing page: {exc}",
                    exc=str(exc),
                )
                continue
            date_published = dt.datetime.strptime(
                raw["datePublished"], "%Y-%m-%d %H:%M:%S"
            )
            # Reviews are sorted newest-first on banki.ru. Skip reviews
            # newer than end_date (they're outside our window).
            if date_published >= end_date:
                continue
            # If we hit a review older than start_date, we've passed the
            # left boundary — all subsequent reviews will be even older.
            if date_published < start_date:
                hit_left_boundary = True
                break

            # Store the candidate with internal metadata (prefixed with _)
            # that will be consumed by _enrich_one() and then discarded.
            candidates.append({
                "_raw":         raw,
                "_review_url":  BASE_URL + url_match.group(1),
                "_listing_url": listing_url,
                "_product_key":   product_key,
                "_product_label": product_label,
            })

        return candidates, hit_left_boundary, any_matched

    # ── detail enrichment ───────────────────────────────────────────────
    async def _enrich_one(self, candidate: dict) -> None:
        """Fetch the detail page for one review and append the finished record.

        Uses the same sleep → request loop as listing pages. On success the
        author's city is extracted; if all attempts fail ``location`` is stored
        as an empty string so the review is never lost.
        """
        raw           = candidate["_raw"]
        review_url    = candidate["_review_url"]
        listing_url   = candidate["_listing_url"]
        product_key   = candidate["_product_key"]
        product_label = candidate["_product_label"]

        response = await self.client.get(
            review_url,
            product=product_key,
            extra_headers={"Referer": listing_url},
        )

        if response is not None:
            m = LOC_PATTERN.search(response.text)
            loc = m.group(1) if m else ""
        else:
            loc = ""

        self.records.append({
            "datePublished": raw["datePublished"],
            "reviewBody":    clean_text_pipe(raw["reviewBody"]),
            "bankName":      raw["itemReviewed"]["name"].strip(),
            "url":           review_url,
            "location":      loc,
            "product":       product_label,
        })
