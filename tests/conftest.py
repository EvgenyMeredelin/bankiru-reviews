"""Shared fixtures for the test suite.

The tests exercise ``GET /reviews`` end-to-end through the ASGI app but with
no Postgres and no S3: the ``get_async_session`` / ``get_async_client``
dependencies are replaced by the fakes below through
``app.dependency_overrides``.

Two import-time details matter and are handled at the top of this module:

  * ``Settings`` has required fields — they must exist in ``os.environ``
    before anything imports ``bankiru.config``.
  * ``OPENAI_API_KEY`` is deliberately empty so that importing
    ``bankiru.ui.blocks`` (which calls ``list_foundation_models()`` while
    building the Blocks) takes the offline fallback branch instead of
    reaching out to Cloud.ru.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# ── Environment (must precede any bankiru import) ────────────────────────────
ADMIN_TOKEN = "test-admin-token"
# Two guest tokens on purpose: GUEST_API_TOKEN is owner:token pairs and
# the second entry must be accepted just like the first.
GUEST_TOKEN = "test-guest-token"
GUEST_TOKEN_SECOND = "test-guest-second"

os.environ.update(
    {
        "API_TOKEN": ADMIN_TOKEN,
        "GUEST_API_TOKEN": (
            f"alice@example.org:{GUEST_TOKEN},"
            f"bob@example.org:{GUEST_TOKEN_SECOND}"
        ),
        "POSTGRES_URL": "postgresql+psycopg://test:test@localhost/test",
        "OBS_BUCKET": "test-bucket",
        "OBS_ACCESS_KEY": "test-access-key",
        "OBS_SECRET_KEY": "test-secret-key",
        "OBS_REGION": "ru-central-1",
        "OBS_ENDPOINT": "https://obs.test.invalid",
        # Empty on purpose — keeps the UI import offline (see module docstring).
        "OPENAI_API_KEY": "",
    }
)

import logfire  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import Select  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from bankiru.api import routes  # noqa: E402
from bankiru.api.botocore_client import get_async_client  # noqa: E402
from bankiru.api.deps import GATEWAY_HEADER, GATEWAY_HEADER_VALUE  # noqa: E402
from bankiru.api.routes import router  # noqa: E402
from bankiru.config import get_settings  # noqa: E402
from bankiru.db import get_async_session  # noqa: E402
from bankiru.models import Review  # noqa: E402

# Keep spans local: the router is instrumented and would otherwise warn on
# every test about a missing Logfire token.
logfire.configure(send_to_logfire=False)


# ── Request headers ──────────────────────────────────────────────────────────
def gateway(token: str | None = GUEST_TOKEN) -> dict[str, str]:
    """Headers of a request arriving through the public Nginx gateway.

    Nginx overwrites ``X-Bankiru-Gateway`` itself, so its presence is the only
    thing that tells a public request from an internal one. ``token=None``
    builds the tokenless request that must be refused.
    """
    headers = {GATEWAY_HEADER: GATEWAY_HEADER_VALUE}
    if token is not None:
        headers["API-Token"] = token
    return headers


# ── Settings ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def fresh_settings():
    """Drop the cached ``Settings`` around every test.

    ``get_settings`` is ``lru_cache(maxsize=1)``, so a test that patches the
    environment or a setting would otherwise be served the instance an earlier
    test built — or leak its own change into the next one.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Fake database session ────────────────────────────────────────────────────
class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeResult:
    """Mimics the slice of ``Result`` the handler uses."""

    def __init__(self, rows: list[Any] | None = None, one: Any = None) -> None:
        self._rows = rows or []
        self._one = one

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)

    def one(self) -> Any:
        return self._one


class FakeSession:
    """Stand-in for ``AsyncSession`` that answers the two queries the handler
    makes and records the SQL it was asked to run.

    The bounds query and the main query are told apart by the *structure* of
    the statement rather than by its text: ``select(Review)`` reports ``Review``
    itself as the first column expression, while
    ``select(func.min(...), func.max(...))`` reports a function. Statements
    that are not ``Select`` at all (the handler issues
    ``text("SET LOCAL hnsw.ef_search = ...")`` on the semantic-search path)
    get an empty result instead of an error.
    """

    def __init__(
        self,
        bounds: tuple[datetime | date | None, datetime | date | None] = (None, None),
        rows: list[Any] | None = None,
    ) -> None:
        self.bounds = bounds
        self.rows = rows or []
        self.statements: list[Any] = []
        self.sql: list[str] = []
        self.bounds_queries = 0
        self.main_queries = 0
        # ``SET LOCAL hnsw.ef_search`` — issued on the semantic path only.
        self.session_settings: list[str] = []

    def _record(self, statement: Any) -> None:
        self.statements.append(statement)
        try:
            self.sql.append(
                str(
                    statement.compile(
                        dialect=postgresql.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                )
            )
        except Exception:  # pragma: no cover - only for exotic statements
            self.sql.append(str(statement))

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        self._record(statement)
        if not isinstance(statement, Select):
            self.session_settings.append(self.sql[-1])
            return _FakeResult()
        if statement.column_descriptions[0]["expr"] is Review:
            self.main_queries += 1
            return _FakeResult(rows=self.rows)
        self.bounds_queries += 1
        return _FakeResult(one=self.bounds)

    @property
    def bounds_sql(self) -> str:
        """SQL of the bounds query — always the first statement when it ran."""
        assert self.bounds_queries, "the bounds query never ran"
        return self.sql[0]

    @property
    def main_sql(self) -> str:
        """SQL of the main SELECT (the last statement on every query path)."""
        assert self.main_queries, "the main query never ran"
        return self.sql[-1]


# ── Fake S3 client ───────────────────────────────────────────────────────────
class FakeBotoClient:
    """Records uploads and hands back a deterministic pre-signed URL."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.presigns: list[dict[str, Any]] = []

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.uploads.append(kwargs)
        return {}

    async def generate_presigned_url(self, **kwargs: Any) -> str:
        self.presigns.append(kwargs)
        key = kwargs.get("Params", {}).get("Key", "export")
        return f"https://obs.test.invalid/{key}"


class BrokenBotoClient(FakeBotoClient):
    """Fails one of the two S3 calls, so each can be checked on its own."""

    def __init__(self, fail: str = "put_object") -> None:
        super().__init__()
        self.fail = fail

    @staticmethod
    def _error(operation: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "bucket unavailable"}},
            operation,
        )

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail == "put_object":
            raise self._error("PutObject")
        return await super().put_object(**kwargs)

    async def generate_presigned_url(self, **kwargs: Any) -> str:
        if self.fail == "generate_presigned_url":
            raise self._error("GetObject")
        return await super().generate_presigned_url(**kwargs)


# ── Review rows ──────────────────────────────────────────────────────────────
@dataclass
class FakeReview:
    """Row object for the inline / export paths.

    Must expose attributes rather than dict keys: ``ReviewOut`` is validated
    with ``from_attributes=True`` and the export handler reads the ORM columns
    via ``getattr``.
    """

    id: int = 1
    datePublished: datetime = datetime(2026, 3, 1, 12, 0, 0)
    reviewBody: str = "Очень долгое ожидание в отделении."
    bankName: str = "Тестбанк"
    url: str = "https://www.banki.ru/services/responses/bank/response/1/"
    location: str = "Москва"
    product: str = "Дебетовые карты"


# ── Stubs for the two outbound calls ─────────────────────────────────────────
# BGE-M3 dimensionality; the fake session never looks at the values, but the
# vector has to be the shape pgvector would accept.
EMBEDDING_DIM = 1024

# The real provider error names the internal endpoint, which is exactly what
# must not reach a client. A stub error without a host in it would let the
# leak test pass no matter what the handler does with the message.
PROVIDER_HOST = "foundation-models.api.cloud.ru"
PROVIDER_ERROR = (
    f"Client error '403 Forbidden' for url 'https://{PROVIDER_HOST}/v1/embeddings'"
)


class EmbedderStub:
    """Replaces ``routes.embed_texts`` and records what it was asked to embed."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception | None = None,
        vectors: list[list[float]] | None = None,
    ):
        async def stub(texts, mode):
            self.calls.append((list(texts), mode))
            if error is not None:
                raise error
            if vectors is not None:
                return vectors
            return [[0.0] * EMBEDDING_DIM for _ in texts]

        monkeypatch.setattr(routes, "embed_texts", stub)
        return self


class SummarizerStub:
    """Replaces ``routes.summarize_map_reduce`` and records its inputs."""

    SUMMARY = "summary"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch):
        async def stub(texts, model_name):
            self.calls.append((list(texts), model_name))
            return self.SUMMARY

        monkeypatch.setattr(routes, "summarize_map_reduce", stub)
        return self


@pytest.fixture
def embedder(monkeypatch):
    """A working embedder. ``embedder.calls`` records every query embedded."""
    return EmbedderStub().install(monkeypatch)


@pytest.fixture
def broken_embedder(monkeypatch):
    """An embedder whose provider refuses the call, naming its endpoint."""
    return EmbedderStub().install(monkeypatch, error=RuntimeError(PROVIDER_ERROR))


@pytest.fixture
def empty_embedder(monkeypatch):
    """A provider that answers successfully but with no vector at all."""
    return EmbedderStub().install(monkeypatch, vectors=[])


@pytest.fixture
def summarizer(monkeypatch):
    """A stub summarizer — no test may reach a real LLM."""
    return SummarizerStub().install(monkeypatch)


# ── App / client fixtures ────────────────────────────────────────────────────
def build_app(session: FakeSession, client: FakeBotoClient) -> FastAPI:
    """Minimal app: the router only, without ``create_app``'s lifespan.

    ``create_app`` bootstraps the database schema and the HNSW index on
    startup, which the tests neither need nor can provide.
    """
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_async_session] = lambda: session
    app.dependency_overrides[get_async_client] = lambda: client
    return app


@pytest.fixture
def api():
    """Factory: ``api(bounds=..., rows=...)`` → client, session, S3 client.

    The caller uses the client inside an ``async with`` block, so the factory
    returns the unopened client together with the fakes it talks to.
    """

    def factory(
        bounds: tuple[datetime | date | None, datetime | date | None] = (None, None),
        rows: list[Any] | None = None,
        boto: FakeBotoClient | None = None,
        raise_app_exceptions: bool = True,
    ) -> tuple[AsyncClient, FakeSession, FakeBotoClient]:
        session = FakeSession(bounds=bounds, rows=rows)
        boto = boto if boto is not None else FakeBotoClient()
        app = build_app(session, boto)
        client = AsyncClient(
            # raise_app_exceptions=False turns an unhandled exception into the
            # 500 a deployed client would see, instead of re-raising it here.
            transport=ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://test",
        )
        return client, session, boto

    return factory
