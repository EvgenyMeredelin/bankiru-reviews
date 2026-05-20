"""Application configuration.

All environment variables are declared here. Values are populated from the
process environment, which in production is fed by `scripts/start.sh`
(Infisical export -> /dev/shm/bankiru-reviews-secrets/.env -> docker compose
`env_file`). Locally, just copy `.env.example` to `.env` and `uv run`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> list[str]:
    """Split a comma-separated string into a stripped list; passthrough for lists."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []  # type: ignore[unreachable]


CommaList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Auth
    API_TOKEN: str
    LOGFIRE_TOKEN: str | None = None

    # Database
    POSTGRES_URL: str

    # Object storage (S3-compatible)
    OBS_BUCKET: str
    OBS_ACCESS_KEY: str
    OBS_SECRET_KEY: str
    OBS_REGION: str
    OBS_ENDPOINT: str

    # API
    API_PORT: int = 1706

    # Parser
    CREATE_REVIEWS_ENDPOINT: str = "http://api:1706/reviews"
    PARSER_CRON_HOUR: int = 0
    PARSER_CRON_MINUTE: int = 5
    PARSER_TIMEZONE: str = "Europe/Moscow"

    PARSER_DAYS: int = 1

    # Parser request pacing — one request at a time, randomised sleep
    PARSER_SLEEP_MIN: float = 10.0
    PARSER_SLEEP_MAX: float = 20.0
    PARSER_CONNECT_TIMEOUT: float = 5.0
    PARSER_READ_TIMEOUT: float = 15.0
    PARSER_MAX_RETRIES: int = 5
    PARSER_BAN_PAUSE_MAX: float = 300.0

    # Summarization
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://foundation-models.api.cloud.ru/v1"
    DEFAULT_CLOUD_MODEL: str = "anthropic/claude-sonnet-4.6"
    OUTPUT_TOKENS_LIMIT: int = 50000

    # Map-reduce summarizer tuning
    DEFAULT_MODEL_CONTEXT: int = 200000
    SUMMARIZER_MAP_CONCURRENCY: int = 4
    SUMMARIZER_SAFETY_MARGIN_TOKENS: int = 512
    SUMMARIZER_MAX_PASSES: int = 4

    # UI service
    UI_PORT: int = 17060
    # `*` trusts X-Forwarded-* from any upstream. The container is bound to
    # 127.0.0.1 on the host so this is safe in the default deployment.
    # Tighten only if UI_PORT is published to the public internet directly.
    TRUSTED_HOSTS: CommaList = ["*"]
    SESSION_MIDDLEWARE_SECRET: str | None = None
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
    GET_REVIEWS_URL: str = "http://api:1706/reviews"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
