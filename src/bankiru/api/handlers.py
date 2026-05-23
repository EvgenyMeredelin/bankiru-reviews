"""Output format handlers: scalars -> {csv, json, parquet, xlsx} -> S3.

Serialization (``body`` property) is CPU-bound and runs in a thread pool
via ``asyncio.to_thread`` so it does not block the event loop.  This keeps
the ``/healthz`` endpoint responsive during large exports and prevents
Docker from restarting the API container on a healthcheck timeout.
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

# Set StyleFrame sizing factors once at module level (not per-call).
StyleFrame.A_FACTOR = 3
StyleFrame.P_FACTOR = 1.1


class ScalarsHandler(ABC):
    """Base handler: serialize scalars to a format, upload to S3, sign URL,
    optionally summarize."""

    def __init__(
        self,
        scalars: list[Review],
        botoclient: AioBaseClient,
    ) -> None:
        with logfire.span("Make a dataframe from the scalars"):
            records = [
                {c: getattr(row, c) for c in review_columns}
                for row in scalars
            ]
            self.df = pd.DataFrame.from_records(records)

        with logfire.span("Set the rest of the attributes"):
            self.client = botoclient
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
        return f"{uuid4()}.{self.extension}"

    def _build_body(self) -> None:
        """Trigger the ``@cached_property`` body serialization (CPU-bound)."""
        self.body.seek(0)

    async def upload_contents(self) -> None:
        with logfire.span("Make a format-specific object"):
            await asyncio.to_thread(self._build_body)

        with logfire.span("Put an object to a bucket"):
            await self.client.put_object(
                Bucket=get_settings().OBS_BUCKET,
                Key=self.key,
                Body=self.body,
                ContentType=self.content_type,
            )

    async def generate_url(self) -> str:
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
        """
        with logfire.span("Summarize reviews"):
            texts = self.df.reviewBody.unique().tolist()
            return await summarize_map_reduce(texts, model_name=model_name)


class CSVMaker(ScalarsHandler):
    extension = "csv"
    content_type = "text/csv"

    @cached_property
    def body(self) -> io.BytesIO:
        self.df.to_csv(self._body, index=False, encoding="utf-8")
        return self._body


class JSONMaker(ScalarsHandler):
    extension = "json"
    content_type = "application/json"

    @cached_property
    def body(self) -> io.BytesIO:
        self.df.to_json(
            self._body,
            orient="records",
            date_format="iso",
            force_ascii=False,
            indent=4,
        )
        return self._body


class ParquetMaker(ScalarsHandler):
    extension = "parquet"
    content_type = "application/vnd.apache.parquet"

    @cached_property
    def body(self) -> io.BytesIO:
        self.df.to_parquet(self._body, index=False)
        return self._body


class XlsxMaker(ScalarsHandler):
    extension = "xlsx"
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    @cached_property
    def body(self) -> io.BytesIO:
        number = itertools.count(1)
        enumerator = collections.defaultdict(lambda: next(number))
        review_n = self.df.url.apply(lambda url: enumerator[url])
        odd_row_mask = (review_n % 2).astype(bool)

        base_style = Styler(
            font="Consolas",
            font_size=10,
            horizontal_alignment=utils.horizontal_alignments.left,
            wrap_text=False,
            shrink_to_fit=False,
            date_time_format="YYYY-MM-DD HH:mm:ss",
        )

        base_params = vars(base_style)
        sf = StyleFrame(self.df, base_style)

        headers_update = {"bg_color": "#57534D", "font_color": "#FFFFFF"}
        headers_params = base_params | headers_update
        sf.apply_headers_style(Styler(**headers_params))

        # Build BOTH row stylers from base_params so each one carries the
        # full base style (including `date_time_format`). Previously the
        # odd-row styler had only `bg_color`, causing it to overwrite the
        # base style on those rows — Excel then rendered datetimes as raw
        # serial numbers.
        odd_row_params = base_params | {"bg_color": "#D0FAE5"}
        even_row_params = base_params | {"bg_color": "#FAD0E5"}

        sf.apply_style_by_indexes(
            indexes_to_style=sf[odd_row_mask],
            styler_obj=Styler(**odd_row_params),
            complement_style=Styler(**even_row_params),
            overwrite_default_style=False,
        )

        best_fit_columns = self.df.columns.to_list()
        best_fit_columns.remove("reviewBody")

        with StyleFrame.ExcelWriter(self._body) as writer:
            sf.to_excel(
                excel_writer=writer,
                columns_and_rows_to_freeze="A2",
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
