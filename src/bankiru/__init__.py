# ── Root package for the bankiru-reviews project ────────────────────────────
#
# This is a monorepo with a src-layout: the importable package lives at
# src/bankiru/ and is installed into the venv by hatchling (see pyproject.toml).
#
# Four runnable services share this package — each has its own __main__.py:
#   - bankiru.api      — FastAPI REST service (POST/GET/DELETE reviews, S3 export, LLM summaries)
#   - bankiru.parser   — APScheduler cron job that crawls banki.ru daily for negative reviews
#   - bankiru.ui       — Gradio web UI gated by Authentik OIDC, calls the API internally
#   - bankiru.embedder — CLI tool for backfilling/reindexing pgvector embeddings
#
# All four services import shared modules from this package:
#   config.py  — pydantic-settings configuration (all env vars in one place)
#   models.py  — SQLAlchemy ORM models (reviews + review_embeddings tables)
#   db.py      — async SQLAlchemy engine, session factory, table bootstrap
#   logging.py — Logfire observability setup (configure + auto-trace)
#
# The docker-compose.yml runs all three long-lived services (api, parser, ui)
# from a single Docker image; the embedder is run ad-hoc via `docker exec`.

# __version__ is referenced by the FastAPI app factory (api/app.py) to populate
# the OpenAPI spec version field in the Swagger UI.
__version__ = "0.1.0"
