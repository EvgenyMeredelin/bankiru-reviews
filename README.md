<!-- bankiru-reviews README — explicit <a id> anchors for TOC; section headers use · [↑](#toc). -->
<p align="center">
  <img src="assets/bankiru-logo-white.svg" alt="banki.ru logo" width="500">
</p>

<h1 align="center">bankiru-reviews</h1>

<p align="center">
  Dockerized stack that collects negative customer reviews from
  <a href="https://www.banki.ru">banki.ru</a>, stores them in Postgres,
  and serves filtered, LLM-summarized exports through a Gradio web UI
  gated by <a href="https://goauthentik.io/">Authentik</a> OIDC, with
  all secrets pulled at start-up from a self-hosted
  <a href="https://infisical.com/">Infisical</a> instance into tmpfs.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/banki.ru-reviews-00a859?style=flat-square" alt="banki-ru">
  <img src="https://img.shields.io/badge/cloud.ru-Advanced-1a73e8?style=flat-square" alt="cloud-ru">
  <img src="https://img.shields.io/badge/gradio-UI-f97316?style=flat-square&logo=gradio&logoColor=white" alt="gradio">
  <img src="https://img.shields.io/badge/fastapi-API-009688?style=flat-square&logo=fastapi&logoColor=white" alt="fastapi">
  <img src="https://img.shields.io/badge/apscheduler-cron-4051b5?style=flat-square" alt="apscheduler">
</p>

---

<a id="toc"></a>

## Table of contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Stack overview](#stack-overview)
- [Data model](#data-model)
  - [Embeddings table: `bankiru.review_embeddings`](#embeddings-table-bankiru-review-embeddings)
- [API reference](#api-reference)
  - [Public HTTPS API (guest tokens)](#public-https-api-guest-tokens)
  - [`GET /healthz` — health probe](#get-healthz--health-probe)
  - [`POST /reviews` — insert reviews](#post-reviews--insert-reviews)
  - [`GET /reviews` — filter, export / inline, and optional summarize](#get-reviews--filter-export-and-summarize)
  - [`DELETE /reviews` — delete by ID](#delete-reviews--delete-by-id)
  - [`DELETE /reviews/by-date` — delete by date range](#delete-reviewsby-date--delete-by-date-range)
  - [`DELETE /reviews/duplicates` — deduplicate the table](#delete-reviewsduplicates--deduplicate-the-table)
  - [`GET /` — redirect to docs](#get--redirect-to-docs)
- [Output format handlers](#output-format-handlers)
- [Parser — crawl mechanics](#parser--crawl-mechanics)
- [Parser — request pacing and retry](#parser--request-pacing-and-retry)
  - [How `PARSER_*` timing parameters interact](#how-parser_-timing-parameters-interact)
  - [Putting it all together — a concrete example](#putting-it-all-together-a-concrete-example)
  - [POST batch delivery](#post-batch-delivery)
  - [Other implementation details](#other-implementation-details)
- [Summarization — map-reduce pipeline](#summarization--map-reduce-pipeline)
- [Semantic search](#semantic-search)
  - [Design note: LLM failure vs embedder failure](#design-note-llm-failure-vs-embedder-failure)
  - [Embedding pipeline](#embedding-pipeline)
  - [Embedder CLI](#embedder-cli)
  - [Pre-deploy checklist](#pre-deploy-checklist)
- [UI and authentication](#ui-and-authentication)
  - [Registering the OIDC client in Authentik](#registering-the-oidc-client-in-authentik)
- [Security hardening](#security-hardening)
- [Repository layout](#repository-layout)
- [Secrets — Infisical + tmpfs](#secrets--infisical--tmpfs)
- [Configuration reference](#configuration-reference)
  - [Required (no defaults)](#required-no-defaults)
  - [Optional (shown with defaults)](#optional-shown-with-defaults)
- [Quick start](#quick-start)
  - [Production (with Infisical)](#production-with-infisical)
  - [`start.sh` flags](#startsh-flags)
  - [Local development (without Docker)](#local-development-without-docker)
  - [Tests](#tests)
- [Day-2 operations](#day-2-operations)
  - [Count reviews per day (with url dedup)](#count-reviews-per-day-with-url-dedup)
  - [Export reviews to CSV on the host (no summarization)](#export-reviews-to-csv-on-the-host-no-summarization)
  - [Changing the daily crawl schedule](#changing-the-daily-crawl-schedule)
    - [Option A — SIGHUP (safe while a crawl is running)](#option-a--sighup-safe-while-a-crawl-is-running)
    - [Option B — Container restart (kills any running crawl)](#option-b--container-restart-kills-any-running-crawl)
- [Описание проекта (на русском)](#описание-проекта-на-русском)
  - [Назначение](#назначение)
  - [Парсер и источник данных](#парсер-и-источник-данных)
  - [Хранение](#хранение)
  - [API (сервис `api`, порт по умолчанию 1706)](#api-сервис-api-порт-по-умолчанию-1706)
  - [Семантический поиск](#семантический-поиск)
  - [Эмбеддинги](#эмбеддинги)
  - [Суммаризация](#суммаризация)
  - [Заметка: сбой LLM и сбой эмбеддера](#заметка-сбой-llm-и-сбой-эмбеддера)
  - [Веб-интерфейс (сервис `ui`)](#веб-интерфейс-сервис-ui)
  - [Безопасность UI](#безопасность-ui)
  - [Инфраструктура и секреты](#инфраструктура-и-секреты)
  - [Наблюдаемость и расширяемость](#наблюдаемость-и-расширяемость)
  - [Эксплуатация](#эксплуатация)
- [Краткое описание (на русском)](#краткое-описание-на-русском)
  - [Что делает система](#что-делает-система)
  - [Архитектура](#архитектура)
  - [Ключевые возможности](#ключевые-возможности)
  - [Технологии](#технологии)
- [References](#references)

---

<a id="architecture-at-a-glance"></a>
## Architecture at a glance · [↑](#toc)

```mermaid
flowchart TD
    user(["User browser"]) -->|"HTTPS"| nginx["Nginx on host\n(TLS termination)"]
    guest(["API client"]) -->|"HTTPS + API-Token"| nginx

    subgraph stack [Docker Compose stack: bankiru-reviews]
      direction TB
      ui["ui\nGradio + OIDC"]
      api["api\nFastAPI"]
      parser["parser\nAPScheduler"]
      ui -->|"GET /reviews\nno token"| api
      parser -->|"POST /reviews\nAPI_TOKEN"| api
    end

    nginx -->|"127.0.0.1:17060"| ui
    nginx -->|"127.0.0.1:1706\n/reviews /docs /healthz"| api
    ui <-->|"OIDC"| authentik(("Authentik"))
    api -->|"SQLAlchemy"| postgres[("Postgres\nreviews + embeddings")]
    api -->|"put_object\npresigned_url"| s3[("S3 / OBS")]
    parser -->|"sequential HTTP/1.1\nbackoff + retry"| bankiruRu[("banki.ru")]
    infisical(("Infisical")) -.->|"secrets via tmpfs\n/dev/shm/...env"| stack
```

**Request path for a UI query:**

1. Browser → Nginx (TLS) → `ui` service (`127.0.0.1:17060`).
2. Gradio calls `GET /reviews` on the `api` service (internal compose network, no `API-Token`). The Format dropdown defaults to `parquet`, and `summarize` is sent explicitly (`true` only when a real **Summary model** is selected). Clearing Format omits `outputFormat`, so the API answers inline.
3. `api` queries Postgres, optionally runs the LLM when `summarize=true`, then either returns rows inline or uploads the export to S3 and returns a pre-signed URL — JSON body `{url, comment, …}` or `{reviews, comment, …}`. An omitted date bound always resolves to the matching bound of the stored data (omitted `startDate` = earliest `datePublished` in DB, omitted `endDate` = the latest one), whatever `summarize` is; the resolved range drives the SQL filter and is echoed back in the response. An inverted effective range returns **400**, and with `summarize=true` a span longer than three calendar months returns **400** before the main select/LLM.
4. On success, UI renders the summary in the Markdown panel (empty when `<no summary>`) and may show an info toast **"Download your file"** only when an export URL is present. On API/network failure the UI raises a Gradio **error toast** with the API `detail` (not Summary text), clears the signed URL and Summary, and does not show the download info toast. "Download reviews" opens the pre-signed URL in a new tab (**browser → OBS**, no server round-trip). "Download summary" saves the Summary Markdown as a local `.md` (client-side Blob).

**Request path for an external API client:** Browser/script → Nginx (TLS) → `api` (`127.0.0.1:1706`) with `API-Token` (guest or admin). Omitting `outputFormat` returns reviews **inline** in `reviews`. Omitting `summarize` is always **`false`** (gateway, localhost, or UI). Omitting a date bound resolves it to the earliest / latest stored review date, which is also what the response echoes back — a breaking change, since those fields are no longer `null`. Unknown query params → **422**. An inverted effective range → **400** (previously an empty 200); `summarize=true` with an effective date span &gt; 3 calendar months → **400** with a fixed `detail` string (same for Nginx, localhost, and Gradio); a `keywords` query the embedding provider cannot embed → **503** (previously an empty 200). Full guide: [`docs/bankiru-reviews-public-api.md`](docs/bankiru-reviews-public-api.md).

---

<a id="stack-overview"></a>
## Stack overview · [↑](#toc)

| Service | Image | Purpose |
|---------|-------|---------|
| `api` | Built from `./Dockerfile` (`python:3.13-slim`, uv) | FastAPI. Handles `POST /reviews` (insert + inline embeddings), `GET /reviews` (filter; inline JSON or S3 export; optional summarize), `DELETE /reviews` (by ID), `DELETE /reviews/by-date` (by date range), `DELETE /reviews/duplicates`. Bound to `127.0.0.1:API_PORT` on the host; public HTTPS via Nginx. On startup: `create_all_tables()` (blocks while building a missing HNSW index), then a background embedding backfill. Each POST merges rows into per-`datePublished` Parquet files under `bankiru-reviews/` (new inserts and all-skipped retries; multi-day batches split by review date). DELETE does not prune OBS. |
| `parser` | Same image, `command: python -m bankiru.parser` | APScheduler cron job. Crawls banki.ru once daily, collects negative reviews for the previous `PARSER_DAYS` days, and POSTs the deduplicated batch to the `api`. |
| `ui` | Same image, `command: python -m bankiru.ui` | FastAPI + Gradio. OIDC-gated via Authentik (Authlib). Calls the `api` over the compose network **without** `API-Token`. Bound to `127.0.0.1:17060` on the host; public access goes through Nginx. |
| External: Postgres | (managed elsewhere) | Sole persistent data store. Two tables: `bankiru.reviews` and `bankiru.review_embeddings`. Schema is bootstrapped at `api` startup via `create_all_tables()` (pgvector extension, ORM tables, B-tree indexes, HNSW index via `ensure_hnsw_index()` — idempotent when valid; building a missing HNSW index blocks readiness). |
| External: S3 / OBS | (managed elsewhere) | Stores named export files (pre-signed 1-hour URLs) and daily Parquet backups (`bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet`). |
| External: Authentik | `https://uva-advanced.ru` | OIDC identity provider for the UI login flow. |
| External: Infisical | `https://infisical.uva-advanced.ru` | Secrets store; `scripts/start.sh` pulls secrets into `/dev/shm` at boot. |

**Target environment:** Linux x86_64, Docker Engine, `docker compose` v2, Nginx on the host.

---

<a id="data-model"></a>
## Data model · [↑](#toc)

Two tables in PostgreSQL schema `bankiru`: `reviews` and `review_embeddings`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK | Auto-increment surrogate key. |
| `datePublished` | `DATETIME` | Publication timestamp extracted from banki.ru's JSON-LD structured data. Format: `YYYY-MM-DD HH:MM:SS`. |
| `reviewBody` | `TEXT` | Cleaned review text. HTML tags stripped (double pass — banki.ru sometimes HTML-encodes tags inside the body), emojis replaced via the `emoji` library, leading/trailing whitespace removed. |
| `bankName` | `TEXT` | Bank name from the `itemReviewed.name` field in JSON-LD. |
| `url` | `TEXT` | Canonical URL of the review's detail page on banki.ru. The same URL may appear in multiple rows (one per product tag); not unique. |
| `location` | `TEXT` | Author's city, extracted from the detail page (`<span class="l3a372298">…</span>`). Empty string if the element is absent or the detail page is unreachable. |
| `product` | `TEXT` | Human-readable banking product label (e.g. `Кредитная карта`, `Ипотека`). Mapped from the banki.ru product slug by `parser/settings.py`. |

**Indexes** (created automatically by `create_all_tables()` at API startup):

| Index | Columns / expression | Purpose |
|-------|---------------------|---------|
| PK | `id` | Primary key. |
| `ix_reviews_datePublished` | `datePublished` | Date-range filters in `GET /reviews` and `DELETE /reviews/by-date`. |
| `ix_reviews_bankName` | `bankName` | Bank name filter in `GET /reviews`. |
| `ix_reviews_product` | `product` | Product filter in `GET /reviews`. |
| `ix_reviews_location` | `location` | Location prefix filter in `GET /reviews`. |

**Storage model:** one row per applied product tag. The same review page URL may appear multiple times with different `product` values — there is intentionally **no UNIQUE on `url`**. Insert idempotency (retried `POST /reviews`) keys on `(url, product)`.

**Cleanup deduplication:** `(reviewBody, product)` — compared via `md5(reviewBody)` to keep the hash table small (32-byte strings vs full review bodies). Postgres uses `HashAggregate` for the full-table scan — no dedicated index needed. MD5 collisions on natural-language texts are negligible. The crawler also deduplicates in-memory by `(reviewBody, product)` before POSTing. The `DELETE /reviews/duplicates` endpoint deduplicates the database table in place (keeps the row with the lowest `id`).

<a id="embeddings-table-bankiru-review-embeddings"></a>
### Embeddings table: `bankiru.review_embeddings` · [↑](#toc)

| Column | Type | Description |
|--------|------|-------------|
| `review_id` | `INTEGER` PK, FK → `reviews.id` | Foreign key with `ON DELETE CASCADE`. |
| `embedding` | `vector(1024)` | BAAI/bge-m3 embedding of enriched passage text (`bankName \| product \| location` + `reviewBody`). |

**Index:**

| Index | Type | Purpose |
|-------|------|---------|
| PK | B-tree | Primary key on `review_id`. |
| `ix_review_embeddings_hnsw` | HNSW (`vector_cosine_ops`, m=16, ef_construction=200) | Approximate nearest neighbor search for semantic queries. |

---

<a id="api-reference"></a>
## API reference · [↑](#toc)

Base URL (in compose): `http://api:1706`. On the host loopback: `http://127.0.0.1:1706` (not published on `0.0.0.0`). Public HTTPS: `https://bankiru.uva-advanced.ru` (Nginx → api for `/reviews`, `/docs`, `/healthz`, …).

<a id="public-https-api-guest-tokens"></a>
### Public HTTPS API (guest tokens) · [↑](#toc)

External clients use **`https://bankiru.uva-advanced.ru`**. Full Russian guide: [`docs/bankiru-reviews-public-api.md`](docs/bankiru-reviews-public-api.md).

| Path | Auth |
|------|------|
| `GET /reviews` via Nginx | `API-Token` ∈ `GUEST_API_TOKEN` **or** `API_TOKEN` (Nginx sets `X-Bankiru-Gateway`) |
| `GET /reviews` from UI / compose network | No token (no gateway header) |
| `POST` / `DELETE` `/reviews*` | `API-Token` must match **`API_TOKEN` only** (guest tokens → 403) |
| `GET /healthz`, `/docs`, `/redoc`, `/openapi.json` | No token |

Deploy order: recreate `api` (loopback bind + gateway auth) **before** reloading the Nginx conf that proxies `/reviews`.

<a id="get-healthz--health-probe"></a>
### `GET /healthz` — health probe · [↑](#toc)

No auth. Returns `{"status": "ok"}`. Used by Docker's `healthcheck`.

<a id="post-reviews--insert-reviews"></a>
### `POST /reviews` — insert reviews · [↑](#toc)

**Auth:** `API-Token` header (must match `API_TOKEN`; guest tokens rejected).

**Body:** JSON array of Review objects.

```json
[
  {
    "datePublished": "2025-01-15 14:32:00",
    "reviewBody": "Terrible service…",
    "bankName": "Сбербанк",
    "url": "https://www.banki.ru/services/responses/bank/response/123456/",
    "location": "Москва",
    "product": "Кредитная карта"
  }
]
```

Dedupes the request body and skips pairs already stored under `(url, product)` (one page may yield multiple rows — one per product tag; no UNIQUE on `url`). Skipped pairs are **not** updated (re-crawl does not refresh body/location/bank). Inserts the remainder, commits, generates vector embeddings for the new reviews (inline, best-effort), then **merges** the full request payload into per-`datePublished` Parquet backups on S3 (`bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet`). Multi-day batches are split by review date. If PutObject fails after commit, the API returns **503** (reviews stay in Postgres); the parser retries — inserts are skipped and the all-skipped path re-merges into OBS. DELETE endpoints do not prune OBS. Returns `201 Created` with `{"inserted": N, "skipped": M}` (empty `[]` → `{"inserted": 0, "skipped": 0}`).

An empty JSON array (`[]`) is accepted and also returns `201` without touching the database or S3. If embedding generation fails for a batch, the reviews are still saved; missing embeddings are backfilled on the next API restart (see [Semantic search](#semantic-search)).

<a id="get-reviews--filter-export-and-summarize"></a>
### `GET /reviews` — filter, export / inline, and optional summarize · [↑](#toc)

**Auth:** None for internal callers (UI over the compose network). Via the public Nginx gateway: `API-Token` must match `GUEST_API_TOKEN` or `API_TOKEN`.

**Query parameters** (all optional):

| Parameter | Type | Description |
|-----------|------|-------------|
| `startDate` | `YYYYMMDD` or `YYYY-MM-DD` | Include reviews published on or after this date. Hyphens stripped automatically. If omitted: earliest `datePublished` in the table, for every value of `summarize`. |
| `endDate` | `YYYYMMDD` or `YYYY-MM-DD` | Include reviews published on or before this date. If omitted: latest `datePublished` in the table, for every value of `summarize`. |
| `bankName` | repeatable string | Include only these bank names. Multiple values: `?bankName=Сбербанк&bankName=ВТБ`. Exact match. |
| `location` | repeatable string | Include only reviews whose `location` starts with one of the given prefixes. Useful for matching a city when the stored value includes district suffixes. |
| `product` | repeatable string | Exact match on `product`. |
| `keywords` | `string` | Free-text semantic search (UI label: **Semantic search**). When provided, the query is embedded with the BGE-M3 **query** prefix, reviews are ranked by cosine similarity (HNSW via pgvector), optionally filtered by `SEMANTIC_SEARCH_MAX_DISTANCE`, and capped at `SEMANTIC_SEARCH_LIMIT` (default 200). Combinable with all other filters. |
| `outputFormat` | `csv` / `json` / `parquet` / `xlsx` | **If omitted:** return matching rows inline in `reviews` (`url`/`filename` null). **If set:** export to S3 and return a pre-signed `url` (`reviews` null). Breaking change vs older default-`parquet` behaviour. Gradio defaults to `parquet`; clearing the Format dropdown omits this parameter (inline). |
| `summarize` | `bool` | If omitted: **`false`** for every caller (gateway, localhost, Gradio). When `true`, the effective date interval must be ≤ 3 calendar months or the API returns **400** (omitted dates still count — see `startDate` / `endDate`). |
| `cloudModel` | string | Summarization model when `summarize` is true. API default if omitted: `DEFAULT_CLOUD_MODEL`. UI **Summary model** defaults to `<no summary>` (`summarize=false`). |

**Successful response** (export + summarize example):

```json
{
  "startDate": "2025-01-01",
  "endDate": "2025-01-31",
  "bankName": ["Сбербанк"],
  "product": null,
  "location": null,
  "keywords": null,
  "outputFormat": "xlsx",
  "summarize": true,
  "cloudModel": "anthropic/claude-sonnet-4.6",
  "filename": "a1b2c3d4-….xlsx",
  "url": "https://obs.cloud.ru/…?X-Amz-Expires=3600…",
  "comment": "**Summary model:** `anthropic/claude-sonnet-4.6`\n\n## Наиболее острые темы…",
  "reviews": null
}
```

When `outputFormat` is omitted, `reviews` is a list of review objects and `url`/`filename` are `null`. If no reviews match, `reviews`/`url`/`filename` are `null` and `comment` is a "no results" message (even when `summarize` is false). The echoed `startDate` / `endDate` hold the **effective** bounds and are never `null` when the table has data (breaking change). On an empty table an unresolved bound stays `null` (both omitted → both `null`; one given → that side is echoed, the other is `null`). Unknown query params → **422**.

An inverted effective range returns **400** for every value of `summarize` — including `startDate` past the newest review with an omitted `endDate`, which used to yield an empty 200:

```json
{
  "detail": "Empty date range: endDate is earlier than startDate (an omitted bound falls back to the earliest / latest review date stored in the database)."
}
```

With `summarize=true`, an effective interval longer than three calendar months returns **400** before SQL select / LLM — including when `startDate` and/or `endDate` are omitted — with:

```json
{
  "detail": "Summarization is only allowed for date ranges of at most three calendar months. Narrow startDate/endDate (an omitted bound falls back to the earliest / latest review date stored in the database), or set summarize=false."
}
```

A `keywords` query the embedding provider cannot embed — unreachable, erroring, or answering with no vector — returns **503**, where it used to return an empty 200 with the provider's message in `comment`:

```json
{
  "detail": "Semantic search is temporarily unavailable: the query could not be embedded. Retry later, or repeat the request without keywords."
}
```

Public HTTPS details and curl/HTTPie examples: [`docs/bankiru-reviews-public-api.md`](docs/bankiru-reviews-public-api.md).

<a id="delete-reviews--delete-by-id"></a>
### `DELETE /reviews` — delete by ID · [↑](#toc)

**Auth:** `API-Token` header (must match `API_TOKEN`; guest tokens rejected).

**Body:** JSON array of integer IDs, e.g. `[42, 43, 44]`. Deletes the matching rows and commits.

Returns `204 No Content`.

<a id="delete-reviewsby-date--delete-by-date-range"></a>
### `DELETE /reviews/by-date` — delete by date range · [↑](#toc)

**Auth:** `API-Token` header (must match `API_TOKEN`; guest tokens rejected).

**Query parameters** (both required):

| Parameter | Type | Description |
|-----------|------|-------------|
| `startDate` | `YYYY-MM-DD` | Delete reviews published on or after this date (inclusive). |
| `endDate` | `YYYY-MM-DD` | Delete reviews published on or before this date (inclusive). |

Deletes all matching rows and commits.

Returns `204 No Content`.

**Example** — delete reviews for May 17–18:

```bash
curl -s -X DELETE -H "API-Token: $API_TOKEN" \
  "http://127.0.0.1:1706/reviews/by-date?startDate=2026-05-17&endDate=2026-05-18"
```

<a id="delete-reviewsduplicates--deduplicate-the-table"></a>
### `DELETE /reviews/duplicates` — deduplicate the table · [↑](#toc)

**Auth:** `API-Token` header (must match `API_TOKEN`; guest tokens rejected).

Keeps the row with the lowest `id` per `(reviewBody, product)` pair (grouped by `md5(reviewBody)` to keep the hash table small). Deletes everything else. The query uses a CTE to materialise keeper IDs first, then performs an integer-only `NOT IN` delete. Postgres uses `HashAggregate` for the full-table scan — no dedicated index needed. A per-statement timeout of 300 s is set so a slow query surfaces as an error instead of hanging indefinitely.

**Response** (JSON): `{"deleted": <count>}`.

<a id="get--redirect-to-docs"></a>
### `GET /` — redirect to docs · [↑](#toc)

Redirects to `/docs` (Swagger UI). The API service exposes auto-docs; the UI service does not.

---

<a id="output-format-handlers"></a>
## Output format handlers · [↑](#toc)

Handlers live in `src/bankiru/api/handlers.py`. Each format is a class that subclasses `ScalarsHandler` and ends with `Maker`:

| Class | Extension | MIME type |
|-------|-----------|-----------|
| `CSVMaker` | `.csv` | `text/csv` |
| `JSONMaker` | `.json` | `application/json` |
| `ParquetMaker` | `.parquet` | `application/vnd.apache.parquet` |
| `XlsxMaker` | `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |

**Registration is automatic.** `schemas.py` discovers all `*Maker` classes via `inspect.getmembers(handlers)` and builds `available_output_formats = {cls.extension: cls}`. Adding a new format means writing a subclass — no other file needs to change.

**Backup key:** `bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet` — one object per review `datePublished` date; newly inserted rows are merged on POST (multi-day batches split across dates). Named export keys: `<uuid4>.<extension>`.

**XLSX specifics:** Rows are colour-coded by review URL (alternating mint/pink per distinct URL) to visually group rows that belong to the same review. `reviewBody` is excluded from auto-fit. The `datePublished` column format (`YYYY-MM-DD HH:mm:ss`) is stamped directly via openpyxl after writing, because StyleFrame's per-row style merge is unreliable for `date_time_format`.

---

<a id="parser--crawl-mechanics"></a>
## Parser — crawl mechanics · [↑](#toc)

**Products covered:** 24 URL slugs mapping to 23 distinct product labels (12 retail + 11 business). Defined in `parser/settings.py` as a `{slug: label}` dict. Note: banki.ru uses both `corporate` and `legal` slugs for "Обслуживание юридических лиц" — both are crawled and the crawler's in-memory deduplication discards the duplicate bodies.

**Crawl loop (per product):**

1. Fetch the listing page: `GET /services/responses/list/product/{slug}/?page={n}&type=all&rate[]=1&rate[]=2` — the `rate[]=1&rate[]=2` filter restricts to 1-star and 2-star reviews (negatives only).
2. Extract review candidates from the listing HTML using two regexes:
   - `REVIEW_CONTENT_PATTERN` — matches inlined JSON-LD `Review` objects (strips out the fields we don't need: author, rating, postal address).
   - `REVIEW_URL_PATTERN` — matches the href of each review's detail-page link.
   Both iterators are zipped so content and URL stay aligned. If the match counts differ, a warning is logged and **the page's pairs are dropped** (no zip of unequal lists); pagination continues (`any_matched=True`) so a bad page does not stop the product crawl — a signal that banki.ru changed its HTML or a regex needs updating.
3. For each candidate in the date window `[start_date, end_date)`: parse the JSON-LD fragment; **malformed JSON is skipped** (logged, crawl continues). Fetch the detail page, extract the author's city from `<span class="l3a372298">…</span>` (`LOC_PATTERN`), and append the finished record. If the detail page fails, `location` is stored as `""` — the review is never dropped.
4. Stop paginating when the oldest review on the page predates `start_date` (`hit_left_boundary = True`) or the page contains no review markup at all (past the last page). Crucially, the crawler does **not** stop when `candidates` is empty: a page full of today's reviews (all newer than `end_date`) still needs to be paginated through to reach the date window.
5. After all products: deduplicate in-memory on `(reviewBody, product)` via pandas `drop_duplicates`.

**Date window:** `start_date = today(tz) - relativedelta(days=PARSER_DAYS)`, `end_date = today(tz)`, where `tz = ZoneInfo(PARSER_TIMEZONE)`. Both anchored at 00:00:00 in the configured timezone (via `dateutil.utils.today(tz)`), so the window is `[yesterday 00:00:00, today 00:00:00)` in `PARSER_TIMEZONE` time. This ensures the date window matches the cron schedule's timezone regardless of the container's system clock.

**Text cleaning pipeline** (`tools.py`):
1. `BeautifulSoup(...).text` — strip HTML tags.
2. Same again — banki.ru occasionally double-encodes HTML inside review bodies.
3. `emoji.replace_emoji` — remove emoji characters.
4. `str.strip` — trim whitespace.

---

<a id="parser--request-pacing-and-retry"></a>
## Parser — request pacing and retry · [↑](#toc)

The client (`parser/client.py`) is deliberately **fully sequential** — one request at a time — to avoid triggering banki.ru's WAF.

<a id="how-parser_-timing-parameters-interact"></a>
### How `PARSER_*` timing parameters interact · [↑](#toc)

Six `PARSER_*` environment variables control the timing of every HTTP request the parser makes to banki.ru. They fall into three groups — **pacing**, **timeouts**, and **ban recovery** — and together they determine the total time a single request occupies and the average crawl throughput.

```
                        one request cycle
├───── pacing sleep ──────┤├──── TCP connect ────┤├──── response read ────┤
│ uniform(SLEEP_MIN,      ││ ≤ CONNECT_TIMEOUT   ││ ≤ READ_TIMEOUT        │
│         SLEEP_MAX)      ││   (5 s)             ││   (15 s)              │
│   (10 – 20 s)           ││                     ││                       │
```

**① Pacing — `PARSER_SLEEP_MIN` / `PARSER_SLEEP_MAX`**

Before *every* request (including the very first warmup and every retry), the client sleeps a random duration drawn from `uniform(PARSER_SLEEP_MIN, PARSER_SLEEP_MAX)`. This is the primary rate-limiting mechanism.

| Setting | Default | Effect |
|---------|---------|--------|
| `PARSER_SLEEP_MIN` | `10.0` s | Lower bound of the random sleep. |
| `PARSER_SLEEP_MAX` | `20.0` s | Upper bound of the random sleep. |

Average sleep = `(SLEEP_MIN + SLEEP_MAX) / 2` = **15 s** by default. Combined with the network round-trip (~0.5–2 s), this yields an average interval of **~15–17 s between consecutive requests** (~4 req/min).

*Example — aggressive pacing:* `SLEEP_MIN=5, SLEEP_MAX=10` → average sleep 7.5 s → ~8 req/min. Higher risk of WAF bans.

*Example — zero pacing:* `SLEEP_MIN=0, SLEEP_MAX=0` → sleep is 0 s → the only delay is the network round-trip (connect + read ≈ 0.3–2 s). This produces ~30–200 req/min and will almost certainly trigger an immediate WAF ban, sending the client into the ban-recovery loop (see ③ below).

**② Timeouts — `PARSER_CONNECT_TIMEOUT` / `PARSER_READ_TIMEOUT`**

These are the httpx split timeouts applied to every GET request to banki.ru. They determine how long the client waits for the *network* portion of the request before declaring failure.

| Setting | Default | Effect |
|---------|---------|--------|
| `PARSER_CONNECT_TIMEOUT` | `5.0` s | Max time to establish a TCP connection. Kept short to detect WAF bans quickly — a banned IP typically sees the connection hang or reset within 1–2 s. |
| `PARSER_READ_TIMEOUT` | `15.0` s | Max time to receive the full HTTP response body after the connection is established. Listing pages are ~100–500 KB; 15 s is generous for normal conditions. |

A `ConnectTimeout` is classified as a probable WAF ban and triggers unlimited retry with exponential back-off (see ③). A `ReadTimeout` is classified as a transient error and counts toward the `PARSER_MAX_RETRIES` budget.

**③ Ban recovery — `PARSER_BAN_PAUSE_MAX` / `PARSER_MAX_RETRIES`**

| Setting | Default | Effect |
|---------|---------|--------|
| `PARSER_MAX_RETRIES` | `5` | Max non-connect failures (non-200 status, read timeout, etc.) per URL before the request is abandoned (`None`). The crawler stores the review without location data or skips the listing page — data loss is minimised. |
| `PARSER_BAN_PAUSE_MAX` | `300.0` s | Ceiling for the exponential back-off pause during a connect-error streak (probable WAF ban). |

**Connect-error back-off formula:**

```
base  = min(SLEEP_MAX × 2^(streak − 1), BAN_PAUSE_MAX)
delay = min(base × jitter, BAN_PAUSE_MAX)       # jitter ∈ [0.5, 1.5)
```

| Ban streak | Base (defaults) | Delay range |
|------------|-----------------|-------------|
| 1 | min(20 × 1, 300) = 20 s | 10 – 30 s |
| 2 | min(20 × 2, 300) = 40 s | 20 – 60 s |
| 3 | min(20 × 4, 300) = 80 s | 40 – 120 s |
| 4 | min(20 × 8, 300) = 160 s | 80 – 240 s |
| 5 | min(20 × 16, 300) = 300 s | 150 – 300 s |
| 6+ | 300 s (capped) | 150 – 300 s |

The connect-error retry is **unlimited** — the crawl pauses and resumes once the ban lifts, guaranteeing no data loss from transient bans.

<a id="putting-it-all-together-a-concrete-example"></a>
### Putting it all together — a concrete example · [↑](#toc)

With default settings, a single successful request cycle takes:

```
sleep:    uniform(10, 20)  →  ~15 s  (average)
connect:  ~0.1 s           (typical, no ban)
read:     ~0.5 s           (typical listing page)
─────────────────────────────────────────────────
total:    ~15.6 s per request  →  ~4 req/min
```

A daily run with `PARSER_DAYS=1` typically processes ~300–500 reviews across 24 products. Each product requires 1–5 listing pages + 1 detail page per review. For 400 reviews with ~100 listing pages:

```
warmup:          1 request  ×  ~15 s  =    15 s
listing pages: 100 requests ×  ~15 s  = 1 500 s  (25 min)
detail pages:  400 requests ×  ~15 s  = 6 000 s  (100 min)
──────────────────────────────────────────────────────────
total:         501 requests ×  ~15 s  ≈ 2.1 hours
```

Actual run time is 0.5–1.5 hours because many products have only 1–2 listing pages and the sleep distribution is uniform (not always 15 s).

<a id="post-batch-delivery"></a>
### POST batch delivery · [↑](#toc)

After the crawl completes, `runner.py` POSTs the collected reviews to the API (`CREATE_REVIEWS_ENDPOINT`). This POST uses a **separate** httpx client with a flat 600 s timeout (not the crawl client's split timeouts) — long enough to accommodate bulk INSERT, inline embedding generation, and merging inserted rows into per-`datePublished` Parquet backups.

Before the first POST attempt, the runner polls `GET /healthz` (up to 30 × 5 s) so a mid-crawl API restart does not fail the delivery immediately.

The POST retries with exponential back-off capped at 60 s, up to **20 attempts**, for transient failures (network errors, 5xx — including **503** when S3 backup fails after a successful DB commit). After that the error is raised so a permanent failure cannot block the next daily crawl. **Client errors do not retry:** `401`, `403`, `404`, and `422` fail fast so a bad token or malformed payload surfaces immediately. The API skips already-stored `(url, product)` pairs and re-merges the payload into OBS on all-skipped retries, so a retried POST after commit+backup-fail heals S3 without multiplying rows.

<a id="other-implementation-details"></a>
### Other implementation details · [↑](#toc)

- `http2=False` — banki.ru's WAF silently drops TLS handshakes that advertise the `h2` ALPN.
- `max_connections=1, max_keepalive_connections=0` — enforces one connection at a time and no keepalive, matching the original parser's behaviour.
- `User-Agent` and `Accept-Language` are rotated per request from small realistic pools.
- Detail-page requests include a `Referer` header pointing to the listing page they were linked from.

**Warmup:** On `BankiruClient.__aenter__`, the client fetches `GET /` to seed the cookie jar before any product pages. If the warmup transport-errors, `run_once` logs the error and returns early — no point attempting 24 products against an unreachable origin.

**Typical run time:** 0.5–1.5 hours for a normal daily run (depends on review volume).

---

<a id="summarization--map-reduce-pipeline"></a>
## Summarization — map-reduce pipeline · [↑](#toc)

The summarizer (`api/summarizer.py`) handles arbitrarily large *token* budgets: it chunks the corpus to fit the model's context window and recursively reduces partial summaries until the result is a single coherent text. Users never see a "context size exceeded" error. Separately, `GET /reviews` rejects `summarize=true` when the effective date span exceeds three calendar months (**400** before the LLM runs).

```mermaid
flowchart TD
    Texts["unique reviewBody texts"] --> Tokenize["tiktoken cl100k_base"]
    Tokenize --> Check{"fits one call?"}
    Check -- yes --> Final["pydantic_ai Agent\nSYSTEM_PROMPT_FINAL"]
    Check -- no  --> Pack["greedy bin-pack into chunks\n≤ input_budget tokens"]
    Pack --> Map["asyncio.gather (≤ SUMMARIZER_MAP_CONCURRENCY)\nSYSTEM_PROMPT_MAP per chunk"]
    Map --> Summaries["chunk summaries"]
    Summaries --> Check
    Final --> Result["string returned to UI"]
```

**Budget arithmetic** (recalculated each pass):

```
per_call_output = min(OUTPUT_TOKENS_LIMIT, max(256, max_model_len // 4))
input_budget    = max_model_len − system_prompt_tokens
                  − per_call_output − SUMMARIZER_SAFETY_MARGIN_TOKENS
```

The `// 4` cap prevents a small-context model (e.g. 4 k tokens) with a large `OUTPUT_TOKENS_LIMIT` from producing a negative `input_budget`. The `max(256, …)` floor keeps the value sensible for extremely small models.

**System prompts (Russian):**
- `SYSTEM_PROMPT_MAP` — for partial batch passes: extract acute and frequent complaint topics, no global conclusions.
- `SYSTEM_PROMPT_REDUCE` — for merging partial summaries: merge into exactly two `## …` sections.
- `SYSTEM_PROMPT_FINAL` — for the terminal pass (when everything fits one call): produce exactly two `## …` sections (`Наиболее острые темы` / `Наиболее частые темы`).

**Post-processing:** `_strip_wrapper_heading` removes a stray outer `#`/`##` heading (e.g. `## Сводка жалоб`) that some models prepend despite the prompt instruction.

**Model context discovery:** The Cloud.ru `/models` endpoint is queried on demand, results cached for 1 hour. If the endpoint is unreachable, `DEFAULT_MODEL_CONTEXT` is used as a fallback. The same cached data feeds the UI model dropdown.

**Error handling:** `ModelHTTPError` and `UsageLimitExceeded` are caught and returned as strings in `comment` with HTTP **200** (reviews / export URL still accompany the body). That is intentional — see [Design note: LLM failure vs embedder failure](#design-note-llm-failure-vs-embedder-failure).

---

<a id="semantic-search"></a>
## Semantic search · [↑](#toc)

The **Semantic search** field in the UI enables semantic (vector) search over review texts. When a query is provided, the system:

1. Embeds the query with the BGE-M3 **query** instruction prefix via **BAAI/bge-m3** (1024-dim, multilingual).
2. **INNER JOIN**s `bankiru.reviews` with `bankiru.review_embeddings`, applies all scalar filters (date range, bank, product, location), ranks by cosine distance, and applies optional tuning (`SEMANTIC_SEARCH_EF_SEARCH`, `SEMANTIC_SEARCH_MAX_DISTANCE`).
3. Returns the top `SEMANTIC_SEARCH_LIMIT` (default 200) most relevant reviews.

**What gets embedded (passage side):** each stored vector encodes enriched review text — `{bankName} | {product} | {location}\n{reviewBody}` with the BGE-M3 **passage** prefix. An empty/whitespace `location` is omitted from the header (`{bankName} | {product}\n{reviewBody}`). This helps queries about banks, products, or cities match even when those words appear only in metadata, not in the review body.

Reviews that have no embedding row yet are **excluded** from semantic search (they still appear when Semantic search is empty and are picked up once backfill completes). When embedding the query fails, the request ends in **503** with a fixed `detail`:

> Semantic search is temporarily unavailable: the query could not be embedded. Retry later, or repeat the request without keywords.

The provider's own message names an internal endpoint, so it stays in the Logfire log rather than the response body. This replaces the earlier fail-soft **200** whose `comment` explained the failure: an empty result set was indistinguishable from "nothing matches your query", so a client could record "no complaints on this topic" for a search that never ran. In the Gradio UI the same `detail` arrives as an **error toast**.

<a id="design-note-llm-failure-vs-embedder-failure"></a>
### Design note: LLM failure vs embedder failure · [↑](#toc)

Why LLM errors stay HTTP **200** with text in `comment`, while a broken embedder is **503**: see [Заметка: сбой LLM и сбой эмбеддера](#заметка-сбой-llm-и-сбой-эмбеддера) (Russian half of this README).

When Semantic search is empty (including whitespace-only `keywords`), the query path is unchanged — all matching reviews are returned without vector ranking. There is **no row limit** on this path; very broad filters on a large table can produce heavy exports and long LLM runs.

**Query-time tuning** (via environment variables):

| Variable | Default | Effect |
|----------|---------|--------|
| `SEMANTIC_SEARCH_EF_SEARCH` | `100` | Sets `hnsw.ef_search` for the query transaction (pgvector default is 40). Higher values improve recall at the cost of slightly slower search. |
| `SEMANTIC_SEARCH_MAX_DISTANCE` | `0.55` | Cosine distance ceiling — results above this threshold are excluded. Set empty, `none`, or omit to disable the floor. Must be ≥ 0 when set. |

<a id="embedding-pipeline"></a>
### Embedding pipeline · [↑](#toc)

- **New reviews:** Embedded inline during `POST /reviews` using enriched passage text. If embedding fails, the review is saved without an embedding and will be backfilled later.
- **Startup backfill:** At API startup, a background task embeds any reviews that don't yet have embeddings. A batch that fails repeatedly is skipped for the rest of that run (avoiding an infinite loop); those rows are retried on the next restart or via the CLI.
- **Reindex CLI:** `python -m bankiru.embedder reindex --confirm` regenerates all embeddings from scratch (required after changing embedding format or model). The embedder is a **CLI module**, not a long-lived Compose service — run it with `docker exec bankiru-api`.
- **Build-index CLI:** `python -m bankiru.embedder build-index` creates the HNSW index on existing embeddings only (no re-embed). Use when vectors are present but the index is missing or invalid (~15–45+ min for ~380K rows). API startup also calls this via `ensure_hnsw_index()` and **blocks until the index is ready**.

<a id="embedder-cli"></a>
### Embedder CLI · [↑](#toc)

```bash
# Backfill only — embed reviews missing embeddings
docker exec bankiru-api python -m bankiru.embedder backfill

# Build HNSW index only — no re-embed (recovery after failed reindex)
docker exec bankiru-api python -m bankiru.embedder build-index
docker exec bankiru-api python -m bankiru.embedder build-index --force  # drop + rebuild

# Dry-run — show what reindex would do
docker exec bankiru-api python -m bankiru.embedder reindex

# Full reindex — TRUNCATE + re-embed all reviews (~1 hour for ~380K rows) + index build
docker exec bankiru-api python -m bankiru.embedder reindex --confirm
```

**After deploying embedding-quality changes:** run `reindex --confirm` once so existing vectors use the same enriched passage format as new inserts. New reviews pick up the improved format immediately; old vectors stay stale until reindex completes.

**If reindex finished embedding but failed on index creation:** run `build-index` — do **not** re-run `reindex --confirm`.

<a id="pre-deploy-checklist"></a>
### Pre-deploy checklist · [↑](#toc)

1. **Enable pgvector on Cloud.ru RDS:**
   - Go to RDS console → instance → Plugins
   - Enable the `vector 0.8.0` plugin
   - Restart the instance if required by the plugin activation
   - Verify: `SELECT extversion FROM pg_extension WHERE extname = 'vector';`

2. **Configure Infisical secrets:**
   - Set `EMBEDDINGS_API_KEY` (or reuse `OPENAI_API_KEY` — the embedder falls back automatically)
   - Optionally set `EMBEDDINGS_BASE_URL` and `EMBEDDINGS_MODEL` if different from defaults

3. **Deploy** (always via `start.sh` so secrets and compose interpolation stay in sync):

   ```bash
   ./scripts/start.sh --refresh
   ```

   Or, if secrets are already on tmpfs and only the image changed:

   ```bash
   docker compose --env-file /dev/shm/bankiru-reviews-secrets/.env build
   docker compose --env-file /dev/shm/bankiru-reviews-secrets/.env up -d --force-recreate api parser ui
   ```

4. **Monitor startup** (HNSW index build + backfill):
   ```bash
   docker logs bankiru-api -f
   ```
   On first deploy the API **blocks readiness** while it builds the HNSW index on existing embeddings (~15–45+ min for ~380K rows if vectors are already present; skipped when the index is valid). After that it auto-creates the `review_embeddings` table (if needed) and starts backfilling ~380K reviews in the background. Progress is logged every batch (~500 reviews). Expect ~63 minutes for the initial backfill. Subsequent restarts skip both index rebuild (when valid) and backfill (when all rows are embedded).

---

<a id="ui-and-authentication"></a>
## UI and authentication · [↑](#toc)

The UI service (`python -m bankiru.ui`) mounts a Gradio `Blocks` application inside a FastAPI app. The FastAPI layer handles OIDC; the Gradio layer handles the review query form.

**Layout:** three columns inside a full-height Blocks page — filters (left), format/model/actions (middle), summary (right). The summary lives in a **Summary** accordion with a fixed-height Markdown panel (490 px) and a **copy** button. After a successful Submit with an export URL, an info toast prompts download; API failures surface as Gradio **error** toasts (same `detail` as the REST **400**/**422**/**503**), and no download prompt is shown. Buttons, dropdowns, text inputs, and the Summary accordion use the Ocean theme with **zero corner radius** (rectangular); mount CSS enforces square corners on inner Gradio chrome if theme tokens miss a control. The Gradio footer is hidden via mount CSS.

**OIDC flow:**

```
/              → (no session)  → 302 /login
/login         → authorize_redirect(OIDC_REDIRECT_URI)
                    → Authentik authenticates the user
/auth          → authorize_access_token()
                    session = {sub, username, email, id_token}
                    → 302 /gradio/
/gradio/*      → auth_dependency(get_user) → renders Gradio UI
/logout        → end_session_endpoint?id_token_hint=…&post_logout_redirect_uri=…
                    → session cleared + Authentik SSO session terminated
```

**Gradio controls:**

| Control | Type | Notes |
|---------|------|-------|
| Start / End | DateTime | Date range filter (no time component). Leaving a field empty means the earliest / latest review date in the database, whatever the Summary model selection is. |
| Bank | Multi-select dropdown | 50 banks pre-loaded in `choices.py` (top-50 by complaint volume 2025). Default: `Сбербанк`. |
| Product | Multi-select dropdown | 23 banking product labels in `choices.py` (one entry per distinct label; the parser crawls 24 URL slugs including both `corporate` and `legal` for "Обслуживание юридических лиц"). |
| Location | Multi-select dropdown | 88 Russian regional capitals. Uses `startswith` matching on the server side. |
| Semantic search | Textbox (single line) | Free-text semantic search query (API param: `keywords`). Ranked by cosine similarity, capped at `SEMANTIC_SEARCH_LIMIT`. Only reviews with embeddings participate. |
| Format | Single-select dropdown | `csv`, `json`, `parquet`, `xlsx`. Default: `parquet`. |
| Summary model | Single-select dropdown | Default: `<no summary>` (skips LLM; UI sends `summarize=false`). Other choices from Cloud.ru `/models` API (TTL-cached 1 h); falls back to a hardcoded list if the API is unreachable. Choices are resolved **once at UI process start** — restart the `ui` container after provider catalog changes. Empty Start/End resolve to the earliest / latest review date in the database and still count towards the API’s three-month summarize limit. |
| Submit | Button | Calls `GET /reviews`. On success: fills Summary + signed URL; info toast **"Download your file"** only if an export URL is present. On API/network error: Gradio **error toast** with the API `detail` (e.g. the three-month summarize **400**); clears signed URL and Summary (no stale Download). |
| Clear | Button | Resets all inputs, the summary, and the hidden URL state. |
| Download reviews | Button | Client-side JS: opens the stored pre-signed URL in a new tab. No server round-trip. |
| Download summary | Button | Client-side JS: saves the summary Markdown panel content as a `.md` file. Filename matches the reviews file (same stem, `.md` extension). No server round-trip. |
| Summary panel | Markdown (in accordion) | Displays the LLM summary when a model is selected; stays empty for `<no summary>`. Includes a copy button. Header in the API response uses `**Summary model:**` plus the model name. API validation/range errors appear as Gradio error toasts, not in this panel. |

**Session details:** Starlette `SessionMiddleware`, signed cookie `bankiru_session`, `same_site=lax`, `https_only=True`, 1-hour TTL. Session payload: `{sub, username, email, id_token}`.

<a id="registering-the-oidc-client-in-authentik"></a>
### Registering the OIDC client in Authentik · [↑](#toc)

Create one **OAuth2/OpenID** provider + application:

| Field | Value |
|-------|-------|
| Application slug | `bankiru` |
| Client type | Confidential |
| Authorization flow | any (e.g. `default-provider-authorization-implicit-consent`) |
| Redirect URIs | exactly `OIDC_REDIRECT_URI` (e.g. `https://bankiru.uva-advanced.ru/auth`) |
| Post-Logout Redirect URIs | exactly `OIDC_POST_LOGOUT_URI` (e.g. `https://bankiru.uva-advanced.ru/`) |
| Scopes | `openid profile email` |

Copy the Client ID and Client Secret into Infisical as `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`.

---

<a id="security-hardening"></a>
## Security hardening · [↑](#toc)

Defense-in-depth over a vanilla Authlib-on-Starlette template, plus API edge controls:

1. **Pinned `redirect_uri`** (`OIDC_REDIRECT_URI` from config, not derived from request headers). Defeats header-spoofed open-redirect / auth-code-injection scenarios if `TRUSTED_HOSTS` drifts.
2. **RP-initiated logout** — `/logout` reads `end_session_endpoint` from the OIDC discovery document and redirects with `id_token_hint` + `post_logout_redirect_uri`, terminating the Authentik SSO session (not just the local cookie).
3. **Session rotation** — `request.session.clear()` before writing identity on `/auth`.
4. **Narrow session payload** — only `{sub, username, email, id_token}` stored (not the full `userinfo` dict). Avoids 4 KB cookie corruption; limits what is base64-readable.
5. **UI auto-docs disabled** — `docs_url=None, redoc_url=None, openapi_url=None` on the UI FastAPI app. The `api` service keeps its Swagger docs (public contract at `/docs`).
6. **Explicit `OAuthError` handling** — authentication failures log a warning via Logfire and redirect to `/login` instead of surfacing a 500.
7. **Security headers at the Nginx edge** — HSTS (`max-age=31536000; includeSubDomains`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN` (not `DENY` — Gradio uses iframes internally), `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: geolocation=(), microphone=(), camera=()`.
8. **Dotfile blocking at the edge** — `location ~ /\.` returns 403 for any request containing a dotfile segment (`.env`, `.git/`, `.htaccess`, etc.); `access_log off; log_not_found off` silences scanner noise. A nested exception lets `/.well-known/` through for ACME challenges.
9. **API loopback bind** — compose publishes `api` as `127.0.0.1:API_PORT` only, so the REST port is not reachable from the public internet except through Nginx.
10. **Gateway guest tokens** — Nginx overwrites `X-Bankiru-Gateway` on `/reviews`; the API then requires `API-Token` ∈ `GUEST_API_TOKEN` or `API_TOKEN` for GET. Write routes accept **`API_TOKEN` only**. The Gradio UI (already behind Authentik) calls the API over the compose network without a token.

**Explicitly NOT done** (with rationale):

- **CSP**: incompatible with Gradio's inline scripts and `eval`.
- **Cookie encryption**: signing is sufficient given the narrowed payload.
- **`/login` rate limiting**: delegated to Authentik's brute-force protection.
- **HSTS preload**: opt in manually once long-term cert renewal is proven stable.
- **OIDC on REST**: external API clients use shared guest tokens instead.

---

<a id="repository-layout"></a>
## Repository layout · [↑](#toc)

```text
bankiru-reviews/
├── assets/
│   ├── bankiru-icon.png            # favicon (served at /favicon.ico)
│   ├── bankiru-logo.svg            # wordmark (black text)
│   └── bankiru-logo-white.svg      # wordmark (white text; README header)
├── config/
│   └── bankiru-reviews.conf        # Nginx vhost (TLS, UI + API proxy, Gradio SSE/WS)
├── docs/
│   └── bankiru-reviews-public-api.md  # RU guide for https://bankiru.uva-advanced.ru API
├── docker-compose.yml              # api + parser + ui; single shared env_file on tmpfs
├── Dockerfile                      # one image, three CMDs; uv, python:3.13-slim
├── pyproject.toml                  # single uv project; src layout; hatchling build
├── uv.lock                         # locked dependency tree
├── .env.example                    # canonical key list with all defaults shown
├── scripts/
│   ├── start.sh                    # Infisical bootstrap → docker compose up
│   └── check-public-api.sh         # live checks of GET /reviews (read-only)
├── tests/                          # pytest suite; no Postgres / S3 (fakes via dependency_overrides)
│   ├── conftest.py                 # env vars, FakeSession / FakeBotoClient / FakeReview, stubs, app factory
│   ├── test_auth_gateway.py        # gateway header, guest / admin tokens, write routes
│   ├── test_guest_tokens.py        # GUEST_API_TOKEN owner:token parsing
│   ├── test_query_validation.py    # formats, booleans, repeated params, dates over HTTP
│   ├── test_schemas.py             # ReviewsQuery validation: extra_forbidden, date formats
│   ├── test_date_resolution.py     # omitted bound → min / max datePublished
│   ├── test_date_bounds_sql.py     # resolved bounds reach SQL, inclusive on both ends
│   ├── test_summarize_limit.py     # three-calendar-month limit and its detail text
│   ├── test_inverted_range.py      # endDate before startDate → 400
│   ├── test_empty_table.py         # no bounds to resolve → 200 "no results"
│   ├── test_filters_sql.py         # each filter in SQL; standard vs semantic path
│   ├── test_response_shape.py      # inline / export / no results / summary, and the echo
│   ├── test_semantic_failure.py    # embedder down → 503, provider message stays in the log
│   ├── test_export_failures.py     # S3 refusing an upload or a pre-signed URL → 500
│   ├── test_ui_params.py           # which query params Submit actually sends
│   ├── test_ui_errors.py           # gr.Error toasts, Download info toast, failure cleanup
│   └── test_documented_messages.py # error texts and test names quoted in the docs stay in step
└── src/bankiru/
    ├── __init__.py                 # __version__ = "0.1.0"
    ├── config.py                   # Pydantic Settings; all env vars in one place
    ├── logging.py                  # configure_logfire() + install_auto_tracing()
    ├── db.py                       # async SQLAlchemy engine, session factory, create_all_tables(), ensure_hnsw_index()
    ├── models.py                   # Review + ReviewEmbedding ORM (schema="bankiru"); indexes; review_columns list
    ├── api/
    │   ├── __main__.py             # uvicorn entry; auto-traces api.routes + api.handlers
    │   ├── app.py                  # FastAPI factory; lifespan: create_all_tables (blocks on HNSW) + backfill task
    │   ├── routes.py               # GET/POST/DELETE /reviews; GET /healthz
    │   ├── deps.py                 # DBSession, BotoClient, api_token type aliases
    │   ├── schemas.py              # Pydantic Request / Response / Review; format registry
    │   ├── handlers.py             # ScalarsHandler base; CSV/JSON/Parquet/XlsxMaker; asyncio.to_thread
    │   ├── summarizer.py           # summarize_map_reduce; tiktoken chunker; pydantic_ai
    │   ├── model_catalog.py        # Cloud.ru /models TTL cache; get_model_context()
    │   └── botocore_client.py      # aiobotocore async S3 client factory
    ├── embedder/
    │   ├── __init__.py             # embed_texts(), format_review_for_embedding(), backfill_embeddings(), reindex_embeddings()
    │   └── __main__.py             # CLI: backfill | build-index [--force] | reindex [--confirm]
    ├── parser/
    │   ├── __main__.py             # APScheduler entry; SIGHUP live reschedule
    │   ├── runner.py               # run_once(days=N); POST with capped retry
    │   ├── crawler.py              # BankiruCrawler; product/page/detail loop
    │   ├── client.py               # BankiruClient; randomised pacing; unlimited ban retry
    │   ├── settings.py             # PRODUCTS dict; regexes; UA/Accept-Language pools
    │   └── tools.py                # clean_text_pipe (double strip-tags → emoji → strip)
    └── ui/
        ├── __main__.py             # uvicorn entry; auto-traces ui.app + ui.blocks
        ├── app.py                  # FastAPI + SessionMiddleware + Authlib OIDC + Gradio mount (Ocean theme, rectangular controls)
        ├── blocks.py               # Gradio Blocks (3-column layout); async get_reviews; Download JS
        ├── choices.py              # static BANKS / LOCATIONS / PRODUCTS / FILE_FORMATS
        └── foundation_models.py    # sync wrapper; TTL cache; fail-soft to hardcoded list
```

---

<a id="secrets--infisical--tmpfs"></a>
## Secrets — Infisical + tmpfs · [↑](#toc)

Secrets live in a self-hosted Infisical instance and are pulled into a tmpfs file at start-up — **they never touch the SSD** and are wiped on host reboot.

| Parameter | Value |
|-----------|-------|
| Infisical API | `https://infisical.uva-advanced.ru/api` |
| Project ID | `1038a643-15a6-42f5-9996-22cbc9b4738e` |
| Environment | `prod` |
| Path | `/` (project root) |
| Auth method | Universal Auth |
| Client ID (not a secret) | `b8be4a01-8d9c-4a6a-b85c-28ad705e6144` |

`scripts/start.sh` authenticates with Universal Auth, runs `infisical export` for the `prod` environment at path `/`, and writes the result to `/dev/shm/bankiru-reviews-secrets/.env`. It then passes `--env-file` to `docker compose`, which both interpolates `${VAR:-default}` in `docker-compose.yml` and injects variables into containers via `env_file`.

> **Always use `./scripts/start.sh`** — never run `docker compose up` directly on first boot or after a host reboot (which clears `/dev/shm`). Port substitutions and container env injection both depend on the secret file existing.

---

<a id="configuration-reference"></a>
## Configuration reference · [↑](#toc)

All configuration is environment-driven via Pydantic Settings (`src/bankiru/config.py`). The same env file is shared by all three containers.

<a id="required-no-defaults"></a>
### Required (no defaults) · [↑](#toc)

| Variable | Used by | Purpose |
|----------|---------|---------|
| `API_TOKEN` | api, parser | Privileged secret on the `API-Token` header for `POST`/`DELETE` `/reviews*` and for gateway `GET /reviews`. |
| `POSTGRES_URL` | api | `postgresql+psycopg://user:pass@host/db`. |
| `OBS_BUCKET` | api | S3 bucket name. |
| `OBS_ACCESS_KEY` | api | S3 access key. |
| `OBS_SECRET_KEY` | api | S3 secret key. |
| `OBS_REGION` | api | S3 region. |
| `OBS_ENDPOINT` | api | S3-compatible endpoint URL (e.g. `https://s3.cloud.ru`). |
| `SESSION_MIDDLEWARE_SECRET` | ui | Signs Starlette session cookies. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `OIDC_CLIENT_ID` | ui | Authentik OAuth2 client ID. |
| `OIDC_CLIENT_SECRET` | ui | Authentik OAuth2 client secret. |

<a id="optional-shown-with-defaults"></a>
### Optional (shown with defaults) · [↑](#toc)

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGFIRE_TOKEN` | `None` | Logfire ingestion token. Omit for local dev (no-op). |
| `GUEST_API_TOKEN` | `[]` (empty) | Comma-separated `owner@example.org:token` pairs for `GET /reviews` via `https://bankiru.uva-advanced.ru`. The client sends only the token in `API-Token`. Guests cannot `POST`/`DELETE`. Empty → only `API_TOKEN` accepted on the gateway GET path. Tokens must not contain commas. |
| `OBS_BACKUP_PREFIX` | `bankiru-reviews` | S3 key prefix (subfolder) for daily Parquet backups. Files are written as `{prefix}/bankiru-reviews-YYYY-MM-DD.parquet`. |
| `API_PORT` | `1706` | API listen port (bound to `127.0.0.1` on the host). If changed, also update `CREATE_REVIEWS_ENDPOINT`, `GET_REVIEWS_URL`, and the Nginx upstream port in `config/bankiru-reviews.conf`. |
| `UI_PORT` | `17060` | UI listen port (bound to `127.0.0.1` on the host). |
| `CREATE_REVIEWS_ENDPOINT` | `http://api:1706/reviews` | Where the parser POSTs batches. |
| `GET_REVIEWS_URL` | `http://api:1706/reviews` | Where the UI GETs filtered exports. |
| `PARSER_CRON_HOUR` | `0` | Hour of the daily crawl (cron, local `PARSER_TIMEZONE`). |
| `PARSER_CRON_MINUTE` | `5` | Minute of the daily crawl. |
| `PARSER_TIMEZONE` | `Europe/Moscow` | Timezone for the cron schedule. |
| `PARSER_DAYS` | `1` | Number of past calendar days to collect per run. Set to `7` to backfill a week. |
| `PARSER_SLEEP_MIN` | `10.0` | Minimum random sleep before each HTTP request (seconds). |
| `PARSER_SLEEP_MAX` | `20.0` | Maximum random sleep (seconds). Average `~15 s ≈ 4 req/min`. |
| `PARSER_CONNECT_TIMEOUT` | `5.0` | TCP connect timeout (seconds). Short to detect bans quickly. |
| `PARSER_READ_TIMEOUT` | `15.0` | Response read timeout (seconds). |
| `PARSER_MAX_RETRIES` | `5` | Retries per request on non-connect errors before abandoning. |
| `PARSER_BAN_PAUSE_MAX` | `300.0` | Max back-off pause (seconds) during connect-error streaks. |
| `OPENAI_API_KEY` | `None` | Required for LLM summarization and the model dropdown. |
| `OPENAI_BASE_URL` | `https://foundation-models.api.cloud.ru/v1` | OpenAI-compatible base URL. Any OpenAI-compatible provider works. |
| `DEFAULT_CLOUD_MODEL` | `anthropic/claude-sonnet-4.6` | Fallback summarization model when `summarize=true` and `cloudModel` is omitted. The Gradio UI defaults to `<no summary>` instead. |
| `OUTPUT_TOKENS_LIMIT` | `50000` | Per-call output token cap (clipped to `max_model_len // 4` for small-context models). |
| `DEFAULT_MODEL_CONTEXT` | `200000` | Fallback context window when the Cloud.ru `/models` catalog is unreachable. |
| `SUMMARIZER_MAP_CONCURRENCY` | `4` | Maximum concurrent LLM calls in the map pass. |
| `SUMMARIZER_SAFETY_MARGIN_TOKENS` | `512` | Slack subtracted from the input budget each pass. |
| `SUMMARIZER_MAX_PASSES` | `4` | Hard ceiling on map-reduce recursion depth. If exceeded, partial summaries are joined verbatim. |
| `TRUSTED_HOSTS` | `*` | Upstreams whose `X-Forwarded-*` headers `ProxyHeadersMiddleware` trusts. Safe as `*` because the container is bound to `127.0.0.1`. |
| `OIDC_DISCOVERY_URL` | Authentik well-known URL | OIDC discovery document endpoint. |
| `OIDC_REDIRECT_URI` | `None` (falls back to `url_for`) | Must exactly match the Redirect URI registered in Authentik. Set in production. |
| `OIDC_POST_LOGOUT_URI` | `None` (falls back to `/`) | Must exactly match a Post-Logout Redirect URI in Authentik. |
| `EMBEDDINGS_API_KEY` | `None` | Cloud.ru API key for embeddings. Falls back to `OPENAI_API_KEY` if not set. |
| `EMBEDDINGS_BASE_URL` | `None` | Base URL for the embeddings endpoint. Falls back to `OPENAI_BASE_URL` if not set. |
| `EMBEDDINGS_MODEL` | `BAAI/bge-m3` | Embedding model name. Default: `BAAI/bge-m3`. |
| `EMBEDDINGS_DIMENSIONS` | `1024` | Vector dimensions. Default: `1024`. |
| `EMBEDDINGS_BATCH_SIZE` | `50` | Texts per API call. Default: `50`. |
| `EMBEDDINGS_BACKFILL_BATCH` | `500` | DB rows per backfill iteration. Default: `500`. |
| `SEMANTIC_SEARCH_LIMIT` | `200` | Max results when Semantic search is used. Default: `200`. |
| `SEMANTIC_SEARCH_EF_SEARCH` | `100` | pgvector HNSW recall at query time (default in pgvector is 40). |
| `SEMANTIC_SEARCH_MAX_DISTANCE` | `0.55` | Cosine distance ceiling; set empty, `none`, or omit to disable. Must be ≥ 0 when set. |
| `AWS_REQUEST_CHECKSUM_CALCULATION` | `when_required` | Botocore compat flag for non-AWS S3. Set in environment, not as a Pydantic field. |
| `AWS_RESPONSE_CHECKSUM_VALIDATION` | `when_required` | Same. |

**Port coupling note:** `API_PORT`, `CREATE_REVIEWS_ENDPOINT`, and `GET_REVIEWS_URL` must agree on the same port. The `.env.example` comment flags this explicitly.

---

<a id="quick-start"></a>
## Quick start · [↑](#toc)

<a id="production-with-infisical"></a>
### Production (with Infisical) · [↑](#toc)

```bash
# 1. Install the Infisical CLI (once per host)
curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | sudo -E bash
sudo apt-get update && sudo apt-get install -y infisical

# 2. Clone
git clone <repo-url> ~/git/bankiru-reviews
cd ~/git/bankiru-reviews

# 3. Register the OIDC client in Authentik (see "UI and authentication" above);
#    populate OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / GUEST_API_TOKEN in Infisical.

# 4. Start the stack (prompts for the Infisical client secret)
./scripts/start.sh

# 5. Provision / reload the Nginx vhost AFTER api is up with loopback + gateway auth
sudo cp config/bankiru-reviews.conf /etc/nginx/conf.d/bankiru.conf
sudo certbot certonly --webroot -w /var/www/html \
     -d bankiru.uva-advanced.ru -d www.bankiru.uva-advanced.ru
sudo nginx -t && sudo systemctl reload nginx

# 6. Verify
docker compose --env-file /dev/shm/bankiru-reviews-secrets/.env ps
curl http://127.0.0.1:1706/healthz   # {"status": "ok"}
curl -sS https://bankiru.uva-advanced.ru/healthz
open https://bankiru.uva-advanced.ru
```

<a id="startsh-flags"></a>
### `start.sh` flags · [↑](#toc)

```bash
./scripts/start.sh               # fetch secrets + docker compose up -d
./scripts/start.sh --no-start    # fetch secrets only (skip compose up)
./scripts/start.sh --refresh     # re-fetch secrets + force-recreate api/parser/ui
```

Pass the client secret non-interactively:

```bash
INFISICAL_CLIENT_SECRET=… ./scripts/start.sh
```

<a id="local-development-without-docker"></a>
### Local development (without Docker) · [↑](#toc)

```bash
cp .env.example .env
# Fill in at minimum:
#   POSTGRES_URL, OBS_*, API_TOKEN, OPENAI_API_KEY,
#   SESSION_MIDDLEWARE_SECRET, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET

uv sync
uv run python -m bankiru.api      # terminal 1 — http://localhost:1706
uv run python -m bankiru.parser   # terminal 2 — fires at 00:05 by default
uv run python -m bankiru.ui       # terminal 3 — http://localhost:17060
```

Trigger a one-off crawl (bypassing the cron schedule):

```bash
uv run python -c "
from bankiru.logging import configure_logfire; configure_logfire('parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once())
"
```

Trigger a one-off crawl for a specific date range (e.g. May 17–18):

```bash
uv run python -c "
from bankiru.logging import configure_logfire; configure_logfire('parser')
import asyncio; from bankiru.parser.runner import run_once
asyncio.run(run_once(start_date='2026-05-17', end_date='2026-05-19'))
"
```

> **Note:** `end_date` is *exclusive* (the crawl window is `[start_date, end_date)`).
> To collect reviews published on May 17 and May 18, set `end_date` to May 19.

<a id="tests"></a>
### Tests · [↑](#toc)

```bash
uv sync --group dev
uv run pytest
```

The suite needs neither Postgres nor S3: the session and S3 dependencies are
replaced with fakes through `app.dependency_overrides`, and `tests/conftest.py`
supplies the required environment variables (including an empty
`OPENAI_API_KEY`, which keeps the UI import offline). The LLM and the embedder
are always stubbed, through the `summarizer`, `embedder`, `broken_embedder` and
`empty_embedder` fixtures — no test reaches either provider.

**The public gateway in tests.** From the application's point of view a request
through Nginx differs in exactly one way: the `X-Bankiru-Gateway: 1` header,
which the proxy overwrites so a client cannot forge its absence. The tests set
that header themselves (`conftest.gateway()`). Since this layer either passes a
request through untouched or answers 403, the rest of the suite runs without it
and only a representative few scenarios are repeated through the gateway. What
only a real deployment can show — TLS, the proxy setting the header, an error
body reaching the client unaltered — is checked by
[`scripts/check-public-api.sh`](scripts/check-public-api.sh) against the running
service.

#### Layer 1 — authorization · `tests/test_auth_gateway.py`, `tests/test_guest_tokens.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| No gateway header, no token | 200 — the UI and anything on the compose network | `test_an_internal_caller_needs_no_token` |
| No gateway header, nonsense token | 200 — the token is not looked at | `test_an_internal_caller_may_send_any_token` |
| Gateway header set to `0`, `true`, `2`, or empty | 200 — only the exact value `1` engages the check | `test_only_the_exact_header_value_engages_the_check` |
| Gateway + guest token (the secret from either `owner:token` pair) or admin token | 200 | `test_accepted_tokens` |
| Gateway + the `owner:token` pair itself | 403 — the header is the secret only | `test_the_owner_token_pair_is_not_a_credential` |
| Gateway, no token | 403 | `test_a_missing_token_is_refused` |
| Gateway + empty, wrong, or shorter token | 403 — a length mismatch must not raise out of `compare_digest` | `test_refused_tokens` |
| Gateway, no token | Neither query runs | `test_a_refused_caller_reads_no_data` |
| Gateway, no token **and** an unknown parameter | 403 — authorization is decided first | `test_authorization_precedes_query_validation` |
| Guest token on any of the four write routes | 403 — guests never write, gateway header or not, and no statement runs | `test_a_guest_cannot_post`, `test_a_guest_cannot_delete`, `test_a_guest_cannot_delete_by_date`, `test_a_guest_cannot_delete_duplicates`, `test_no_write_route_touches_the_database_unauthorized` |
| Any write route with an absent or empty `API-Token` header | 401, not 403 — `APIKeyHeader` rejects before `api_token` runs | `test_an_absent_write_token_is_401_not_403` |
| `GET /healthz` through the gateway, no token | 200 — Docker's healthcheck depends on it | `test_healthz_needs_no_token_through_the_gateway` |
| `GET /` | Redirect to `/docs` | `test_the_root_redirects_to_the_docs` |
| `GUEST_API_TOKEN` empty | No guest tokens | `test_empty_string_yields_no_tokens` |
| Two `owner:token` pairs | Only the secrets are kept | `test_two_pairs_keep_only_the_tokens` |
| Spaces around commas and colons | Stripped | `test_whitespace_around_commas_and_colons_is_stripped` |
| Trailing comma | Ignored | `test_a_trailing_comma_is_ignored` |
| Empty comma segments (leading, middle, trailing) | Skipped | `test_empty_comma_segments_are_skipped` |
| Token containing colons | Kept in full after the first `:` | `test_a_token_may_contain_colons` |
| `list[str]` constructor argument | Kept as tokens (no pair parse) | `test_a_list_is_kept_as_tokens` |
| Bare token, missing `:`, empty owner/secret, or mixed malformed | `ValidationError` at settings load | `test_malformed_entries_are_rejected` |

#### Layer 2 — query validation · `tests/test_query_validation.py`, `tests/test_schemas.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| `outputFormat` = `csv`, `json`, `parquet`, `xlsx` | 200, `filename` ends with that extension | `test_each_format_is_accepted` |
| The handler set discovered by introspection | Exactly those four | `test_every_handler_is_reachable_by_name` |
| `outputFormat` = `pdf`, `CSV`, or empty | 422 | `test_other_formats_are_rejected` |
| `summarize` = `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` | 200, echoed as a boolean | `test_accepted_booleans` |
| `summarize` = `maybe`, `2`, `-1` | 422 | `test_rejected_booleans` |
| `summarize` omitted | `false` for every caller | `test_summarize_defaults_to_false` |
| `bankName` / `product` / `location` given once | Echoed as a one-element list | `test_a_single_value_becomes_a_one_element_list` |
| The same parameter repeated | Every value collected | `test_a_repeated_parameter_collects_every_value` |
| A date as `2026-03-01`, `20260301`, or `2026-3-1` | All three mean 1 March 2026 | `test_both_date_spellings_reach_the_same_bound` |
| A date as an empty string | Treated as omitted, then resolved | `test_an_empty_date_is_resolved_like_an_omitted_one`, `test_empty_string_means_no_bound` |
| A `datetime` passed to the model | Truncated to a date | `test_datetime_input_is_normalized_to_date` |
| `not-a-date`, `2026-13-45`, `01-03-2026`, `20260231`, `2026-03-01T12:00` | 422 | `test_malformed_dates_are_rejected`, `test_malformed_date_is_rejected` |
| An unknown parameter | 422, `extra_forbidden`, naming the parameter | `test_an_unknown_parameter_names_itself`, `test_unknown_query_param_is_rejected` |
| Several unknown parameters | All of them reported | `test_every_unknown_parameter_is_reported` |
| An unknown parameter | No query reaches the database | `test_validation_precedes_any_database_work` |
| `cloudModel` with `summarize=false` | Echoed, but nothing is summarized | `test_cloud_model_without_summarize_is_inert` |

#### Layer 3 — date bounds · `tests/test_date_resolution.py`, `tests/test_date_bounds_sql.py`, `tests/test_inverted_range.py`, `tests/test_summarize_limit.py`, `tests/test_empty_table.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| `startDate` omitted | Earliest stored `datePublished`, whatever `summarize` is | `test_omitted_start_resolves_to_min` |
| `endDate` omitted | Latest stored `datePublished` — never "today" | `test_omitted_end_resolves_to_max`, `test_bounds_do_not_depend_on_the_current_date` |
| Both omitted | The full span of the stored data | `test_both_omitted_span_the_stored_data` |
| Both given | The bounds query is skipped entirely | `test_no_bounds_query_when_both_dates_given` |
| Other filters given | Bounds are still read over the whole table, so a narrow filter cannot dodge the limit | `test_bounds_query_ignores_the_other_filters` |
| A guest through the gateway | The same bounds as an internal caller | `test_a_guest_through_the_gateway_gets_the_same_bounds` |
| `min()` / `max()` return `date` rather than `datetime` | Accepted | `test_plain_date_bounds_are_accepted` |
| Any resolved range | Inclusive in SQL: `00:00:00` to `23:59:59.999999` | `test_bounds_are_inclusive`, `test_single_day_range_covers_that_day` |
| Dates omitted | The resolved bounds still reach the SQL filter | `test_resolved_bounds_are_applied_when_dates_are_omitted` |
| `keywords` or `summarize=true` | The same bounds as the plain path | `test_semantic_search_filters_on_the_resolved_bounds`, `test_summarized_query_filters_on_the_same_bounds` |
| Effective `endDate` before `startDate` | 400 with a fixed `detail`, main query skipped | `test_explicitly_inverted_dates`, `test_start_after_the_last_stored_review`, `test_end_before_the_first_stored_review`, `test_inverted_range_skips_the_main_query` |
| Inverted **and** longer than three months with `summarize` | The inversion is reported | `test_inversion_is_reported_before_the_summarize_limit` |
| `summarize=true`, effective span exactly three calendar months | 200 | `test_exactly_three_months_is_allowed` |
| `summarize=true`, one day more | 400, before the main query | `test_one_day_over_three_months_is_rejected`, `test_rejection_precedes_the_main_query` |
| Three months across a shorter month (30 Nov → 28 Feb) | `relativedelta` clamps, moving the boundary a day | `test_three_months_are_calendar_months_not_ninety_days` |
| `summarize=false`, any span | 200 — the limit guards summarization only | `test_same_range_without_summarize_is_fine` |
| `summarize=true`, no dates at all | 400 — the incident case | `test_omitted_dates_hit_the_limit` |
| `summarize=true`, only `endDate` given over a wide table | 400 — the omitted start opens the interval to the earliest review | `test_omitted_start_over_a_wide_table_is_rejected` |
| `summarize=true`, only `startDate` given, data ending within three months | 200 — the span ends at the newest review, however old that is | `test_omitted_end_within_three_months_is_allowed`, `test_omitted_end_over_stale_data_is_allowed` |
| Empty table, dates omitted | 200 "no results", both echoed dates `null`, main query skipped | `test_empty_table_returns_no_results`, `test_empty_table_skips_the_main_query` |
| Empty table, one bound given | The given bound is echoed back; the other stays `null` | `test_one_given_bound_still_resolves_against_nothing` |
| Empty table, `summarize=true`, dates omitted | 200 — resolution finds no bounds, so the span check never runs | `test_empty_table_does_not_trip_the_summarize_limit` |
| Empty table, both dates given | Bounds are not resolved; an ordinary "no results" with the dates echoed (a span &gt; 3 months with `summarize=true` is still **400**) | `test_explicit_dates_take_the_ordinary_no_results_path` |

#### Layer 4 — filters and the query path · `tests/test_filters_sql.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| No filters | Only the two date comparisons in `WHERE` | `test_without_filters_only_the_dates_are_constrained` |
| `bankName` / `product` | `IN (...)`, exact match | `test_exact_filters_compile_to_in` |
| `location` | `LIKE 'value' \|\| '%'` — prefix match | `test_location_matches_by_prefix` |
| Several locations | Combined with `OR` | `test_several_locations_are_combined_with_or` |
| Dates and all three filters | All combined with `AND` | `test_all_filters_apply_together` |
| No `keywords` | Ordered, and with no `LIMIT` — a broad filter can produce a huge export | `test_the_standard_path_is_ordered_and_unlimited` |
| `keywords` empty or whitespace only | The standard path; the embedder is never called | `test_blank_keywords_take_the_standard_path` |
| `keywords` with text | `JOIN review_embeddings`, ranked by cosine distance, capped by `SEMANTIC_SEARCH_LIMIT` | `test_the_semantic_path_joins_embeddings_and_ranks_by_distance`, `test_the_semantic_limit_comes_from_settings` |
| `keywords` with text | `SET LOCAL hnsw.ef_search` is issued for the transaction | `test_the_semantic_path_raises_hnsw_recall` |
| `keywords` plus scalar filters | Ranking does not widen the filters | `test_the_semantic_path_keeps_the_scalar_filters` |
| `SEMANTIC_SEARCH_MAX_DISTANCE` set | The distance also appears as a ceiling in `WHERE` | `test_the_distance_ceiling_is_applied_when_configured` |
| `SEMANTIC_SEARCH_MAX_DISTANCE` empty | Ranking only, no ceiling | `test_no_distance_ceiling_when_disabled` |
| `SEMANTIC_SEARCH_EF_SEARCH` zero or negative | Clamped to 1 — the value is interpolated into SQL | `test_a_nonpositive_ef_search_is_clamped` |
| `bankName` / `product` given as an empty string | A real filter for the empty string: matches nothing | `test_an_empty_exact_filter_matches_nothing` |
| `location` given as an empty string | The opposite — an empty prefix matches everything | `test_an_empty_location_matches_everything` |
| `keywords` with `summarize=true` | The LLM reads the ranked, capped rows rather than the whole interval | `test_a_summary_reads_the_ranked_result_set` |

#### Layer 5 — the response · `tests/test_response_shape.py`, `tests/test_semantic_failure.py`, `tests/test_export_failures.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| No `outputFormat`, rows found | `reviews` inline; `url` / `filename` `null` | `test_inline_branch` |
| An inline row | Exactly the seven documented fields, `datePublished` as `YYYY-MM-DD HH:MM:SS` | `test_inline_rows_carry_every_documented_field` |
| `outputFormat` set, rows found | `url` + `filename`, `reviews` `null`, one upload to S3 | `test_export_branch`, `test_the_export_is_uploaded_with_a_body` |
| No rows | `comment` is the "no results" message; `reviews`, `url`, `filename` all `null` | `test_no_results_branch` |
| No rows with `outputFormat` | No link to a file that was never written | `test_no_results_with_an_output_format_has_no_url` |
| No rows with `summarize=true` | The summarizer is not called | `test_no_results_never_calls_the_summarizer` |
| `summarize=true` | `comment` opens with `**Summary model:**` and the model used | `test_the_summary_names_the_default_model`, `test_an_explicit_model_overrides_the_default` |
| Duplicate review bodies | Deduplicated before the LLM sees them | `test_identical_bodies_are_summarized_once` |
| `summarize=true` with `outputFormat` | Both the summary and the download URL | `test_a_summary_and_an_export_arrive_together` |
| `summarize` false or omitted | `comment` is `null` | `test_no_summary_without_the_flag` |
| Explicit dates | Echoed unchanged, whatever `summarize` is | `test_echo_repeats_explicit_dates_unchanged` |
| Any filters | All echoed, so the response is self-describing | `test_every_filter_is_echoed_back` |
| Any response | Exactly the thirteen documented fields | `test_the_response_holds_no_unexpected_fields` |
| `keywords` the embedder cannot embed | 503 with a fixed `detail`; no rows are read | `test_embedder_failure_is_a_503`, `test_no_reviews_are_read_when_the_search_cannot_run` |
| The provider answers successfully but with no vector | The same 503, not a 500 | `test_an_empty_provider_answer_is_the_same_503` |
| The same, through the gateway | The same 503 — not to be read as an authorization failure | `test_the_same_503_arrives_through_the_gateway` |
| The same | The provider's own message (naming an internal endpoint) is logged and kept out of the body | `test_the_provider_message_stays_out_of_the_response`, `test_the_provider_message_is_logged` |
| `keywords` with a working embedder | 200; the query is embedded stripped, in `query` mode | `test_a_working_embedder_still_searches`, `test_the_query_is_stripped_before_embedding` |
| S3 refuses the upload or the pre-signed URL | 500 — never a 2xx, and no filename to retry | `test_a_failed_export_is_never_a_success`, `test_a_failed_export_names_no_file` |
| The same, with `summarize=true` | The summary is lost with the response rather than delivered alone | `test_a_lost_summary_is_not_delivered_alone` |
| No `outputFormat` with S3 unavailable | 200 — the inline path never touches the bucket | `test_an_inline_query_never_touches_s3` |
| Any export | The link keeps botocore's default lifetime, about an hour | `test_the_download_link_keeps_the_default_lifetime` |

#### Layer 6 — the Gradio UI · `tests/test_ui_params.py`, `tests/test_ui_errors.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| Model dropdown at `<no summary>`, empty, or cleared | `summarize=false`, no `cloudModel` | `test_no_model_means_no_summarization` |
| A model chosen | `summarize=true` plus `cloudModel` | `test_a_chosen_model_requests_a_summary` |
| No summary wanted | `summarize=false` is still sent — the filter drops by value, not by truthiness | `test_summarize_false_is_sent_rather_than_dropped` |
| Empty dates, lists, or keywords | Omitted from the request rather than sent empty | `test_empty_inputs_are_not_sent` |
| Every input filled | All of them reach the API, lists as repeated parameters | `test_every_filled_input_reaches_the_api` |
| A format chosen in the dropdown | Passed through unchanged | `test_each_format_is_passed_through` |
| The Format dropdown cleared | No `outputFormat` is sent, so the answer comes back inline | `test_a_cleared_format_asks_for_an_inline_answer` |
| Any Submit | Nothing beyond the known parameters, which would come back as a 422 | `test_no_unexpected_parameters_are_sent` |
| API answers 400, 401, 403, or 503 | `gr.Error` toast carrying the API's `detail` verbatim; no info toast | `test_an_api_error_becomes_an_error_toast`, `test_a_failed_request_shows_no_download_prompt` |
| API answers 422 | The `loc`/`msg` list is flattened into one line | `test_validation_errors_are_flattened` |
| A non-JSON or empty error body | Falls back to the text, then to the status code | `test_non_json_body_falls_back_to_text`, `test_empty_body_falls_back_to_the_status_code` |
| A JSON `detail` string | Returned verbatim | `test_string_detail_is_returned_verbatim` |
| The API unreachable | `gr.Error` naming the network failure | `test_network_failure_becomes_an_error_toast` |
| 200 with an export URL | The "Download your file" info toast | `test_download_toast_only_fires_with_a_url` |
| 200 without a URL | No info toast — there is nothing to download | `test_no_toast_without_a_url` |
| Any failed Submit | The `.failure` listener clears the stale URL and Summary | `test_failure_handler_clears_url_and_summary` |

#### Documentation

`tests/test_documented_messages.py` keeps the prose honest: every `detail`
string quoted in this file and in `docs/bankiru-reviews-public-api.md` must
match the constant in the code, and every test named in the tables above must
exist in `tests/`.

---

<a id="day-2-operations"></a>
## Day-2 operations · [↑](#toc)

```bash
# Stream logs
docker logs -f bankiru-api
docker logs -f bankiru-parser
docker logs -f bankiru-ui

# Pick up rotated secrets + rebuild image + recreate containers
./scripts/start.sh --refresh

# After UI-only code changes (skip full secret refresh if tmpfs .env is current):
docker compose --env-file /dev/shm/bankiru-reviews-secrets/.env build ui
docker compose --env-file /dev/shm/bankiru-reviews-secrets/.env up -d --force-recreate ui

# One-off parser run (backfill yesterday)
docker exec bankiru-parser python -c "
from bankiru.logging import configure_logfire; configure_logfire('parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once())"

# Backfill the last 7 days
docker exec bankiru-parser python -c "
from bankiru.logging import configure_logfire; configure_logfire('parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once(days=7))"

# Crawl a specific date range (e.g. May 17–18; end_date is exclusive)
docker exec bankiru-parser python -c "
from bankiru.logging import configure_logfire; configure_logfire('parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once(start_date='2026-05-17', end_date='2026-05-19'))"

# Delete reviews for a date range (e.g. May 17–18; both dates inclusive)
curl -s -X DELETE -H "API-Token: $API_TOKEN" \
  "http://127.0.0.1:1706/reviews/by-date?startDate=2026-05-17&endDate=2026-05-18"

# Deduplicate the database (keeps lowest id per reviewBody+product)
curl -s -X DELETE -H "API-Token: $API_TOKEN" http://127.0.0.1:1706/reviews/duplicates

# Backfill missing embeddings (manual, if startup backfill failed)
docker exec bankiru-api python -m bankiru.embedder backfill

# Build HNSW index only (embeddings OK, index missing/invalid; 15-45+ min)
docker exec bankiru-api python -m bankiru.embedder build-index
docker exec bankiru-api python -m bankiru.embedder build-index --force  # drop + rebuild

# Reindex all embeddings (required after embedding format or model change; ~1 h for ~380K rows)
docker exec bankiru-api python -m bankiru.embedder reindex          # dry-run
docker exec bankiru-api python -m bankiru.embedder reindex --confirm

# Verify embeddings + HNSW index are fully ready (-i required for heredoc stdin)
docker exec -i bankiru-api python - <<'PY'
import asyncio
from sqlalchemy import text
from bankiru.db import get_engine

async def main():
    sql = text("""
        SELECT
          (SELECT count(*) FROM bankiru.reviews)           AS reviews,
          (SELECT count(*) FROM bankiru.review_embeddings) AS embeddings,
          (SELECT count(*) FROM bankiru.reviews r
             LEFT JOIN bankiru.review_embeddings e ON e.review_id = r.id
             WHERE e.review_id IS NULL)                    AS missing,
          i.indisvalid, i.indisready
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        WHERE c.relname = 'ix_review_embeddings_hnsw'
    """)
    async with get_engine().begin() as conn:
        row = (await conn.execute(sql)).first()
    if row is None:
        print("HNSW index MISSING -> run: python -m bankiru.embedder build-index")
        return
    reviews, embeddings, missing, valid, ready = row
    ok = missing == 0 and valid and ready
    print(f"reviews={reviews} embeddings={embeddings} missing={missing} "
          f"index_valid={valid} index_ready={ready}")
    print("READY" if ok else "NOT READY")

asyncio.run(main())
PY

# Postgres backup (external DB)
pg_dump "$POSTGRES_URL" --no-owner --no-acl | gzip > backup-$(date +%F).sql.gz

# S3 backup is automatic — each successful insert merges into
# per-datePublished Parquet files bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet.
```

<a id="count-reviews-per-day-with-url-dedup"></a>
### Count reviews per day (with url dedup) · [↑](#toc)

Query `bankiru.reviews` inside `bankiru-api` — no API call, no summarization, no S3.

- `START` / `END` are calendar dates, **both inclusive** (same as [`GET /reviews`](#get-reviews--filter-export-and-summarize) and [`DELETE /reviews/by-date`](#delete-reviewsby-date--delete-by-date-range)).
- **Total** — row count that day.
- **Unique** — `COUNT(DISTINCT url)` that day.
- **Dup** — `Total − Unique` that day (extra rows sharing a `url` on that day).
  Note: some shared URLs are **expected** (one row per product tag). A storm of
  identical `(url, product)` retries shows up as a much larger Dup than the
  usual multi-tag fan-out.
- **TOTAL** — sums the daily columns. TOTAL **Unique** is the sum of daily `COUNT(DISTINCT url)` values (the same `url` on two days is counted twice). It is not range-level `COUNT(DISTINCT url)` across the whole period.
- Every calendar day in `[START, END]` is printed; days with no reviews show `0`. The example below had reviews on all three days.

Change `START` / `END` in the Python block for other ranges.

```bash
docker exec -i bankiru-api python - <<'PY'
import asyncio
from datetime import date, datetime, time, timedelta
from sqlalchemy import func, select
from bankiru.db import get_engine
from bankiru.models import Review

START = date(2026, 6, 11)   # inclusive
END   = date(2026, 6, 13)   # inclusive

async def main():
    start = datetime.combine(START, time.min)
    end = datetime.combine(END, time.max)
    day_col = func.date_trunc("day", Review.datePublished).label("day")
    sql = (
        select(
            day_col,
            func.count().label("total"),
            func.count(Review.url.distinct()).label("unique_urls"),
        )
        .where(Review.datePublished >= start, Review.datePublished <= end)
        .group_by(day_col)
        .order_by(day_col)
    )
    async with get_engine().begin() as conn:
        rows = (await conn.execute(sql)).all()

    by_day = {r[0].date(): (r[1], r[2]) for r in rows}

    # Build rows for every day in range (including zeros).
    table_rows = []
    sum_total = 0
    sum_unique = 0
    d = START
    while d <= END:
        total, unique = by_day.get(d, (0, 0))
        dup = total - unique
        table_rows.append((d.isoformat(), total, unique, dup))
        sum_total += total
        sum_unique += unique
        d += timedelta(days=1)

    sum_dup = sum_total - sum_unique

    w_day = max(len("Day"), max(len(r[0]) for r in table_rows))
    w_total = max(len("Total"), len(str(sum_total)))
    w_unique = max(len("Unique"), len(str(sum_unique)))
    w_dup = max(len("Dup"), len(str(sum_dup)))

    def sep(left, mid, right, fill="-"):
        return (
            left + fill * (w_day + 2)
            + mid + fill * (w_total + 2)
            + mid + fill * (w_unique + 2)
            + mid + fill * (w_dup + 2) + right
        )

    def row(day, total, unique, dup):
        return (
            f"| {day:<{w_day}} "
            f"| {total:>{w_total}} "
            f"| {unique:>{w_unique}} "
            f"| {dup:>{w_dup}} |"
        )

    print(sep("+", "+", "+"))
    print(row("Day", "Total", "Unique", "Dup"))
    print(sep("+", "+", "+"))
    for day, total, unique, dup in table_rows:
        print(row(day, total, unique, dup))
    print(sep("+", "+", "+"))
    print(row("TOTAL", sum_total, sum_unique, sum_dup))
    print(sep("+", "+", "+"))

asyncio.run(main())
PY
```

Example output (illustrative; your counts will differ):

```text
+------------+-------+--------+-----+
| Day        | Total | Unique | Dup |
+------------+-------+--------+-----+
| 2026-06-11 |   574 |    277 | 297 |
| 2026-06-12 |   440 |    183 | 257 |
| 2026-06-13 |   413 |    182 | 231 |
+------------+-------+--------+-----+
| TOTAL      |  1427 |    642 | 785 |
+------------+-------+--------+-----+
```

<a id="export-reviews-to-csv-on-the-host-no-summarization"></a>
### Export reviews to CSV on the host (no summarization) · [↑](#toc)

Unlike [`GET /reviews`](#get-reviews--filter-export-and-summarize), this does **not** run LLM summarization or upload to OBS.

- `bankiru-api` has **no bind mount** to the VM ([`docker-compose.yml`](docker-compose.yml) does not mount host paths), so the workflow is: write CSV in container `/tmp`, then `docker cp` to the host.
- Uses `get_session_maker()` (not `get_engine().begin()`) so query results are full `Review` ORM rows.
- Host output directory: `~/bankiru-reviews/export/`.
- **Important:** `docker cp`, `rm`, and `wc -l` paths must match `START` / `END` in the script (`reviews-{START}_to_{END}.csv`).
- Very wide date ranges can be slow or hit the DB `statement_timeout` (300 s); narrow the range or use direct `psql` COPY for huge dumps.

```bash
mkdir -p ~/bankiru-reviews/export

docker exec -i bankiru-api python - <<'PY'
import asyncio
from datetime import date, datetime, time
from pathlib import Path
from sqlalchemy import select
from bankiru.db import get_session_maker
from bankiru.models import Review

START, END = date(2026, 6, 11), date(2026, 6, 13)   # both inclusive
OUT = Path(f"/tmp/reviews-{START}_to_{END}.csv")

async def main():
    start = datetime.combine(START, time.min)
    end = datetime.combine(END, time.max)
    sql = (
        select(Review)
        .where(Review.datePublished >= start, Review.datePublished <= end)
        .order_by(Review.datePublished, Review.url, Review.product)
    )
    async with get_session_maker()() as session:
        rows = (await session.execute(sql)).scalars().all()

    import pandas as pd
    df = pd.DataFrame([{
        "datePublished": r.datePublished,
        "reviewBody": r.reviewBody,
        "bankName": r.bankName,
        "url": r.url,
        "location": r.location,
        "product": r.product,
    } for r in rows])
    df.to_csv(OUT, index=False)
    print(OUT, len(df))

asyncio.run(main())
PY

docker cp bankiru-api:/tmp/reviews-2026-06-11_to_2026-06-13.csv \
  ~/bankiru-reviews/export/

# Optional: verify row count (+1 for header) — after successful docker cp above
wc -l ~/bankiru-reviews/export/reviews-2026-06-11_to_2026-06-13.csv
```

**Clean up `bankiru-api` `/tmp` after a successful `docker cp`**

Exports sit in the container filesystem, not on the VM. Remove the file so repeated exports do not accumulate. `/tmp` is also cleared on container recreate, but explicit cleanup is good on a long-lived container. Do not run `rm -rf /tmp/*` — use the specific file or the `reviews-*.csv` pattern only.

Remove one file (match the export filename):

```bash
docker exec bankiru-api rm -f /tmp/reviews-2026-06-11_to_2026-06-13.csv
```

Remove all export CSVs from `/tmp`:

```bash
docker exec bankiru-api sh -c 'rm -f /tmp/reviews-*.csv'
```

<a id="changing-the-daily-crawl-schedule"></a>
### Changing the daily crawl schedule · [↑](#toc)

The parser reads `PARSER_CRON_HOUR` and `PARSER_CRON_MINUTE` at startup and also reloads them on **SIGHUP** ([`__main__.py`](src/bankiru/parser/__main__.py)).

<a id="option-a--sighup-safe-while-a-crawl-is-running"></a>
#### Option A — SIGHUP (safe while a crawl is running) · [↑](#toc)

Write the new cron values into `/app/.env` **inside the container**, then send SIGHUP to PID 1. The handler reads the file, patches `os.environ`, rebuilds `Settings`, and calls `reschedule_job()`. The running crawl (if any) is **not** interrupted — only the *next* trigger time changes.

```bash
# Single command — write .env + signal, all inside the container:
docker exec bankiru-parser sh -c \
    'printf "PARSER_CRON_HOUR=3\nPARSER_CRON_MINUTE=30\n" > .env && kill -HUP 1'
```

Check the parser logs to confirm:

```bash
docker logs --tail 5 bankiru-parser
# … SIGHUP: rescheduled to 03:30 Europe/Moscow
```

> **What `kill -HUP 1` does:**
>
> ```
> kill    -HUP    1
>  │        │     └─ PID 1 = the parser's main Python process (container init)
>  │        └─ send SIGHUP (signal #1) — the Unix "reload config" convention
>  └─ deliver a signal (does NOT kill the process when a handler is registered)
> ```
>
> The parser registers a SIGHUP handler at startup.  On receiving the signal
> it reads `/app/.env`, patches `os.environ` (Docker-injected env vars are
> immutable from outside, so the file is the only override path), clears the
> cached `Settings`, and calls `reschedule_job()`.  The process stays alive;
> only the next trigger time changes.

<a id="option-b--container-restart-kills-any-running-crawl"></a>
#### Option B — Container restart (kills any running crawl) · [↑](#toc)

Update the values in Infisical (or edit `/dev/shm/bankiru-reviews-secrets/.env` on the host), then restart:

```bash
# Minimal restart — only the parser; api and ui stay up
docker compose --env-file /dev/shm/bankiru-reviews-secrets/.env restart parser
```

The scheduler's `replace_existing=True` ensures the new trigger cleanly replaces the old one.

> **⚠️ If a manual `docker exec` crawl is running:**
>
> - `docker compose restart parser` stops and restarts the container. The `docker exec` process **is killed** because all processes in the container's PID namespace are terminated.
> - `./scripts/start.sh --refresh` runs `docker compose up -d --build --force-recreate`, which **destroys and recreates** the container — any `docker exec` process is killed as well.
>
> Use **Option A** (SIGHUP) to reschedule without interrupting a running crawl.

---

<a id="описание-проекта-на-русском"></a>
## Описание проекта (на русском) · [↑](#toc)

Краткое описание системы на русском языке. Подробности — в англоязычных разделах выше.

<a id="назначение"></a>
### Назначение · [↑](#toc)

Централизованный сбор, хранение и анализ негативных отзывов и претензий к российским банкам с портала [banki.ru](https://www.banki.ru) (оценки 1–2 звезды): фильтрация, семантический поиск, выгрузка в объектное хранилище или inline JSON, опциональная LLM-суммаризация — через веб-интерфейс (Authentik/OIDC) и публичный REST API с гостевыми токенами (`GUEST_API_TOKEN`).

<a id="парсер-и-источник-данных"></a>
### Парсер и источник данных · [↑](#toc)

Ежедневный cron (APScheduler, по умолчанию **00:05** `Europe/Moscow`) обходит **24 URL-слага** (**23** уникальных названия продуктов: 12 для физлиц и 11 для юрлиц; слаги `corporate` и `legal` ведут к одной услуге «Обслуживание юридических лиц»). За каждый запуск собираются отзывы за последние `PARSER_DAYS` календарных суток (по умолчанию **1** — «вчера») и одним пакетом отправляются в API.

Перед POST парсер опрашивает `GET /healthz`. Таймаут POST — **600 с**. При сбоях сети и **5xx** (включая **503** при сбое S3-бэкапа после commit) повторяет отправку с экспоненциальной паузой (до 60 с), **не более 20 попыток**; при **401/403/404/422** сразу завершается с ошибкой. API идемпотентен по `(url, product)` — повторный POST после успешного commit не размножает строки; all-skipped повтор **снова мержит** payload в OBS.

**Стратегия обхода banki.ru:** один HTTP-запрос за раз; перед каждым — случайная пауза `uniform(PARSER_SLEEP_MIN, PARSER_SLEEP_MAX)` (по умолчанию 10–20 с, ~4 запроса/мин). Раздельные таймауты на соединение и чтение. Ошибки TCP-соединения (вероятный бан WAF) повторяются без лимита с нарастающей задержкой. HTTP/2 отключён — WAF banki.ru отбрасывает ALPN `h2`. При расхождении числа content/URL на листинге пары страницы **отбрасываются** (без zip неравных списков), пагинация продолжается. Один и тот же отзыв хранится по одному ряду на каждый тег продукта (без UNIQUE на `url`). Дедуп вставки — `(url, product)`; очистка почти-дублей тел — `(reviewBody, product)` (в SQL через `md5(reviewBody)`).

<a id="хранение"></a>
### Хранение · [↑](#toc)

Внешний PostgreSQL, схема `bankiru`:

| Таблица | Назначение |
|---------|------------|
| `reviews` | Отзывы: дата, текст, банк, URL, город, продукт. Одна страница → несколько строк (по тегу `product`); UNIQUE на `url` нет |
| `review_embeddings` | Векторы pgvector (1024 измерения, BAAI/bge-m3), индекс HNSW |

Схема создаётся при старте API (`create_all_tables()`); построение отсутствующего HNSW-индекса **блокирует** готовность API. Бэкап — Parquet в OBS по дате отзыва (`datePublished`): `bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet` (сбой PutObject → **503**, отзывы уже в Postgres; парсер ретраит и мержит payload на all-skipped; многодневный пакет режется по датам). DELETE эндпоинты OBS не чистят.

<a id="api-сервис-api-порт-по-умолчанию-1706"></a>
### API (сервис `api`, порт по умолчанию 1706) · [↑](#toc)

| Эндпоинт | Аутентификация | Назначение |
|----------|----------------|------------|
| `GET /healthz` | нет | Проверка доступности (Docker healthcheck) |
| `GET /` | нет | Редирект на `/docs` (Swagger) |
| `POST /reviews` | `API-Token` = `API_TOKEN` | Приём пакета: дедуп и пропуск уже сохранённых `(url, product)` (без upsert), INSERT, inline-эмбеддинги, мерж в Parquet по `datePublished` (в т.ч. all-skipped retry). Ответ всегда `{"inserted", "skipped"}` (в т.ч. `[]` → нули). |
| `GET /reviews` | внутри сети — нет; через Nginx — гостевой или `API_TOKEN` | Фильтры; без `outputFormat` — inline `reviews`, с форматом — файл в S3; `summarize` по умолчанию **false** везде; неизвестные query → **422**; пустая граница дат = граница данных (`min` / `max` `datePublished`) при любом `summarize` — и для SQL, и для полей-эхо; перевёрнутый диапазон → **400**; при `summarize=true` эффективный интервал ≤ 3 календарных месяцев иначе **400** с фиксированным `detail`; недоступный провайдер эмбеддингов при заданном `keywords` → **503**. Gradio: error toast + очистка URL/Summary. Публичный URL: `https://bankiru.uva-advanced.ru` (см. [`docs/bankiru-reviews-public-api.md`](docs/bankiru-reviews-public-api.md)) |
| `DELETE /reviews` | только `API_TOKEN` | Удаление по списку ID |
| `DELETE /reviews/by-date` | только `API_TOKEN` | Удаление по диапазону дат (включительно) |
| `DELETE /reviews/duplicates` | только `API_TOKEN` | Дедупликация таблицы (остаётся строка с минимальным `id`) |

**Фильтры `GET /reviews`:** диапазон дат; банк и продукт — точное совпадение; город — префикс (`startswith`). Порт API на хосте слушает только `127.0.0.1`. Без `outputFormat` ответ содержит список в `reviews` (не Parquet по умолчанию).

<a id="семантический-поиск"></a>
### Семантический поиск · [↑](#toc)

В UI поле называется **Semantic search**; параметр API — `keywords`.

При запросе с непустым (после `strip`) `keywords`:

1. Текст запроса кодируется моделью **BAAI/bge-m3** с **query**-префиксом BGE-M3.
2. Выполняется **INNER JOIN** `reviews` + `review_embeddings`, применяются все скалярные фильтры, ранжирование по косинусному расстоянию (pgvector HNSW).
3. Возвращаются топ-`SEMANTIC_SEARCH_LIMIT` результатов (по умолчанию **200**).

**Что попадает в вектор (passage):** обогащённый текст `{bankName} | {product} | {location}\n{reviewBody}` с **passage**-префиксом BGE-M3; пустой/`whitespace` `location` из заголовка выкидывается (`{bankName} | {product}\n{reviewBody}`). Это помогает находить отзывы по банку, продукту или городу, даже если эти слова есть только в метаданных.

| Переменная | По умолчанию | Назначение |
|----------|--------------|------------|
| `SEMANTIC_SEARCH_LIMIT` | `200` | Максимум строк в semantic-режиме |
| `SEMANTIC_SEARCH_EF_SEARCH` | `100` | `hnsw.ef_search` на время запроса (выше — лучше recall) |
| `SEMANTIC_SEARCH_MAX_DISTANCE` | `0.55` | Потолок косинусного расстояния; пусто/`none`/не задано — отключить |

Отзывы **без** строки в `review_embeddings` в semantic-поиск **не попадают** (но видны при пустом Semantic search). Без Semantic search **лимита строк нет** — широкий фильтр на большой таблице может дать тяжёлую выгрузку и долгую суммаризацию. При ошибке эмбеддинга запроса ответ — **503** с фиксированным `detail` (в UI — error toast), а не пустой **200** с пояснением в `comment`: пустой результат нельзя отличить от «ничего не найдено».

<a id="заметка-сбой-llm-и-сбой-эмбеддера"></a>
### Заметка: сбой LLM и сбой эмбеддера · [↑](#toc)

**Агент:**

> Ошибки LLM по-прежнему возвращаются текстом в comment с кодом 200 — вы решили это не менять, и оба документа описывают такое поведение верно, но асимметрия с эмбеддингами теперь заметна.
>
> **Как сейчас работает LLM.**
> При summarize=true отзывы уже выбраны (а при экспорте — ещё и залиты в S3). Если провайдер падает, summarize_map_reduce не кидает исключение, а возвращает текст ошибки строкой. Маршрут кладёт её в comment и отвечает 200 — рядом остаются reviews или url/filename.
>
> **Чем это отличается от keywords.**
> При сбое эмбеддера выборки нет вообще: раньше приходил пустой 200, неотличимый от «ничего не найдено». Вы тогда сказали: если файл/данные есть — gr.Info нормален; если данных нет, а ошибка реальная — нужен gr.Error. Отсюда и 503 для семантики.
>
> **Что я имел в виду под «решили не менять».**
> По тому же критерию ошибка LLM обычно как раз «данные есть, сводки нет»: в UI всё ещё появляется Download, а текст про сбой модели — в Summary. Поэтому в этом коммите путь суммаризации не трогали — меняли только случай без данных.
>
> Если хотите симметрии (ошибка LLM → тоже 503 / gr.Error), это уже отдельное решение: при экспорте придётся либо не отдавать ссылку, либо отдавать 503 после успешной выгрузки.

**Вы:**

> Мне не кажется, что это асимметрия. Сырые данные без суммаризации при ее невозможности - разумный компромисс. В то время как винегрет из почти всегда нерелевантных данных не является разумным компромиссом при нерабочем эмбеддере: лучше ничего, чем всё подряд. Согласен?

**Агент:**

> Да, согласен — это не асимметрия, а разные ставки.
>
> При сбое LLM основной результат уже есть: отзывы выбраны по вашим фильтрам. Ответ «вот данные, сводки нет» — нормальный компромисс.
>
> При сбое эмбеддера семантический поиск не состоялся. Подсовывать обычную выборку (или пустой 200) — это уже другой запрос, часто с почти нерелевантным набором. Лучше явный отказ, чем «всё подряд под видом поиска по смыслу».

<a id="эмбеддинги"></a>
### Эмбеддинги · [↑](#toc)

| Этап | Когда | Как |
|------|-------|-----|
| Inline | `POST /reviews` | Обогащённый passage-текст + `mode=passage` |
| Backfill | Старт API (фон) | Строки без эмбеддинга; неудачный батч пропускается до следующего запуска |
| Build-index | Индекс HNSW отсутствует/битый, эмбеддинги на месте | `build-index` (~15–45+ мин, без re-embed) |
| Reindex | Вручную после смены формата/модели | `reindex --confirm` — TRUNCATE + полная пересборка (~1 ч на ~380K строк) |

```bash
docker exec bankiru-api python -m bankiru.embedder backfill
docker exec bankiru-api python -m bankiru.embedder build-index
docker exec bankiru-api python -m bankiru.embedder build-index --force  # удалить и пересоздать
docker exec bankiru-api python -m bankiru.embedder reindex          # dry-run
docker exec bankiru-api python -m bankiru.embedder reindex --confirm
```

**После деплоя изменений качества эмбеддингов** выполните `reindex --confirm` один раз — иначе старые векторы останутся в прежнем формате.

**Если reindex завершил embedding, но упал на создании индекса:** запустите `build-index`, а не `reindex --confirm` повторно.

<a id="суммаризация"></a>
### Суммаризация · [↑](#toc)

Map-reduce через Cloud.ru Foundation Models (OpenAI-совместимый API). Модель задаётся параметром `cloudModel` или `DEFAULT_CLOUD_MODEL`; в UI — выпадающий список **Summary model** (по умолчанию `<no summary>`, без LLM). В ответе API заголовок резюме — `**Summary model:**` плюс имя модели. При `summarize=true` эффективный интервал дат не длиннее **трёх календарных месяцев** (пустой `startDate` = самый ранний отзыв в БД, пустой `endDate` = самый поздний) — иначе **400** с `detail` про лимит (Nginx / localhost / Gradio одинаково; в UI — error toast). Пустая таблица при опущенных датах — **200** «no results»: границ нет, проверка длины не запускается. Ошибки провайдера LLM возвращаются текстом в `comment` с HTTP **200** (отзывы / ссылка на файл при этом остаются; порядок — select → summarize → upload) — намеренно, см. [Заметка: сбой LLM и сбой эмбеддера](#заметка-сбой-llm-и-сбой-эмбеддера).

<a id="веб-интерфейс-сервис-ui"></a>
### Веб-интерфейс (сервис `ui`) · [↑](#toc)

Gradio + FastAPI + Authentik OIDC. На хосте слушает только `127.0.0.1:17060`; снаружи — через Nginx (TLS).

**Макет:** три колонки — фильтры слева, формат/модель/кнопки в центре, панель **Summary** справа (аккордеон, 490 px, кнопка копирования).

| Элемент UI | Тип | Примечание |
|------------|-----|------------|
| Start / End | DateTime | Диапазон дат. Пустое поле = самый ранний / самый поздний отзыв в БД, независимо от выбора Summary model |
| Bank | Multi-select | 50 банков; по умолчанию «Сбербанк» |
| Product | Multi-select | 23 продукта |
| Location | Multi-select | 88 региональных центров; на сервере — `startswith` |
| **Semantic search** | Textbox (1 строка) | Параметр API: `keywords` |
| Format | Dropdown | csv / json / parquet / xlsx |
| Summary model | Dropdown | По умолчанию `<no summary>` (без LLM); иначе каталог Cloud.ru (загрузка **один раз при старте** контейнера). Пустые Start/End = самый ранний / самый поздний отзыв в БД и в этом виде участвуют в проверке «≤ 3 месяца» на API |
| Submit | Button | `GET /reviews`. Успех: Summary + URL; info «Download your file» только если есть URL экспорта. Ошибка API/сети: Gradio **error toast** с `detail`; URL и Summary очищаются |
| Download reviews | Button | Pre-signed URL в OBS (без round-trip на сервер) |
| Download summary | Button | Сохранение Markdown локально (Blob в браузере) |

Кнопки, выпадающие списки, поля ввода и аккордеон Summary — **прямоугольные** (тема Ocean, радиус 0; дополнительный CSS на случай, если токены темы не покрывают внутренний chrome Gradio).

<a id="безопасность-ui"></a>
### Безопасность UI · [↑](#toc)

Authentik (OIDC): фиксированный `OIDC_REDIRECT_URI`, RP-initiated logout с `id_token_hint`, сессия `{sub, username, email, id_token}`. На Nginx — HSTS, `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`, блокировка dotfile-запросов. Swagger на сервисе `ui` отключён.

**API:** порт `api` на хосте слушает только `127.0.0.1`. Внутри compose-сети UI вызывает `GET /reviews` без токена; с улицы через Nginx нужен гостевой или `API_TOKEN` (см. [`docs/bankiru-reviews-public-api.md`](docs/bankiru-reviews-public-api.md)).

<a id="инфраструктура-и-секреты"></a>
### Инфраструктура и секреты · [↑](#toc)

Docker Compose: один образ, три сервиса (`api`, `parser`, `ui`). Postgres, S3/OBS, Authentik и Infisical **не** входят в compose. В продакшене стек запускается только через `./scripts/start.sh` — секреты из Infisical в tmpfs (`/dev/shm/bankiru-reviews-secrets/.env`), на диск не пишутся и стираются при перезагрузке хоста.

<a id="наблюдаемость-и-расширяемость"></a>
### Наблюдаемость и расширяемость · [↑](#toc)

**Logfire:** имена сервисов `api`, `parser`, `ui`, `embedder`; auto-tracing — модули API и UI; парсер и embedder CLI — явные spans.

**Новый формат выгрузки:** подкласс `*Maker` в `handlers.py`; регистрация автоматическая через `inspect.getmembers` в `schemas.py`.

<a id="эксплуатация"></a>
### Эксплуатация · [↑](#toc)

- Расписание парсера можно менять без перезапуска контейнера (SIGHUP + `/app/.env` внутри контейнера) — см. [Changing the daily crawl schedule](#changing-the-daily-crawl-schedule).
- После изменений кода UI: `docker compose … build ui && up -d --force-recreate ui`.
- После изменений формата/модели эмбеддингов: `docker exec bankiru-api python -m bankiru.embedder reindex --confirm`.
- Если reindex завершил embedding, но упал на индексе: `build-index`, не повторный `reindex --confirm`.
- Полный список команд — в разделе [Day-2 operations](#day-2-operations).

---

<a id="краткое-описание-на-русском"></a>
## Краткое описание (на русском) · [↑](#toc)

**bankiru-reviews** — Docker-стек для автоматического сбора и анализа негативных отзывов клиентов российских банков с портала [banki.ru](https://www.banki.ru).

<a id="что-делает-система"></a>
### Что делает система · [↑](#toc)

- **Собирает** отзывы с оценками 1–2 звезды по 23 банковским продуктам (12 для физлиц, 11 для юрлиц) — ежедневно, по расписанию.
- **Хранит** отзывы в PostgreSQL с метаданными (дата, банк, продукт, город, URL) и отдельной таблицей pgvector-эмбеддингов для семантического поиска.
- **Выгружает** результаты в CSV, JSON, Parquet и XLSX (S3/OBS + pre-signed URL) либо отдаёт отзывы **inline** в JSON, если `outputFormat` не указан.
- **Суммаризирует** отзывы с помощью LLM (Cloud.ru Foundation Models, OpenAI-совместимый API) по схеме map-reduce; `summarize` по умолчанию выключен везде; при `summarize=true` эффективный интервал не длиннее трёх календарных месяцев (пустая граница = граница данных: самый ранний / самый поздний отзыв в БД; иначе **400** с фиксированным `detail` — в Gradio как error toast).
- **Ищет семантически** — встроенный векторный поиск на базе pgvector (BAAI/bge-m3, 1024 измерения, HNSW-индекс) позволяет находить отзывы по смыслу, а не только по ключевым словам.
- **Защищает доступ** — веб-UI через Authentik (OIDC); публичный `GET /reviews` — гостевой или привилегированный `API-Token` (см. [`docs/bankiru-reviews-public-api.md`](docs/bankiru-reviews-public-api.md)).

<a id="архитектура"></a>
### Архитектура · [↑](#toc)

Три сервиса в одном Docker Compose-стеке, собранные из единого образа:

| Сервис | Роль |
|--------|------|
| **api** | REST API (FastAPI): приём, хранение, фильтрация, inline JSON или выгрузка в S3, опциональная суммаризация; публичный `GET /reviews` по гостевому токену |
| **parser** | Ежедневный краулер banki.ru (APScheduler): сбор отзывов и отправка в API |
| **ui** | Веб-интерфейс (Gradio + FastAPI): фильтры, семантический поиск, скачивание файлов и резюме |

Внешние зависимости: PostgreSQL (с pgvector), S3/OBS, Authentik, Infisical.

<a id="ключевые-возможности"></a>
### Ключевые возможности · [↑](#toc)

- **Фильтрация** по дате, банку, продукту и городу (префиксный поиск).
- **Семантический поиск** — текстовый запрос кодируется моделью BGE-M3 и сравнивается с векторами отзывов через HNSW-индекс pgvector.
- **Четыре формата выгрузки** — CSV, JSON, Parquet, XLSX (с цветовой группировкой строк); без `outputFormat` — inline JSON в `reviews`.
- **LLM-суммаризация** — рекурсивный map-reduce, автоматический подбор размера чанков под контекстное окно модели (на публичном API по умолчанию выключена).
- **Автоматический бэкап** — вставленные строки мержатся в Parquet по дате отзыва (`datePublished`) в OBS.
- **Безопасность** — секреты в tmpfs (Infisical), OIDC для UI, гостевые токены для публичного REST, API на loopback, HSTS, блокировка dotfile-запросов на Nginx.
- **Наблюдаемость** — Logfire (OpenTelemetry): структурированные логи и трейсы для всех сервисов.
- **Расширяемость** — новый формат выгрузки добавляется одним классом-наследником `*Maker`; регистрация автоматическая.

<a id="технологии"></a>
### Технологии · [↑](#toc)

Python 3.13 · FastAPI · Gradio · SQLAlchemy · pgvector · pydantic-ai · tiktoken · APScheduler · httpx · aiobotocore · Authlib · Logfire · Docker · Nginx · Infisical

---

<a id="references"></a>
## References · [↑](#toc)

- [banki.ru](https://www.banki.ru/)
- [Cloud.ru Foundation Models](https://console.cloud.ru/spa/ml-foundation-models)
- [Authentik — OAuth2 provider docs](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)
- [Authentik — RP-initiated logout](https://docs.goauthentik.io/docs/users-sources/sources/protocols/oauth#rp-initiated-logout)
- [Infisical CLI](https://infisical.com/docs/cli/overview)
- [Infisical Universal Auth](https://infisical.com/docs/documentation/platform/identities/universal-auth)
- [APScheduler](https://apscheduler.readthedocs.io/)
- [httpx](https://www.python-httpx.org/)
- [tiktoken](https://github.com/openai/tiktoken)
- [pydantic-ai](https://ai.pydantic.dev/)
- [pgvector](https://github.com/pgvector/pgvector)
- [Logfire](https://logfire.pydantic.dev/)
- [Authlib](https://docs.authlib.org/)
- [Gradio](https://www.gradio.app/)
- [StyleFrame](https://styleframe.readthedocs.io/)
