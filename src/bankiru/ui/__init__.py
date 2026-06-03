# ── UI sub-package ────────────────────────────────────────────────────────────
#
# This package implements the Gradio web UI (the "ui" service in
# docker-compose.yml). It provides a browser-based interface for querying,
# filtering, exporting, and summarizing bank reviews.
#
# The UI is protected by Authentik OIDC authentication — users must log in
# via the organisation's identity provider before accessing the Gradio interface.
#
# Architecture:
#   The UI is a FastAPI app with Gradio mounted at /gradio. FastAPI handles
#   the OIDC login/logout flow and session management, while Gradio provides
#   the interactive review query interface.
#
# Key modules:
#   __main__.py          — entrypoint: configures Logfire, launches uvicorn
#   app.py               — FastAPI app factory: SessionMiddleware, OIDC routes,
#                          Gradio mount with auth_dependency
#   blocks.py            — Gradio UI layout and event handlers (the actual
#                          user-facing interface)
#   choices.py           — static dropdown choices: locations, banks, products,
#                          file formats
#   foundation_models.py — lazy, TTL-cached list of available LLM models for
#                          the "Summary model" dropdown
#
# Data flow:
#   Browser → Nginx (TLS) → FastAPI (OIDC auth) → Gradio UI
#     → GET /reviews on the API service (internal compose network)
#       → S3 pre-signed URL (direct browser download)
#       → LLM summary (displayed in the UI)
