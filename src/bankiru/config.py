"""Application configuration — the single source of truth for all env vars.

All environment variables consumed by any service are declared here as typed
fields on the ``Settings`` class. Pydantic-settings reads them from the
process environment (or a .env file for local dev).

Production value chain:
  Infisical (secrets store)
    → scripts/start.sh exports to /dev/shm/bankiru-reviews-secrets/.env
      → docker compose injects via ``env_file:`` directive
        → pydantic-settings reads from ``os.environ``

Local development:
  Copy ``.env.example`` to ``.env``, fill in values, then ``uv run``.

Design choice: a single Settings class (not per-service configs) because all
three services share the same .env file and many settings are cross-cutting
(e.g. POSTGRES_URL is used by the API directly and by the embedder CLI).
"""

from __future__ import annotations

# PEP 563: postponed evaluation of annotations — allows using `str | None`
# syntax without runtime overhead on Python 3.13.
from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_optional_float(value: object) -> float | None:
    """Parse an optional float from env; empty or ``none`` → ``None``.

    Lets operators disable ``SEMANTIC_SEARCH_MAX_DISTANCE`` by leaving the
    variable unset, blank, or set to ``none`` (as documented in .env.example).
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped == "" or stripped == "none":
            return None
    parsed = float(value)  # type: ignore[arg-type]
    if parsed < 0:
        raise ValueError("must be >= 0")
    return parsed


def _split_csv(value: object) -> list[str]:
    """Split a comma-separated string into a stripped list; passthrough for lists.

    Used by the ``CommaList`` type alias below so that env vars like
    ``TRUSTED_HOSTS=host1,host2`` are automatically parsed into
    ``["host1", "host2"]`` by pydantic-settings.

    Why a custom validator instead of pydantic's built-in list parsing?
    Because pydantic-settings would try to JSON-decode the string (expecting
    ``["a","b"]``), but env vars are typically written as ``a,b``.
    """
    if isinstance(value, str):
        # Split on commas, strip whitespace, and drop empty segments.
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        # Already a list (e.g. from a default value) — just normalise items.
        return [str(item).strip() for item in value if str(item).strip()]
    return []  # type: ignore[unreachable]


# Custom pydantic type: a list[str] that is populated from a single
# comma-separated environment variable string.
#   - NoDecode: tells pydantic-settings to pass the raw string through
#     without its own JSON parsing (which would fail on "host1,host2").
#   - BeforeValidator: applies _split_csv BEFORE pydantic's own validation,
#     converting the raw string into a list that pydantic can then validate.
CommaList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    # pydantic-settings configuration:
    #   - env_file: loads a .env file for local development (production uses
    #     real env vars injected by docker compose from /dev/shm/.env)
    #   - extra="ignore": silently ignores env vars not declared here (e.g.
    #     AWS_* checksum vars used by aiobotocore but not by this config)
    #   - case_sensitive=True: env var names must match field names exactly
    #     (all fields use UPPER_SNAKE_CASE to follow the env var convention)
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── Auth ─────────────────────────────────────────────────────────────
    # API_TOKEN: privileged secret for write endpoints (POST/DELETE /reviews)
    # and for GET /reviews when the request arrives via the public Nginx
    # gateway. Checked via the API-Token header.
    API_TOKEN: str
    # GUEST_API_TOKEN: comma-separated read-only tokens for external clients
    # calling GET /reviews through https://bankiru.uva-advanced.ru (Nginx
    # sets X-Bankiru-Gateway). Guests cannot POST/DELETE. Empty → only
    # API_TOKEN is accepted on the gateway GET path.
    GUEST_API_TOKEN: CommaList = []
    # LOGFIRE_TOKEN: optional Logfire write token for observability. When None,
    # logfire.configure() falls back to anonymous/local mode.
    LOGFIRE_TOKEN: str | None = None

    @property
    def guest_api_tokens(self) -> frozenset[str]:
        """Guest tokens as a frozenset (empty when GUEST_API_TOKEN is unset)."""
        return frozenset(self.GUEST_API_TOKEN)

    # ── Database ─────────────────────────────────────────────────────────
    # Full SQLAlchemy async connection URL for the external Postgres instance.
    # Uses the psycopg async driver: "postgresql+psycopg://user:pass@host/db"
    # Consumed by db.py to create the async engine.
    POSTGRES_URL: str

    # ── Object storage (S3-compatible) ───────────────────────────────────
    # Credentials and endpoint for an S3-compatible object store (Cloud.ru OBS).
    # Used by the API to upload exported review files and daily Parquet backups,
    # and to generate pre-signed download URLs for the UI.
    OBS_BUCKET: str
    OBS_ACCESS_KEY: str
    OBS_SECRET_KEY: str
    OBS_REGION: str
    OBS_ENDPOINT: str
    # S3 key prefix for daily Parquet backups (e.g. "bankiru-reviews/bankiru-reviews-2025-01-15.parquet")
    OBS_BACKUP_PREFIX: str = "bankiru-reviews"

    # ── API service ──────────────────────────────────────────────────────
    # Port the FastAPI server listens on. Also referenced in docker-compose.yml
    # port mapping and in the default endpoint URLs below.
    API_PORT: int = 1706

    # ── Parser service ───────────────────────────────────────────────────
    # Internal compose-network URL where the parser POSTs collected reviews.
    CREATE_REVIEWS_ENDPOINT: str = "http://api:1706/reviews"
    # APScheduler cron schedule: hour and minute (in PARSER_TIMEZONE) when the
    # daily crawl fires. Default: 00:05 Moscow time (shortly after midnight,
    # so yesterday's reviews are available on banki.ru).
    PARSER_CRON_HOUR: int = 0
    PARSER_CRON_MINUTE: int = 5
    PARSER_TIMEZONE: str = "Europe/Moscow"

    # How many past calendar days to collect on each run (1 = yesterday only).
    PARSER_DAYS: int = 1

    # Parser request pacing — sequential, one HTTP request at a time with
    # randomised sleep between requests to avoid triggering banki.ru's
    # rate-limiting / anti-bot defences.
    PARSER_SLEEP_MIN: float = 10.0   # minimum sleep between requests (seconds)
    PARSER_SLEEP_MAX: float = 20.0   # maximum sleep between requests (seconds)
    PARSER_CONNECT_TIMEOUT: float = 5.0   # TCP connect timeout per request
    PARSER_READ_TIMEOUT: float = 15.0     # read timeout per request
    PARSER_MAX_RETRIES: int = 5           # max retries on transient failures
    # Maximum pause (seconds) when the parser detects a ban/block response
    # from banki.ru before retrying.
    PARSER_BAN_PAUSE_MAX: float = 300.0

    # ── Summarization ────────────────────────────────────────────────────
    # OpenAI-compatible API credentials for the LLM summarization pipeline.
    # Points to Cloud.ru Foundation Models by default, but any OpenAI-compatible
    # endpoint works (the pydantic-ai library handles the actual API calls).
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://foundation-models.api.cloud.ru/v1"
    # Default model used for map-reduce summarization of review batches.
    DEFAULT_CLOUD_MODEL: str = "anthropic/claude-sonnet-4.6"
    # Maximum output tokens the model is allowed to generate per call.
    OUTPUT_TOKENS_LIMIT: int = 50000

    # Map-reduce summarizer tuning — these control how large review batches
    # are split into chunks (map phase) and then merged (reduce phase).
    DEFAULT_MODEL_CONTEXT: int = 200000          # assumed model context window (tokens)
    SUMMARIZER_MAP_CONCURRENCY: int = 4          # parallel map calls
    SUMMARIZER_SAFETY_MARGIN_TOKENS: int = 512   # reserved tokens for prompt overhead
    SUMMARIZER_MAX_PASSES: int = 4               # max reduce iterations before stopping

    # ── Embeddings (Cloud.ru Foundation Models, OpenAI-compatible) ───────
    # Settings for generating vector embeddings of review texts, used for
    # semantic search (the "Semantic search" field in the UI). The embedder service
    # and the API's GET /reviews?keywords=... endpoint both use these.
    EMBEDDINGS_MODEL: str = "BAAI/bge-m3"
    # Separate base URL and API key for the embeddings endpoint. When None,
    # they fall back to OPENAI_BASE_URL / OPENAI_API_KEY respectively.
    # This allows using a different provider or key for embeddings vs summaries.
    EMBEDDINGS_BASE_URL: str | None = None       # falls back to OPENAI_BASE_URL
    EMBEDDINGS_API_KEY: str | None = None         # falls back to OPENAI_API_KEY
    # Dimensionality of the embedding vectors — must match the pgvector
    # column definition in models.py (Vector(1024)).
    EMBEDDINGS_DIMENSIONS: int = 1024
    EMBEDDINGS_BATCH_SIZE: int = 50               # texts per API call
    # Number of DB rows fetched per backfill iteration. Each iteration makes
    # ceil(EMBEDDINGS_BACKFILL_BATCH / EMBEDDINGS_BATCH_SIZE) API calls.
    EMBEDDINGS_BACKFILL_BATCH: int = 500          # DB rows per backfill iteration
    # Maximum number of reviews returned when the user provides a semantic
    # search query. Caps the result set to keep response times fast.
    SEMANTIC_SEARCH_LIMIT: int = 200
    # pgvector HNSW recall knob at query time (default in pgvector is 40).
    SEMANTIC_SEARCH_EF_SEARCH: int = 100
    # Cosine distance ceiling for semantic results; None disables the floor.
    SEMANTIC_SEARCH_MAX_DISTANCE: Annotated[
        float | None, BeforeValidator(_parse_optional_float)
    ] = 0.55

    # ── UI service ───────────────────────────────────────────────────────
    # Port the Gradio UI server listens on. Bound to 127.0.0.1 on the host
    # (not 0.0.0.0) — public access goes through the host's Nginx reverse proxy.
    UI_PORT: int = 17060
    # `*` trusts X-Forwarded-* from any upstream. The container is bound to
    # 127.0.0.1 on the host so this is safe in the default deployment.
    # Tighten only if UI_PORT is published to the public internet directly.
    TRUSTED_HOSTS: CommaList = ["*"]
    # Secret key for Starlette's SessionMiddleware (signs session cookies).
    # Required in production for OIDC login state persistence across requests.
    SESSION_MIDDLEWARE_SECRET: str | None = None
    # Authentik OIDC settings for the UI login flow.
    # The UI uses Authlib to implement the Authorization Code flow.
    OIDC_CLIENT_ID: str | None = None
    OIDC_CLIENT_SECRET: str | None = None
    OIDC_DISCOVERY_URL: str = (
        "https://uva-advanced.ru/application/o/bankiru/"
        ".well-known/openid-configuration"
    )
    # Must exactly match the Redirect URI registered in Authentik's OAuth2 provider.
    # When None, the UI falls back to `request.url_for(...)` (acceptable for local dev).
    OIDC_REDIRECT_URI: str | None = None
    # Must exactly match one of Authentik's "Post Logout Redirect URIs".
    # When None, RP-initiated logout falls back to a local session clear + redirect to "/".
    OIDC_POST_LOGOUT_URI: str | None = None
    # Internal compose-network URL where the UI fetches reviews from the API.
    GET_REVIEWS_URL: str = "http://api:1706/reviews"


# Singleton accessor: lru_cache(maxsize=1) ensures the Settings object is
# created exactly once per process. Every service (api, parser, ui, embedder)
# calls get_settings() to obtain the shared configuration instance.
# The `type: ignore[call-arg]` suppresses a mypy false positive — pydantic-settings
# populates all required fields from environment variables at runtime.
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
