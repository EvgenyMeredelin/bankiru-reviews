"""Pydantic request/response models for the API.

The `datePublished` field on the inbound `Review` payload is annotated as
`str` but the post-validation value is a `datetime`. The annotation is the
shape published in OpenAPI; SQLAlchemy accepts the datetime when
constructing the ORM Review object.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from bankiru.api import handlers

available_output_formats = {
    obj.extension: obj
    for name, obj in inspect.getmembers(handlers)
    if name.endswith("Maker")
}
outputFormats = Literal[tuple(available_output_formats)]  # type: ignore[valid-type]
date_value = str | date | None


class Review(BaseModel):
    datePublished: str
    reviewBody: str
    bankName: str
    url: str
    location: str
    product: str

    @field_validator("datePublished", mode="after")
    @classmethod
    def handle_datePublished(cls, value: str) -> datetime:  # type: ignore[override]
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


class Request(BaseModel):
    startDate: date_value = None
    endDate: date_value = None
    bankName: list[str] | None = None
    location: list[str] | None = None
    product: list[str] | None = None
    outputFormat: outputFormats = "parquet"  # type: ignore[assignment]
    cloudModel: str | None = None

    @field_validator("startDate", "endDate", mode="before")
    @classmethod
    def handle_dates(cls, value: date_value) -> date_value:
        if value is None or value == "":
            return None
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            value = value.replace("-", "")
            return datetime.strptime(value, "%Y%m%d").date()
        return value


class Response(Request):
    filename: str | None = None
    url: str | None = None
    comment: str | None = None
