"""Output format handlers: scalars -> {csv, json, parquet, xlsx} -> S3.

This module implements the Strategy pattern for exporting review data in
different file formats. Each format handler (CSVMaker, JSONMaker, etc.)
inherits from ScalarsHandler and implements the ``body`` property to
serialize a pandas DataFrame into the target format.

The workflow for each export is:
  1. Convert ORM Review objects to a pandas DataFrame (in __init__)
  2. Serialize the DataFrame to the target format (body property, CPU-bound)
  3. Upload the serialized bytes to S3 (upload_contents)
  4. Generate a pre-signed download URL (generate_url)
  5. Optionally summarize the reviews via the LLM pipeline (summarize_reviews)

Serialization (``body`` property) is CPU-bound and runs in a thread pool
via ``asyncio.to_thread`` so it does not block the event loop.  This keeps
the ``/healthz`` endpoint responsive during large exports and prevents
Docker from restarting the API container on a healthcheck timeout.

Connection to other modules:
  - bankiru.api.routes      — instantiates the appropriate handler based on
                              the outputFormat query parameter
  - bankiru.api.schemas     — discovers handler classes via introspection
                              (any class ending in "Maker" is registered)
  - bankiru.api.summarizer  — called by summarize_reviews() for LLM summaries
  - bankiru.config          — provides OBS_BUCKET for S3 uploads
  - bankiru.models          — provides Review ORM model and review_columns
"""

from __future__ import annotations

import asyncio
import collections
import io
import itertools
from abc import ABC, abstractmethod
from functools import cached_property
from uuid import uuid4

import logfire
import pandas as pd
from aiobotocore.client import AioBaseClient
from styleframe import StyleFrame, Styler, utils

from bankiru.api.summarizer import summarize_map_reduce
from bankiru.config import get_settings
from bankiru.models import Review, review_columns

# Configure StyleFrame's auto-sizing factors at module level (not per-call).
# A_FACTOR controls the additional width added per character when auto-sizing
# columns. P_FACTOR is a multiplier applied to the calculated width.
# These values produce reasonable column widths for Cyrillic text.
StyleFrame.A_FACTOR = 3
StyleFrame.P_FACTOR = 1.1


class ScalarsHandler(ABC):
    """Base handler: serialize scalars to a format, upload to S3, sign URL,
    optionally summarize.

    This is an abstract base class that defines the common workflow for all
    output format handlers. Subclasses must implement:
      - extension: file extension (e.g. "csv", "xlsx")
      - content_type: MIME type for the S3 upload
      - body: cached_property that serializes self.df into self._body (BytesIO)

    The handler lifecycle (called by routes.py get_reviews):
      1. __init__: convert ORM objects to DataFrame
      2. upload_contents(): serialize + upload to S3
      3. generate_url(): create a pre-signed download URL
      4. summarize_reviews(): run the LLM map-reduce pipeline
    """

    def __init__(
        self,
        scalars: list[Review],
        botoclient: AioBaseClient,
    ) -> None:
        """Convert ORM Review objects to a DataFrame and store the S3 client.

        Args:
            scalars: List of SQLAlchemy Review ORM instances from the DB query.
            botoclient: Async S3 client for uploading the serialized file.
        """
        # Convert ORM objects to a list of dicts, then to a DataFrame.
        # Using review_columns (derived from the ORM model) ensures all
        # columns are included and the order matches the model definition.
        with logfire.span("Make a dataframe from the scalars"):
            records = [
                {c: getattr(row, c) for c in review_columns}
                for row in scalars
            ]
            self.df = pd.DataFrame.from_records(records)

        with logfire.span("Set the rest of the attributes"):
            self.client = botoclient
            # _body is the BytesIO buffer that subclasses write serialized
            # data into. It's created here and filled by the body property.
            self._body = io.BytesIO()

    @property
    @abstractmethod
    def extension(self) -> str: ...

    @property
    @abstractmethod
    def content_type(self) -> str: ...

    @cached_property
    @abstractmethod
    def body(self) -> io.BytesIO: ...

    @cached_property
    def key(self) -> str:
        """Generate a unique S3 object key using a UUID and the file extension.

        Example: "a1b2c3d4-e5f6-7890-abcd-ef1234567890.xlsx"
        The UUID ensures no collisions between concurrent exports.
        """
        return f"{uuid4()}.{self.extension}"

    def _build_body(self) -> None:
        """Trigger the ``@cached_property`` body serialization (CPU-bound).

        Called via asyncio.to_thread() in upload_contents() to avoid blocking
        the event loop during potentially slow serialization (especially XLSX
        with styling). The seek(0) resets the buffer position after writing
        so it's ready for the S3 upload.
        """
        self.body.seek(0)

    async def upload_contents(self) -> None:
        """Serialize the DataFrame and upload the result to S3.

        Two-step process:
          1. Serialize in a thread pool (CPU-bound work off the event loop)
          2. Upload the bytes to S3 (async I/O)
        """
        # Step 1: Run serialization in a thread to keep the event loop free.
        # This is important because XLSX generation with styling can take
        # several seconds for large datasets.
        with logfire.span("Make a format-specific object"):
            await asyncio.to_thread(self._build_body)

        # Step 2: Upload the serialized bytes to the configured S3 bucket.
        with logfire.span("Put an object to a bucket"):
            await self.client.put_object(
                Bucket=get_settings().OBS_BUCKET,
                Key=self.key,
                Body=self.body,
                ContentType=self.content_type,
            )

    async def generate_url(self) -> str:
        """Generate a pre-signed S3 URL for downloading the uploaded file.

        The URL is valid for 1 hour (aiobotocore default). The browser
        fetches the file directly from OBS using this URL — no server
        round-trip through the API or UI.
        """
        with logfire.span("Generate a pre-signed URL and return it"):
            return await self.client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": get_settings().OBS_BUCKET, "Key": self.key},
            )

    async def summarize_reviews(self, model_name: str) -> str:
        """Map-reduce summarize the unique reviewBody texts in the dataframe.

        Delegates to `bankiru.api.summarizer.summarize_map_reduce`, which
        chunks the input to fit the chosen model's context window and
        recursively reduces — so any number of reviews is supported.

        Only unique review texts are summarized (duplicates are dropped) to
        avoid biasing the summary toward repeated complaints.

        Args:
            model_name: The LLM model to use (e.g. "anthropic/claude-sonnet-4.6").

        Returns:
            Markdown-formatted summary string.
        """
        with logfire.span("Summarize reviews"):
            # .unique() deduplicates review texts before sending to the LLM,
            # reducing token usage and avoiding repetitive summaries.
            texts = self.df.reviewBody.unique().tolist()
            return await summarize_map_reduce(texts, model_name=model_name)


# ── Concrete format handlers ────────────────────────────────────────────────
# Each handler implements the `body` property to serialize the DataFrame
# into a specific format. The `extension` and `content_type` properties
# are used for S3 upload metadata and the download filename.
#
# These classes are auto-discovered by schemas.py via introspection:
# any class in this module whose name ends with "Maker" is registered
# as an available output format.

class CSVMaker(ScalarsHandler):
    """Export reviews as a UTF-8 CSV file."""
    extension = "csv"
    content_type = "text/csv"

    @cached_property
    def body(self) -> io.BytesIO:
        """Serialize the DataFrame to CSV format (UTF-8 encoded)."""
        self.df.to_csv(self._body, index=False, encoding="utf-8")
        return self._body


class JSONMaker(ScalarsHandler):
    """Export reviews as a pretty-printed JSON array."""
    extension = "json"
    content_type = "application/json"

    @cached_property
    def body(self) -> io.BytesIO:
        """Serialize the DataFrame to JSON (records orientation, indented).

        - orient="records": each row becomes a JSON object in an array
        - force_ascii=False: preserve Cyrillic characters as-is
        - indent=4: human-readable formatting
        """
        self.df.to_json(
            self._body,
            orient="records",
            date_format="iso",
            force_ascii=False,
            indent=4,
        )
        return self._body


class ParquetMaker(ScalarsHandler):
    """Export reviews as an Apache Parquet file (columnar, compressed).

    Parquet is the default format because it's compact, fast to read,
    and preserves column types (dates stay as dates, not strings).
    """
    extension = "parquet"
    content_type = "application/vnd.apache.parquet"

    @cached_property
    def body(self) -> io.BytesIO:
        """Serialize the DataFrame to Parquet format (snappy compression)."""
        self.df.to_parquet(self._body, index=False)
        return self._body


class XlsxMaker(ScalarsHandler):
    """Export reviews as a styled Excel workbook (.xlsx).

    The XLSX output includes:
      - Frozen header row (stays visible when scrolling)
      - Dark header with white text
      - Alternating row colours (green/pink) grouped by review URL,
        so reviews from the same page are visually grouped
      - Auto-sized columns (except reviewBody, which is too wide)
      - Consistent date formatting (YYYY-MM-DD HH:mm:ss)

    This is the most complex handler because StyleFrame's per-row style
    merging has quirks with date formatting that require a post-processing
    fix via openpyxl.
    """
    extension = "xlsx"
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    @cached_property
    def body(self) -> io.BytesIO:
        """Serialize the DataFrame to a styled XLSX workbook.

        The styling logic:
          1. Assign a sequential number to each unique URL (review group)
          2. Odd-numbered groups get green background, even get pink
          3. Apply a dark header style with white text
          4. Auto-size all columns except reviewBody (too wide for auto-fit)
          5. Force date format on the datePublished column via openpyxl
             (workaround for StyleFrame's unreliable date formatting)
        """
        # Create a sequential counter for each unique review URL.
        # Reviews from the same URL get the same number, enabling
        # alternating-colour grouping in the spreadsheet.
        number = itertools.count(1)
        enumerator = collections.defaultdict(lambda: next(number))
        # Map each URL to its group number, then determine odd/even.
        review_n = self.df.url.apply(lambda url: enumerator[url])
        odd_row_mask = (review_n % 2).astype(bool)

        # Base style applied to all data cells.
        base_style = Styler(
            font="Consolas",
            font_size=10,
            horizontal_alignment=utils.horizontal_alignments.left,
            wrap_text=False,
            shrink_to_fit=False,
            date_time_format="YYYY-MM-DD HH:mm:ss",
        )

        # Extract base style parameters as a dict so we can merge overrides.
        base_params = vars(base_style)
        sf = StyleFrame(self.df, base_style)

        # Header row: dark background (#57534D) with white text.
        headers_update = {"bg_color": "#57534D", "font_color": "#FFFFFF"}
        headers_params = base_params | headers_update
        sf.apply_headers_style(Styler(**headers_params))

        # Build BOTH row stylers from base_params so each one carries the
        # full base style (including `date_time_format`). Previously the
        # odd-row styler had only `bg_color`, causing it to overwrite the
        # base style on those rows — Excel then rendered datetimes as raw
        # serial numbers.
        odd_row_params = base_params | {"bg_color": "#D0FAE5"}   # light green
        even_row_params = base_params | {"bg_color": "#FAD0E5"}  # light pink

        # Apply alternating row colours based on the review URL grouping.
        # complement_style applies to rows NOT matching the mask (even groups).
        sf.apply_style_by_indexes(
            indexes_to_style=sf[odd_row_mask],
            styler_obj=Styler(**odd_row_params),
            complement_style=Styler(**even_row_params),
            overwrite_default_style=False,
        )

        # Auto-size all columns except reviewBody (which is typically very
        # wide and would make the spreadsheet unusable if auto-sized).
        best_fit_columns = self.df.columns.to_list()
        best_fit_columns.remove("reviewBody")

        with StyleFrame.ExcelWriter(self._body) as writer:
            sf.to_excel(
                excel_writer=writer,
                columns_and_rows_to_freeze="A2",  # freeze the header row
                best_fit=best_fit_columns,
                index=False,
            )

            # Force the date format on every cell of the `datePublished`
            # column. styleframe's per-row style combine is unreliable for
            # `date_time_format` (some rows end up rendered as Excel
            # serial numbers like 46143.92…), so we stamp the column
            # format directly via openpyxl, which always wins.
            sheet = next(iter(writer.sheets.values()))
            date_col_idx = self.df.columns.get_loc("datePublished") + 1  # 1-based
            for row in sheet.iter_rows(
                min_row=2,  # skip the header row
                min_col=date_col_idx,
                max_col=date_col_idx,
            ):
                for cell in row:
                    cell.number_format = "YYYY-MM-DD HH:mm:ss"

        return self._body
