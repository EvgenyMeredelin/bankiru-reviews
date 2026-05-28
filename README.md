<p align="center">
  <img src="assets/bankiru-reviews-logo.png" alt="bankiru-reviews logo" width="400">
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

## Table of contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Stack overview](#stack-overview)
- [Data model](#data-model)
- [API reference](#api-reference)
- [Output format handlers](#output-format-handlers)
- [Parser — crawl mechanics](#parser--crawl-mechanics)
- [Parser — request pacing and retry](#parser--request-pacing-and-retry)
- [Summarization — map-reduce pipeline](#summarization--map-reduce-pipeline)
- [UI and authentication](#ui-and-authentication)
- [Security hardening](#security-hardening)
- [Repository layout](#repository-layout)
- [Secrets — Infisical + tmpfs](#secrets--infisical--tmpfs)
- [Configuration reference](#configuration-reference)
- [Quick start](#quick-start)
- [Day-2 operations](#day-2-operations)
- [Описание проекта (на русском)](#описание-проекта-на-русском)
- [References](#references)

---

## Architecture at a glance

```mermaid
flowchart TD
    user(["User browser"]) -->|"HTTPS"| nginx["Nginx on host\n(TLS termination)"]

    subgraph stack [Docker Compose stack: bankiru-reviews]
      direction TB
      ui["ui\nGradio + OIDC"]
      api["api\nFastAPI"]
      parser["parser\nAPScheduler"]
      ui -->|"GET /reviews"| api
      parser -->|"POST /reviews"| api
    end

    nginx -->|"127.0.0.1:17060"| ui
    ui <-->|"OIDC"| authentik(("Authentik"))
    api -->|"SQLAlchemy"| postgres[("Postgres\nbankiru.reviews")]
    api -->|"put_object\npresigned_url"| s3[("S3 / OBS")]
    parser -->|"sequential HTTP/1.1\nbackoff + retry"| bankiruRu[("banki.ru")]
    infisical(("Infisical")) -.->|"secrets via tmpfs\n/dev/shm/...env"| stack
```

**Request path for a UI query:**

1. Browser → Nginx (TLS) → `ui` service (`127.0.0.1:17060`).
2. Gradio calls `GET /reviews` on the `api` service (internal compose network).
3. `api` queries Postgres, serialises to the requested format, uploads to S3, generates a pre-signed URL, runs the LLM summarizer, and returns a JSON body with `{url, comment}`.
4. UI renders the summary in the Markdown panel; the "Download reviews" button opens the pre-signed URL in a new browser tab — **the browser fetches the file directly from OBS**, no server round-trip. The "Download summary" button saves the summary as a local `.md` file (client-side Blob, no server round-trip).

---

## Stack overview

| Service | Image | Purpose |
|---------|-------|---------|
| `api` | Built from `./Dockerfile` (`python:3.13-slim`, uv) | FastAPI. Handles `POST /reviews` (insert), `GET /reviews` (filter + export + summarize), `DELETE /reviews` (by ID), `DELETE /reviews/by-date` (by date range), `DELETE /reviews/duplicates`. Every parser POST triggers a daily Parquet backup of the collected batch to S3 under `bankiru-reviews/`. |
| `parser` | Same image, `command: python -m bankiru.parser` | APScheduler cron job. Crawls banki.ru once daily, collects negative reviews for the previous `PARSER_DAYS` days, and POSTs the deduplicated batch to the `api`. |
| `ui` | Same image, `command: python -m bankiru.ui` | FastAPI + Gradio. OIDC-gated via Authentik (Authlib). Calls the `api` over the compose network. Bound to `127.0.0.1:17060` on the host; public access goes through Nginx. |
| External: Postgres | (managed elsewhere) | Sole persistent data store. One table: `bankiru.reviews`. Schema is created automatically at `api` startup via `Base.metadata.create_all`. |
| External: S3 / OBS | (managed elsewhere) | Stores named export files (pre-signed 1-hour URLs) and daily Parquet backups (`bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet`). |
| External: Authentik | `https://uva-advanced.ru` | OIDC identity provider for the UI login flow. |
| External: Infisical | `https://infisical.uva-advanced.ru` | Secrets store; `scripts/start.sh` pulls secrets into `/dev/shm` at boot. |

**Target environment:** Linux x86_64, Docker Engine, `docker compose` v2, Nginx on the host.

---

## Data model

One table: `bankiru.reviews` (PostgreSQL schema `bankiru`).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK | Auto-increment surrogate key. |
| `datePublished` | `DATETIME` | Publication timestamp extracted from banki.ru's JSON-LD structured data. Format: `YYYY-MM-DD HH:MM:SS`. |
| `reviewBody` | `TEXT` | Cleaned review text. HTML tags stripped (double pass — banki.ru sometimes HTML-encodes tags inside the body), emojis replaced via the `emoji` library, leading/trailing whitespace removed. |
| `bankName` | `TEXT` | Bank name from the `itemReviewed.name` field in JSON-LD. |
| `url` | `TEXT` | Canonical URL of the review's detail page on banki.ru (e.g. `https://www.banki.ru/services/responses/bank/response/123456/`). |
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

**Deduplication keys:** `(reviewBody, product)` — compared via `md5(reviewBody)` to keep the hash table small (32-byte strings vs full review bodies). Postgres uses `HashAggregate` for the full-table scan — no dedicated index needed. MD5 collisions on natural-language texts are negligible. The crawler deduplicates in-memory before POSTing. The `DELETE /reviews/duplicates` endpoint deduplicates the database table in place (keeps the row with the lowest `id`).

---

## API reference

Base URL (in compose): `http://api:1706`. Externally: `http://localhost:1706` (port published on all interfaces by default).

### `GET /healthz` — health probe

No auth. Returns `{"status": "ok"}`. Used by Docker's `healthcheck`.

### `POST /reviews` — insert reviews

**Auth:** `API-Token` header (matches `API_TOKEN`).

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

Inserts all rows, commits, then uploads the batch as a daily Parquet backup (`bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet`) to S3. Returns `201 Created` with no body.

### `GET /reviews` — filter, export, and summarize

**Auth:** None (read-only; the UI does not send the API token).

**Query parameters** (all optional):

| Parameter | Type | Description |
|-----------|------|-------------|
| `startDate` | `YYYYMMDD` or `YYYY-MM-DD` | Include reviews published on or after this date. Hyphens stripped automatically. |
| `endDate` | `YYYYMMDD` or `YYYY-MM-DD` | Include reviews published on or before this date. |
| `bankName` | repeatable string | Include only these bank names. Multiple values: `?bankName=Сбербанк&bankName=ВТБ`. Exact match. |
| `location` | repeatable string | Include only reviews whose `location` starts with one of the given prefixes. Useful for matching a city when the stored value includes district suffixes. |
| `product` | repeatable string | Exact match on `product`. |
| `outputFormat` | `csv` / `json` / `parquet` / `xlsx` | Default: `parquet`. |
| `cloudModel` | string | Override the summarization model. Default: `DEFAULT_CLOUD_MODEL`. |

**Successful response** (JSON):

```json
{
  "startDate": "2025-01-01",
  "endDate": "2025-01-31",
  "bankName": ["Сбербанк"],
  "product": null,
  "location": null,
  "outputFormat": "xlsx",
  "cloudModel": "anthropic/claude-sonnet-4.6",
  "filename": "a1b2c3d4-….xlsx",
  "url": "https://obs.cloud.ru/…?X-Amz-Expires=3600…",
  "comment": "**Cloud model:** `anthropic/claude-sonnet-4.6`\n\n## Наиболее острые темы…"
}
```

`url` is a **pre-signed S3 URL** (valid ~1 hour). `comment` contains the LLM summary in Markdown. If no reviews match, `url` and `filename` are `null` and `comment` holds a "no results" message.

### `DELETE /reviews` — delete by ID

**Auth:** `API-Token` header.

**Body:** JSON array of integer IDs, e.g. `[42, 43, 44]`. Deletes the matching rows and commits.

Returns `204 No Content`.

### `DELETE /reviews/by-date` — delete by date range

**Auth:** `API-Token` header.

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
  "http://localhost:1706/reviews/by-date?startDate=2026-05-17&endDate=2026-05-18"
```

### `DELETE /reviews/duplicates` — deduplicate the table

**Auth:** `API-Token` header.

Keeps the row with the lowest `id` per `(reviewBody, product)` pair (grouped by `md5(reviewBody)` to keep the hash table small). Deletes everything else. The query uses a CTE to materialise keeper IDs first, then performs an integer-only `NOT IN` delete. Postgres uses `HashAggregate` for the full-table scan — no dedicated index needed. A per-statement timeout of 300 s is set so a slow query surfaces as an error instead of hanging indefinitely.

**Response** (JSON): `{"deleted": <count>}`.

### `GET /` — redirect to docs

Redirects to `/docs` (Swagger UI). The API service exposes auto-docs; the UI service does not.

---

## Output format handlers

Handlers live in `src/bankiru/api/handlers.py`. Each format is a class that subclasses `ScalarsHandler` and ends with `Maker`:

| Class | Extension | MIME type |
|-------|-----------|-----------|
| `CSVMaker` | `.csv` | `text/csv` |
| `JSONMaker` | `.json` | `application/json` |
| `ParquetMaker` | `.parquet` | `application/vnd.apache.parquet` |
| `XlsxMaker` | `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |

**Registration is automatic.** `schemas.py` discovers all `*Maker` classes via `inspect.getmembers(handlers)` and builds `available_output_formats = {cls.extension: cls}`. Adding a new format means writing a subclass — no other file needs to change.

**Backup key:** `bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet` (daily batch, uploaded on POST). Named export keys: `<uuid4>.<extension>`.

**XLSX specifics:** Rows are colour-coded by review URL (alternating mint/pink per distinct URL) to visually group rows that belong to the same review. `reviewBody` is excluded from auto-fit. The `datePublished` column format (`YYYY-MM-DD HH:mm:ss`) is stamped directly via openpyxl after writing, because StyleFrame's per-row style merge is unreliable for `date_time_format`.

---

## Parser — crawl mechanics

**Products covered:** 24 banking products (12 retail + 12 business). Defined in `parser/settings.py` as a `{slug: label}` dict. Note: banki.ru uses both `corporate` and `legal` slugs for "Обслуживание юридических лиц" — both are crawled and the crawler's in-memory deduplication discards the duplicate bodies.

**Crawl loop (per product):**

1. Fetch the listing page: `GET /services/responses/list/product/{slug}/?page={n}&type=all&rate[]=1&rate[]=2` — the `rate[]=1&rate[]=2` filter restricts to 1-star and 2-star reviews (negatives only).
2. Extract review candidates from the listing HTML using two regexes:
   - `REVIEW_CONTENT_PATTERN` — matches inlined JSON-LD `Review` objects (strips out the fields we don't need: author, rating, postal address).
   - `REVIEW_URL_PATTERN` — matches the href of each review's detail-page link.
   Both iterators are zipped so content and URL stay aligned.
3. For each candidate in the date window `[start_date, end_date)`: fetch the detail page, extract the author's city from `<span class="l3a372298">…</span>` (`LOC_PATTERN`), and append the finished record. If the detail page fails, `location` is stored as `""` — the review is never dropped.
4. Stop paginating when the oldest review on the page predates `start_date` (`hit_left_boundary = True`) or the page contains no review markup at all (past the last page). Crucially, the crawler does **not** stop when `candidates` is empty: a page full of today's reviews (all newer than `end_date`) still needs to be paginated through to reach the date window.
5. After all products: deduplicate in-memory on `(reviewBody, product)` via pandas `drop_duplicates`.

**Date window:** `start_date = today(tz) - relativedelta(days=PARSER_DAYS)`, `end_date = today(tz)`, where `tz = ZoneInfo(PARSER_TIMEZONE)`. Both anchored at 00:00:00 in the configured timezone (via `dateutil.utils.today(tz)`), so the window is `[yesterday 00:00:00, today 00:00:00)` in `PARSER_TIMEZONE` time. This ensures the date window matches the cron schedule's timezone regardless of the container's system clock.

**Text cleaning pipeline** (`tools.py`):
1. `BeautifulSoup(...).text` — strip HTML tags.
2. Same again — banki.ru occasionally double-encodes HTML inside review bodies.
3. `emoji.replace_emoji` — remove emoji characters.
4. `str.strip` — trim whitespace.

---

## Parser — request pacing and retry

The client (`parser/client.py`) is deliberately **fully sequential** — one request at a time — to avoid triggering banki.ru's WAF.

### How `PARSER_*` timing parameters interact

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

### Putting it all together — a concrete example

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

### POST batch delivery

After the crawl completes, `runner.py` POSTs the collected reviews to the API (`CREATE_REVIEWS_ENDPOINT`). This POST uses a **separate** httpx client with a flat 600 s timeout (not the crawl client's split timeouts) — long enough to accommodate the daily Parquet backup that the API uploads after every insert. The POST retries **indefinitely** with exponential back-off capped at 60 s — after spending hours crawling, losing the batch to a transient API outage would be wasteful.

### Other implementation details

- `http2=False` — banki.ru's WAF silently drops TLS handshakes that advertise the `h2` ALPN.
- `max_connections=1, max_keepalive_connections=0` — enforces one connection at a time and no keepalive, matching the original parser's behaviour.
- `User-Agent` and `Accept-Language` are rotated per request from small realistic pools.
- Detail-page requests include a `Referer` header pointing to the listing page they were linked from.

**Warmup:** On `BankiruClient.__aenter__`, the client fetches `GET /` to seed the cookie jar before any product pages. If the warmup transport-errors, `run_once` logs the error and returns early — no point attempting 24 products against an unreachable origin.

**Typical run time:** 0.5–1.5 hours for a normal daily run (depends on review volume).

---

## Summarization — map-reduce pipeline

The summarizer (`api/summarizer.py`) handles arbitrarily large filter results: it chunks the corpus to fit the model's context window and recursively reduces partial summaries until the result is a single coherent text. Users never see a "context size exceeded" error.

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

**Error handling:** `ModelHTTPError` and `UsageLimitExceeded` are caught and returned as strings, so the API always returns a valid response body.

---

## UI and authentication

The UI service (`python -m bankiru.ui`) mounts a Gradio `Blocks` application inside a FastAPI app. The FastAPI layer handles OIDC; the Gradio layer handles the review query form.

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
| Start / End | DateTime | Date range filter (no time component). |
| Bank | Multi-select dropdown | 50 banks pre-loaded in `choices.py` (top-50 by complaint volume 2025). Default: `Сбербанк`. |
| Product | Multi-select dropdown | 23 banking product labels matching the parser's product catalog. |
| Location | Multi-select dropdown | 82 Russian regional capitals. Uses `startswith` matching on the server side. |
| Format | Single-select dropdown | `csv`, `json`, `parquet`, `xlsx`. Default: `parquet`. |
| Cloud model | Single-select dropdown | Populated from Cloud.ru `/models` API (TTL-cached 1 h); falls back to a hardcoded list if the API is unreachable. |
| Submit | Button | Calls `GET /reviews`, populates the Summary panel and stores the signed URL. |
| Clear | Button | Resets all inputs, the summary, and the hidden URL state. |
| Download reviews | Button | Client-side JS: opens the stored pre-signed URL in a new tab. No server round-trip. |
| Download summary | Button | Client-side JS: saves the summary Markdown panel content as a `.md` file. Filename matches the reviews file (same stem, `.md` extension). No server round-trip. |

**Session details:** Starlette `SessionMiddleware`, signed cookie `bankiru_session`, `same_site=lax`, `https_only=True`, 1-hour TTL. Session payload: `{sub, username, email, id_token}`.

### Registering the OIDC client in Authentik

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

## Security hardening

Eight defense-in-depth measures over a vanilla Authlib-on-Starlette template:

1. **Pinned `redirect_uri`** (`OIDC_REDIRECT_URI` from config, not derived from request headers). Defeats header-spoofed open-redirect / auth-code-injection scenarios if `TRUSTED_HOSTS` drifts.
2. **RP-initiated logout** — `/logout` reads `end_session_endpoint` from the OIDC discovery document and redirects with `id_token_hint` + `post_logout_redirect_uri`, terminating the Authentik SSO session (not just the local cookie).
3. **Session rotation** — `request.session.clear()` before writing identity on `/auth`.
4. **Narrow session payload** — only `{sub, username, email, id_token}` stored (not the full `userinfo` dict). Avoids 4 KB cookie corruption; limits what is base64-readable.
5. **UI auto-docs disabled** — `docs_url=None, redoc_url=None, openapi_url=None` on the UI FastAPI app. The `api` service keeps its Swagger docs (public contract).
6. **Explicit `OAuthError` handling** — authentication failures log a warning via Logfire and redirect to `/login` instead of surfacing a 500.
7. **Security headers at the Nginx edge** — HSTS (`max-age=31536000; includeSubDomains`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN` (not `DENY` — Gradio uses iframes internally), `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: geolocation=(), microphone=(), camera=()`.
8. **Dotfile blocking at the edge** — `location ~ /\.` returns 403 for any request containing a dotfile segment (`.env`, `.git/`, `.htaccess`, etc.); `access_log off; log_not_found off` silences scanner noise. A nested exception lets `/.well-known/` through for ACME challenges.

**Explicitly NOT done** (with rationale):

- **CSP**: incompatible with Gradio's inline scripts and `eval`.
- **Cookie encryption**: signing is sufficient given the narrowed payload.
- **`/login` rate limiting**: delegated to Authentik's brute-force protection.
- **HSTS preload**: opt in manually once long-term cert renewal is proven stable.

---

## Repository layout

```text
bankiru-reviews/
├── assets/
│   └── bankiru-reviews-logo.png    # logo; also served at /favicon.ico
├── config/
│   └── bankiru-reviews.conf        # Nginx vhost (TLS, ACME, Gradio SSE/WS proxy)
├── docker-compose.yml              # api + parser + ui; single shared env_file on tmpfs
├── Dockerfile                      # one image, three CMDs; uv, python:3.13-slim
├── pyproject.toml                  # single uv project; src layout; hatchling build
├── uv.lock                         # locked dependency tree
├── .env.example                    # canonical key list with all defaults shown
├── scripts/
│   └── start.sh                    # Infisical bootstrap → docker compose up
└── src/bankiru/
    ├── __init__.py                 # __version__ = "0.1.0"
    ├── config.py                   # Pydantic Settings; all env vars in one place
    ├── logging.py                  # configure_logfire() + install_auto_tracing()
    ├── db.py                       # async SQLAlchemy engine, session factory, create_all; statement_timeout
    ├── models.py                   # Review ORM (schema="bankiru"); indexes; review_columns list
    ├── api/
    │   ├── __main__.py             # uvicorn entry; auto-traces api.routes + api.handlers
    │   ├── app.py                  # FastAPI factory; lifespan runs create_all_tables
    │   ├── routes.py               # GET/POST/DELETE /reviews; GET /healthz
    │   ├── deps.py                 # DBSession, BotoClient, api_token type aliases
    │   ├── schemas.py              # Pydantic Request / Response / Review; format registry
    │   ├── handlers.py             # ScalarsHandler base; CSV/JSON/Parquet/XlsxMaker; asyncio.to_thread
    │   ├── summarizer.py           # summarize_map_reduce; tiktoken chunker; pydantic_ai
    │   ├── model_catalog.py        # Cloud.ru /models TTL cache; get_model_context()
    │   └── botocore_client.py      # aiobotocore async S3 client factory
    ├── parser/
    │   ├── __main__.py             # APScheduler entry; SIGHUP live reschedule
    │   ├── runner.py               # run_once(days=N); POST with unlimited retry
    │   ├── crawler.py              # BankiruCrawler; product/page/detail loop
    │   ├── client.py               # BankiruClient; randomised pacing; unlimited ban retry
    │   ├── settings.py             # PRODUCTS dict; regexes; UA/Accept-Language pools
    │   └── tools.py                # clean_text_pipe (double strip-tags → emoji → strip)
    └── ui/
        ├── __main__.py             # uvicorn entry; auto-traces ui.app + ui.blocks
        ├── app.py                  # FastAPI + SessionMiddleware + Authlib OIDC + Gradio
        ├── blocks.py               # Gradio Blocks; async get_reviews; Download JS
        ├── choices.py              # static BANKS / LOCATIONS / PRODUCTS / FILE_FORMATS
        └── foundation_models.py    # sync wrapper; TTL cache; fail-soft to hardcoded list
```

---

## Secrets — Infisical + tmpfs

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

## Configuration reference

All configuration is environment-driven via Pydantic Settings (`src/bankiru/config.py`). The same env file is shared by all three containers.

### Required (no defaults)

| Variable | Used by | Purpose |
|----------|---------|---------|
| `API_TOKEN` | api, parser | Shared secret on the `API-Token` header for `POST /reviews` and `DELETE /reviews`. |
| `POSTGRES_URL` | api | `postgresql+psycopg://user:pass@host/db`. |
| `OBS_BUCKET` | api | S3 bucket name. |
| `OBS_ACCESS_KEY` | api | S3 access key. |
| `OBS_SECRET_KEY` | api | S3 secret key. |
| `OBS_REGION` | api | S3 region. |
| `OBS_ENDPOINT` | api | S3-compatible endpoint URL (e.g. `https://s3.cloud.ru`). |
| `SESSION_MIDDLEWARE_SECRET` | ui | Signs Starlette session cookies. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `OIDC_CLIENT_ID` | ui | Authentik OAuth2 client ID. |
| `OIDC_CLIENT_SECRET` | ui | Authentik OAuth2 client secret. |

### Optional (shown with defaults)

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGFIRE_TOKEN` | `None` | Logfire ingestion token. Omit for local dev (no-op). |
| `OBS_BACKUP_PREFIX` | `bankiru-reviews` | S3 key prefix (subfolder) for daily Parquet backups. Files are written as `{prefix}/bankiru-reviews-YYYY-MM-DD.parquet`. |
| `API_PORT` | `1706` | API listen port. If changed, also update `CREATE_REVIEWS_ENDPOINT` and `GET_REVIEWS_URL`. |
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
| `DEFAULT_CLOUD_MODEL` | `anthropic/claude-sonnet-4.6` | Default summarization model. |
| `OUTPUT_TOKENS_LIMIT` | `50000` | Per-call output token cap (clipped to `max_model_len // 4` for small-context models). |
| `DEFAULT_MODEL_CONTEXT` | `200000` | Fallback context window when the Cloud.ru `/models` catalog is unreachable. |
| `SUMMARIZER_MAP_CONCURRENCY` | `4` | Maximum concurrent LLM calls in the map pass. |
| `SUMMARIZER_SAFETY_MARGIN_TOKENS` | `512` | Slack subtracted from the input budget each pass. |
| `SUMMARIZER_MAX_PASSES` | `4` | Hard ceiling on map-reduce recursion depth. If exceeded, partial summaries are joined verbatim. |
| `TRUSTED_HOSTS` | `*` | Upstreams whose `X-Forwarded-*` headers `ProxyHeadersMiddleware` trusts. Safe as `*` because the container is bound to `127.0.0.1`. |
| `OIDC_DISCOVERY_URL` | Authentik well-known URL | OIDC discovery document endpoint. |
| `OIDC_REDIRECT_URI` | `None` (falls back to `url_for`) | Must exactly match the Redirect URI registered in Authentik. Set in production. |
| `OIDC_POST_LOGOUT_URI` | `None` (falls back to `/`) | Must exactly match a Post-Logout Redirect URI in Authentik. |
| `AWS_REQUEST_CHECKSUM_CALCULATION` | `when_required` | Botocore compat flag for non-AWS S3. Set in environment, not as a Pydantic field. |
| `AWS_RESPONSE_CHECKSUM_VALIDATION` | `when_required` | Same. |

**Port coupling note:** `API_PORT`, `CREATE_REVIEWS_ENDPOINT`, and `GET_REVIEWS_URL` must agree on the same port. The `.env.example` comment flags this explicitly.

---

## Quick start

### Production (with Infisical)

```bash
# 1. Install the Infisical CLI (once per host)
curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | sudo -E bash
sudo apt-get update && sudo apt-get install -y infisical

# 2. Clone
git clone <repo-url> ~/git/bankiru-reviews
cd ~/git/bankiru-reviews

# 3. Provision the Nginx vhost on the host (not in Docker)
sudo cp config/bankiru-reviews.conf /etc/nginx/conf.d/bankiru.conf
sudo certbot certonly --webroot -w /var/www/html \
     -d bankiru.uva-advanced.ru -d www.bankiru.uva-advanced.ru
sudo nginx -t && sudo systemctl reload nginx

# 4. Register the OIDC client in Authentik (see "UI and authentication" above);
#    populate OIDC_CLIENT_ID / OIDC_CLIENT_SECRET in Infisical.

# 5. Start the stack (prompts for the Infisical client secret)
./scripts/start.sh

# 6. Verify
docker compose ps
curl http://localhost:1706/healthz   # {"status": "ok"}
open https://bankiru.uva-advanced.ru
```

### `start.sh` flags

```bash
./scripts/start.sh               # fetch secrets + docker compose up -d
./scripts/start.sh --no-start    # fetch secrets only (skip compose up)
./scripts/start.sh --refresh     # re-fetch secrets + force-recreate api/parser/ui
```

Pass the client secret non-interactively:

```bash
INFISICAL_CLIENT_SECRET=… ./scripts/start.sh
```

### Local development (without Docker)

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
from bankiru.logging import configure_logfire; configure_logfire('bankiru-parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once())
"
```

Trigger a one-off crawl for a specific date range (e.g. May 17–18):

```bash
uv run python -c "
from bankiru.logging import configure_logfire; configure_logfire('bankiru-parser')
import asyncio; from bankiru.parser.runner import run_once
asyncio.run(run_once(start_date='2026-05-17', end_date='2026-05-19'))
"
```

> **Note:** `end_date` is *exclusive* (the crawl window is `[start_date, end_date)`).
> To collect reviews published on May 17 and May 18, set `end_date` to May 19.

---

## Day-2 operations

```bash
# Stream logs
docker logs -f bankiru-api
docker logs -f bankiru-parser
docker logs -f bankiru-ui

# Pick up rotated secrets + recreate containers
./scripts/start.sh --refresh

# One-off parser run (backfill yesterday)
docker exec bankiru-parser python -c "
from bankiru.logging import configure_logfire; configure_logfire('bankiru-parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once())"

# Backfill the last 7 days
docker exec bankiru-parser python -c "
from bankiru.logging import configure_logfire; configure_logfire('bankiru-parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once(days=7))"

# Crawl a specific date range (e.g. May 17–18; end_date is exclusive)
docker exec bankiru-parser python -c "
from bankiru.logging import configure_logfire; configure_logfire('bankiru-parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once(start_date='2026-05-17', end_date='2026-05-19'))"

# Delete reviews for a date range (e.g. May 17–18; both dates inclusive)
curl -s -X DELETE -H "API-Token: $API_TOKEN" \
  "http://localhost:1706/reviews/by-date?startDate=2026-05-17&endDate=2026-05-18"

# Deduplicate the database (keeps lowest id per reviewBody+product)
curl -s -X DELETE -H "API-Token: $API_TOKEN" http://localhost:1706/reviews/duplicates

# Postgres backup (external DB)
pg_dump "$POSTGRES_URL" --no-owner --no-acl | gzip > backup-$(date +%F).sql.gz

# S3 backup is automatic — every POST /reviews uploads the daily batch
# as bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet in the OBS bucket.
```

### Changing the daily crawl schedule

The parser reads `PARSER_CRON_HOUR` and `PARSER_CRON_MINUTE` at startup and also reloads them on **SIGHUP** ([`__main__.py`](src/bankiru/parser/__main__.py)).

#### Option A — SIGHUP (safe while a crawl is running)

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

#### Option B — Container restart (kills any running crawl)

Update the values in Infisical (or edit `/dev/shm/bankiru-reviews-secrets/.env` on the host), then restart:

```bash
# Minimal restart — only the parser; api and ui stay up
docker compose restart parser
```

The scheduler's `replace_existing=True` ensures the new trigger cleanly replaces the old one.

> **⚠️ If a manual `docker exec` crawl is running:**
>
> - `docker compose restart parser` stops and restarts the container. The `docker exec` process **is killed** because all processes in the container's PID namespace are terminated.
> - `./scripts/start.sh --refresh` runs `docker compose up -d --build --force-recreate`, which **destroys and recreates** the container — any `docker exec` process is killed as well.
>
> Use **Option A** (SIGHUP) to reschedule without interrupting a running crawl.

---

## Описание проекта (на русском)

- Назначение системы — централизованный мониторинг негативных отзывов и претензий к российским банкам, публикуемых на портале banki.ru (оценки 1–2 звезды).
- Источник данных — banki.ru; рубрикатор покрывает 24 банковские услуги для физических и юридических лиц. Парсер ежедневно собирает все отзывы за истекшие `PARSER_DAYS` суток (по умолчанию 1) и пакетом отправляет их в API.
- Стратегия запросов — намеренно простая и последовательная: один запрос за раз, случайная пауза `uniform(PARSER_SLEEP_MIN, PARSER_SLEEP_MAX)` (по умолчанию 10–20 с, ~4 запроса/мин). Непредсказуемый тайминг имитирует поведение пользователя. Раздельные таймауты на соединение и чтение. Ошибки соединения (вероятный бан WAF) ретраятся бесконечно с экспоненциальным back-off. HTTP/2 отключён (WAF banki.ru блокирует ALPN `h2`).
- API предоставляет четыре эндпоинта: `POST /reviews` (приём от парсера, требует `API-Token`), `GET /reviews` (фильтрация и выгрузка, публичный), `DELETE /reviews` (удаление по ID, требует `API-Token`), `DELETE /reviews/duplicates` (дедупликация по `reviewBody` + `product`, требует `API-Token`).
- Поддерживаются фильтры по диапазону дат, банкам (точное совпадение), банковским услугам (точное совпадение) и городам авторов жалоб (префиксный поиск через `startswith`).
- Поддерживаются форматы выгрузки `csv`, `json`, `parquet` и `xlsx`; ссылка на скачивание возвращается в виде pre-signed URL объектного хранилища. После каждого добавления записей (POST от парсера) API автоматически сохраняет ежедневную резервную копию собранного пакета (`bankiru-reviews/bankiru-reviews-YYYY-MM-DD.parquet`) в объектном хранилище.
- Суммаризация выполняется LLM-моделью (Cloud.ru Foundation Models, OpenAI-совместимый протокол) по схеме map-reduce: пользователь получает связное резюме при любом количестве отзывов. Результат — всегда строка; ошибки провайдера отображаются как текст.
- Веб-интерфейс реализован на Gradio; работает поверх FastAPI. Кнопка «Download reviews» открывает pre-signed URL в новой вкладке браузера — файл скачивается напрямую из OBS без промежуточного сервера. Кнопка «Download summary» сохраняет содержимое панели Summary в виде `.md`-файла (клиентский Blob, без обращения к серверу).
- Авторизация — через Authentik (OIDC) с фиксированным `redirect_uri`, корректным RP-initiated logout и заголовками безопасности (HSTS, X-Frame-Options, Referrer-Policy) на уровне Nginx.
- Инфраструктура — Docker Compose, единый образ, три сервиса (`api`, `parser`, `ui`); внешние зависимости (Postgres, S3, Authentik, Infisical) в стеке не разворачиваются.
- Наблюдаемость — Logfire spans + `install_auto_tracing` для API и UI; парсер использует явные spans и `logfire.info()`.
- Управление секретами — Infisical Universal Auth → tmpfs (`/dev/shm`); секреты не записываются на SSD и стираются при перезагрузке хоста.
- Расширяемость форматов выгрузки — новый формат добавляется подклассом `ScalarsHandler` с суффиксом `Maker` в `handlers.py`; регистрация автоматическая через `inspect.getmembers`.

---

## References

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
- [Logfire](https://logfire.pydantic.dev/)
- [Authlib](https://docs.authlib.org/)
- [Gradio](https://www.gradio.app/)
- [StyleFrame](https://styleframe.readthedocs.io/)
