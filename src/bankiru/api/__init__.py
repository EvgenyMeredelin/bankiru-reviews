# ── API sub-package ──────────────────────────────────────────────────────────
#
# This package implements the FastAPI REST service (the "api" service in
# docker-compose.yml).  It is the central hub of the bankiru-reviews stack:
#
#   - The parser POSTs crawled reviews here  (POST /reviews)
#   - The UI fetches filtered reviews here   (GET  /reviews)
#   - The embedder backfill runs at startup  (via app.py lifespan)
#
# Key modules:
#   app.py            — FastAPI application factory + lifespan (DB bootstrap,
#                       embedding backfill background task)
#   routes.py         — HTTP route handlers (CRUD, export, summarize)
#   schemas.py        — Pydantic request/response models
#   handlers.py       — Output format handlers (CSV, JSON, Parquet, XLSX)
#                       with S3 upload and pre-signed URL generation
#   summarizer.py     — Recursive map-reduce LLM summarization pipeline
#   model_catalog.py  — TTL-cached Cloud.ru Foundation Models catalog
#   botocore_client.py — Async S3 client factory (aiobotocore)
#   deps.py           — FastAPI dependency injection helpers (DB session,
#                       S3 client, API token auth)
#
# The __main__.py entrypoint configures Logfire tracing and launches uvicorn.
