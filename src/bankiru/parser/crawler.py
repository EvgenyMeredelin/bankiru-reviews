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
    """Sequential crawler. One instance per run; pass it a fresh `BankiruClient`."""

    def __init__(self, client: BankiruClient) -> None:
        self.client = client
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
        product_label = PRODUCTS[product]

        with logfire.span("crawl product={product}", product=product):
            for page in itertools.count(1):
                url = PAGE_URL.format(base=BASE_URL, product=product, page=page)
                response = await self.client.get(url, product=product)
                if response is None:
                    return

                page_text = " ".join(response.text.replace("\\", "").split())
                candidates, hit_left_boundary, any_matched = self._extract_candidates(
                    page_text, product, product_label, url, start_date, end_date,
                )

                for candidate in candidates:
                    await self._enrich_one(candidate)

                if hit_left_boundary:
                    return

                if not any_matched:
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

        match_pairs = zip(
            REVIEW_CONTENT_PATTERN.finditer(page_text),
            REVIEW_URL_PATTERN.finditer(page_text),
        )

        for content_match, url_match in match_pairs:
            any_matched = True
            raw = json.loads("".join(content_match.groups()))
            date_published = dt.datetime.strptime(
                raw["datePublished"], "%Y-%m-%d %H:%M:%S"
            )
            if date_published >= end_date:
                continue
            if date_published < start_date:
                hit_left_boundary = True
                break

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
