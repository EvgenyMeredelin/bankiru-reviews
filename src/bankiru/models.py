"""SQLAlchemy ORM models.

Two tables — `bankiru.reviews` and `bankiru.review_embeddings`
(schema = `bankiru`). The schema is owned by this project and bootstrapped
at API startup via `create_all_tables()`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, MetaData, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
# Vector is provided by the pgvector Python package. It maps to Postgres's
# `vector(N)` column type, which stores fixed-length float arrays and
# supports distance operators (<=> for cosine, <-> for L2, etc.).
from pgvector.sqlalchemy import Vector


# ── Declarative base with a dedicated Postgres schema ────────────────────
# All tables in this project live under the "bankiru" schema, keeping them
# isolated from other applications that may share the same Postgres database.
# SQLAlchemy's MetaData(schema=...) ensures that CREATE TABLE, SELECT, etc.
# all use the fully-qualified "bankiru.<table>" name automatically.
class Base(DeclarativeBase):
    metadata = MetaData(schema="bankiru")


# ── Reviews table ────────────────────────────────────────────────────────
# Stores negative customer reviews scraped from banki.ru by the parser service.
# Each row represents a single review with its metadata (date, bank, product,
# location, URL). The parser POSTs batches to the API, which inserts them here.
#
# Column design notes:
#   - All text columns use unbounded Text (not VARCHAR) because review bodies
#     and bank names have no predictable max length.
#   - datePublished is indexed for date-range filters in GET /reviews and
#     DELETE /reviews/by-date.
#   - bankName, product, and location are indexed for the corresponding
#     filter dropdowns in the Gradio UI (exact match / prefix match).
#   - reviewBody is intentionally NOT indexed — full-text search is handled
#     by the pgvector semantic search path (ReviewEmbedding + HNSW index).
#   - Deduplication uses md5(reviewBody) + product at insert time, not a
#     unique constraint, to avoid index bloat on the large text column.
class Review(Base):
    __tablename__ = "reviews"

    # Auto-increment surrogate key. Not exposed in the public API response
    # but used internally for DELETE /reviews and as the FK target for embeddings.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Publication timestamp from banki.ru's JSON-LD structured data.
    # Indexed (B-tree) for efficient date-range filtering in GET /reviews.
    datePublished: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # Full review text, cleaned by parser/tools.py (HTML stripped, emoji removed).
    # NOT indexed — semantic search uses the ReviewEmbedding table instead.
    reviewBody: Mapped[str] = mapped_column(Text, nullable=False)
    # Bank name from the JSON-LD `itemReviewed.name` field. Indexed for the
    # bank filter dropdown in the UI (exact match via IN clause).
    bankName: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Canonical URL of the review's detail page on banki.ru. Used for
    # deduplication (in-memory by the crawler) and for XLSX row colouring.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Author's city, extracted from the detail page. Indexed for the location
    # filter (prefix match via startswith). Empty string if unavailable.
    location: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Human-readable banking product label (e.g. "Кредитная карта").
    # Indexed for the product filter dropdown (exact match via IN clause).
    product: Mapped[str] = mapped_column(Text, nullable=False, index=True)


# ── Review embeddings table ─────────────────────────────────────────────
# Stores vector embeddings for semantic search (the "Semantic search" field in the UI).
# Each row holds a 1024-dimensional BAAI/bge-m3 embedding of enriched passage text:
# ``{bankName} | {product} | {location}\n{reviewBody}`` with the BGE-M3 passage prefix
# applied at embed time (see embedder.format_review_for_embedding).
#
# Design decisions:
#   - One-to-one relationship with Review via review_id (PK + FK).
#   - ON DELETE CASCADE: when a review is deleted, its embedding is
#     automatically removed — no orphan vectors.
#   - Kept in a separate table (not a column on Review) to avoid loading
#     large float arrays on every review query. The embeddings are only
#     joined when a semantic search is performed.
#   - Vector(1024) matches the EMBEDDINGS_DIMENSIONS config setting and the
#     BAAI/bge-m3 model output size. Changing the model requires updating
#     both this column definition and the config value.
#   - The HNSW index for approximate nearest-neighbor search is created
#     separately in db.py (not here) because SQLAlchemy's create_all()
#     does not natively support pgvector HNSW index syntax.
class ReviewEmbedding(Base):
    __tablename__ = "review_embeddings"

    # Primary key AND foreign key to reviews.id. The 1:1 relationship means
    # each review has at most one embedding. ON DELETE CASCADE ensures that
    # deleting a review automatically removes its embedding — no orphan vectors.
    review_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reviews.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 1024-dimensional float vector. The dimension must match the embedding
    # model output (BAAI/bge-m3 = 1024) and the EMBEDDINGS_DIMENSIONS config.
    # The HNSW index for approximate nearest-neighbor search is created
    # separately in db.py via raw DDL (SQLAlchemy doesn't support HNSW natively).
    embedding: Mapped[list[float]] = mapped_column(
        Vector(1024), nullable=False,
    )


# Convenience list of column names from the Review table.
# Used by handlers.py to convert ORM rows to dicts for DataFrame construction,
# and by routes.py for dynamic column access. This avoids hardcoding column
# names in multiple places — if a column is added to Review, it automatically
# appears in exports.
review_columns = Review.__table__.columns.keys()
