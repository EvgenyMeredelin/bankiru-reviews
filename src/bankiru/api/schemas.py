"""Pydantic request/response models for the API.

This module defines the data contracts for the API's HTTP endpoints:

  - Review:       inbound model for POST /reviews (parser → API)
  - ReviewsQuery: query parameters for GET /reviews (UI / public clients → API)
  - ReviewOut:    one review in an inline GET /reviews response
  - Response:     outbound model for GET /reviews

The `datePublished` field on the inbound `Review` payload is annotated as
`str` but the post-validation value is a `datetime`. The annotation is the
shape published in OpenAPI; SQLAlchemy accepts the datetime when
constructing the ORM Review object.

Auto-discovery of output formats:
  The `available_output_formats` dict is built by introspecting the handlers
  module for any class whose name ends with "Maker" (e.g. CSVMaker, JSONMaker).
  This means adding a new export format only requires creating a new handler
  class — no changes needed in this module or in routes.py.

Connection to other modules:
  - bankiru.api.handlers — provides the format handler classes (CSVMaker, etc.)
  - bankiru.api.routes   — uses Review for POST validation, ReviewsQuery/Response
                           for GET /reviews query/response
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from bankiru.api import handlers

# ── Auto-discover output format handlers ─────────────────────────────────────
# Introspect the handlers module to find all classes ending with "Maker".
# Each handler exposes an `extension` attribute (e.g. "csv", "xlsx") which
# becomes the key in this dict. This enables the outputFormat query parameter
# to dynamically select the correct handler without hardcoding format names.
available_output_formats = {
    obj.extension: obj
    for name, obj in inspect.getmembers(handlers)
    if name.endswith("Maker")
}

# Build a Literal type from the discovered format names for Pydantic validation.
# This ensures the OpenAPI spec lists all valid format options and rejects
# unknown formats at the validation layer.
outputFormats = Literal[tuple(available_output_formats)]  # type: ignore[valid-type]

# Accepted *input* types for ReviewsQuery date fields (before validation).
# After ``handle_dates``, ``startDate`` / ``endDate`` are ``date | None``.
date_value = str | date | None


class Review(BaseModel):
    """Inbound review model for POST /reviews.

    Matches the dict structure produced by the parser's crawler. The parser
    sends a list of these as the JSON body of the POST request.

    Fields correspond 1:1 to the columns of the Review ORM model (models.py).
    """

    model_config = ConfigDict(extra="forbid")

    datePublished: str    # "YYYY-MM-DD HH:MM:SS" — validated and converted below
    reviewBody: str       # cleaned review text (HTML stripped, emoji removed)
    bankName: str         # bank name from banki.ru's JSON-LD structured data
    url: str              # canonical URL of the review's detail page
    location: str         # author's city (empty string if unavailable)
    product: str          # human-readable product label (e.g. "Кредитная карта")

    @field_validator("datePublished", mode="after")
    @classmethod
    def handle_datePublished(cls, value: str) -> datetime:  # type: ignore[override]
        """Parse the date string into a datetime object.

        The parser sends dates as "YYYY-MM-DD HH:MM:SS" strings (the format
        used by banki.ru's JSON-LD). This validator converts them to Python
        datetime objects so SQLAlchemy can store them in the DateTime column.
        """
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


class ReviewOut(BaseModel):
    """One review row in an inline GET /reviews response (no S3 export)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    datePublished: str
    reviewBody: str
    bankName: str
    url: str
    location: str
    product: str

    @field_validator("datePublished", mode="before")
    @classmethod
    def format_datePublished(cls, value: object) -> str:
        """Normalise ORM datetime / string to ``YYYY-MM-DD HH:MM:SS``."""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)


class ReviewsQuery(BaseModel):
    """Query parameters for GET /reviews.

    All fields are optional. When ``outputFormat`` is omitted, matching reviews
    are returned inline in ``Response.reviews`` (no S3 file). When set, results
    are exported and ``url`` is a pre-signed download link.

    Unknown query parameters are rejected (``extra="forbid"`` → HTTP 422).
    ``summarize`` defaults to ``false`` for every caller (public gateway,
    localhost, Gradio).

    An omitted date bound is always resolved against the stored data
    (``routes._resolve_date_range``), whatever ``summarize`` is: omitted
    ``startDate`` becomes the earliest ``datePublished`` in the database,
    omitted ``endDate`` the latest one. The resolved bounds drive the SQL
    filter, the three-month limit on summarization and the echoed fields
    alike, so those can never disagree.
    """

    model_config = ConfigDict(extra="forbid")

    # Stored as ``date | None`` after validation; ``date_value`` is accepted input.
    startDate: date | None = None      # inclusive start of date range
    endDate: date | None = None        # inclusive end of date range
    bankName: list[str] | None = None  # filter by bank name(s)
    location: list[str] | None = None  # filter by city prefix(es)
    product: list[str] | None = None   # filter by product label(s)
    keywords: str | None = None        # free-text semantic search query
    outputFormat: outputFormats | None = None  # type: ignore[assignment]
    summarize: bool = False            # omit → false on every path
    cloudModel: str | None = None      # LLM model when summarize is true

    @field_validator("startDate", "endDate", mode="before")
    @classmethod
    def handle_dates(cls, value: date_value) -> date | None:
        """Normalise date inputs: accept "YYYY-MM-DD", "YYYYMMDD", date objects, or None.

        The Gradio DateTime component sends dates as "YYYYMMDD" strings
        (no dashes). This validator strips dashes and parses both formats.
        Empty strings are treated as None (no filter).
        """
        if value is None or value == "":
            return None
        if isinstance(value, date):
            # ``datetime`` is a ``date`` subclass; normalise to a pure date.
            return value.date() if isinstance(value, datetime) else value
        if isinstance(value, str):
            # Strip dashes to normalise "2025-06-01" → "20250601"
            value = value.replace("-", "")
            return datetime.strptime(value, "%Y%m%d").date()
        raise ValueError(f"Invalid date value: {value!r}")


class Response(ReviewsQuery):
    """Response model for GET /reviews.

    Extends ReviewsQuery (echo query parameters, including ``summarize``) with
    either an S3 export or an inline review list:

      - filename / url: set when ``outputFormat`` was provided
      - reviews: set when ``outputFormat`` was omitted
      - comment: LLM summary, no-results message, or null

    The echoed ``startDate`` / ``endDate`` hold the *effective* bounds, so
    they are never null even when the request omitted them. The one exception
    is an empty table: nothing exists to resolve against, so the request
    values are echoed unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    filename: str | None = None
    url: str | None = None
    comment: str | None = None
    reviews: list[ReviewOut] | None = None
