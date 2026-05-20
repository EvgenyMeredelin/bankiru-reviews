"""SQLAlchemy ORM models.

Single table `bankiru.reviews` (schema = `bankiru`). The schema is owned by
this project and bootstrapped at API startup via `create_all_tables()`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, MetaData, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="bankiru")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datePublished: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    reviewBody: Mapped[str] = mapped_column(Text, nullable=False)
    bankName: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    product: Mapped[str] = mapped_column(Text, nullable=False, index=True)


review_columns = Review.__table__.columns.keys()
