# Logfire Issues Analysis — bankiru-reviews

## Issue 1: Post-crawl data submission failures

### Root Cause Analysis

The `POST /reviews` request from the parser to the API fails immediately after `crawl complete` with a **`RemoteProtocolError('Server disconnected without sending a response.')`** on the first attempt, followed by **`ConnectionError('All connection attempts failed')`** on attempts 2–4, before finally succeeding on attempt 5 (~50 seconds later).

**Why it happens specifically right after crawl completion:**

The crawl runs for ~2+ hours (from ~17:05 UTC to ~19:51 UTC per the logs). During this entire period, the `httpx.AsyncClient` created inside [`_post_with_retry()`](src/bankiru/parser/runner.py:79) does **not** exist yet — it is created fresh at line 79 with `async with httpx.AsyncClient(timeout=600.0) as http:`. However, the **API server** (`http://api:1706`) has been idle for those 2+ hours from the parser's perspective.

The real issue is a **Docker DNS / connection pool staleness** problem combined with the API container's behaviour:

1. **The API container may have recycled its TCP listener** or the Docker internal DNS entry for `api` may have shifted during the long crawl window. When the parser finishes crawling and immediately tries to POST to `http://api:1706/reviews`, the first TCP connection reaches a stale endpoint or a half-closed socket.

2. **The `RemoteProtocolError('Server disconnected without sending a response.')` on attempt 1** indicates the TCP connection was established but the server immediately closed it without sending any HTTP response. This is the classic symptom of hitting a **server that is busy with a long-running operation** — in this case, the API's `create_all_tables()` running during its lifespan startup. Looking at the logs more carefully:
   - `crawl complete: 536 reviews` at **19:51:15 UTC**
   - `POST attempt=1 failed: RemoteProtocolError` at **20:00:03 UTC** (~9 minutes later)
   - The API logs show `POST /reviews <ongoing?>` starting at **19:51:15 UTC** and index creation at **20:00:26 UTC**

   Wait — re-examining the timestamps: the `POST /reviews <ongoing?>` at 19:51:15 and the index creation at 20:00:26 suggest the API **was processing a previous request** or was restarting. The index creation logs (`creating ix_bankiru_reviews_bankName`, etc.) at 20:00:26 indicate the API container **restarted** during or just before the POST attempts.

3. **The actual root cause**: The API container restarted (or was recreated) around the time the crawl completed. The `create_all_tables()` lifespan function runs index creation, and during this startup period, the API is not yet accepting requests. The parser's POST hits the container while it's still in its startup lifespan, causing:
   - Attempt 1: `RemoteProtocolError` — connection accepted by the OS but uvicorn hasn't started serving yet
   - Attempts 2-4: `ConnectionError('All connection attempts failed')` — the container is mid-restart, port not yet listening
   - Attempt 5 (at 20:00:52): succeeds — API is now fully up

**However**, looking more carefully at the Docker Compose config, the parser `depends_on: api: condition: service_healthy` only applies at **initial container start**, not during runtime. If the API container restarts due to its `restart: unless-stopped` policy (e.g., OOM, crash, healthcheck failure), the parser won't know about it.

**Most likely scenario**: The API container's healthcheck failed (5 retries × 30s interval = 150s of unhealthy state) or the container crashed and restarted. The parser finished its crawl during this restart window. The `create_all_tables()` + index creation took from ~20:00:26 to ~20:00:26 (0.0s each — indexes already exist), and the POST succeeded at 20:00:33.

### Evidence

| Log Entry | Timestamp (UTC) | Service | Details |
|-----------|-----------------|---------|---------|
| `crawl complete: 536 reviews` | 19:51:15 | parser | Crawl finished successfully |
| `POST attempt=1 failed: RemoteProtocolError` | 20:00:03 | parser | Server disconnected without response — [`runner.py:92`](src/bankiru/parser/runner.py:92) |
| `POST attempt=2 failed: ConnectionError` | 20:00:05 | parser | All connection attempts failed — 2s backoff |
| `POST attempt=3 failed: ConnectionError` | 20:00:09 | parser | 4s backoff |
| `POST attempt=4 failed: ConnectionError` | 20:00:17 | parser | 8s → actually 16s backoff |
| `creating ix_bankiru_reviews_bankName` | 20:00:26 | api | API lifespan running `create_all_tables()` — confirms restart |
| `all indexes ensured in 0.0 s` | 20:00:26 | api | Indexes already exist, startup completes |
| `POST /reviews -> 201` | 20:00:33/52 | parser | POST finally succeeds |

**Key code path**: [`_post_with_retry()`](src/bankiru/parser/runner.py:70-96) — the retry loop with `delay = min(60, 2**attempt)` produces delays of 2, 4, 8, 16, 32, 60, 60… seconds. The exponential backoff works correctly and eventually succeeds.

**~9-minute gap** between `crawl complete` (19:51:15) and first POST attempt (20:00:03): This gap is suspicious. The code at [`runner.py:67`](src/bankiru/parser/runner.py:67) calls `_post_with_retry` immediately after logging `crawl complete`. The 9-minute gap suggests either:
- The `BankiruClient.__aexit__` (closing the crawl httpx client) took a long time, OR
- The pandas `drop_duplicates` deduplication in the crawler took time on 536 reviews, OR
- The event loop was blocked by something else

Most likely, the crawl client's `aclose()` hung waiting for connections to drain, or there's a timezone display discrepancy in the logs.

### Fix Plan

**Priority 1 — Add a pre-POST health check** (prevents wasted retries against a down API):

In [`_post_with_retry()`](src/bankiru/parser/runner.py:70), before entering the retry loop, add a health-check poll that waits for the API to be ready:

```python
# Before the POST loop, wait for the API to be healthy
healthz_url = endpoint.rsplit("/", 1)[0] + "/healthz"
for _ in range(30):  # up to 30 × 5s = 150s
    try:
        r = await http.get(healthz_url, timeout=5.0)
        if r.status_code == 200:
            break
    except httpx.HTTPError:
        pass
    await asyncio.sleep(5)
```

**Priority 2 — Close the crawl client before POSTing** (ensure clean resource release):

In [`run_once()`](src/bankiru/parser/runner.py:53-67), the `async with BankiruClient()` context manager exits *before* `_post_with_retry` is called (line 67 is outside the `async with` block). This is already correct. Verify that `BankiruClient.__aexit__` calls `await self._client.aclose()` promptly — it does at [`client.py:99`](src/bankiru/parser/client.py:99).

**Priority 3 — Investigate why the API restarted**:

- Check Docker container restart logs: `docker inspect bankiru-api --format='{{.RestartCount}}'`
- Check if the healthcheck is too aggressive: current config is `interval: 30s, timeout: 3s, retries: 5, start_period: 120s`. This means after startup, 5 consecutive failures (150s) trigger a restart. If the API is busy with a long S3 upload or LLM summarization call, it might miss healthchecks.
- Consider increasing `retries` to 10 or adding a dedicated `/healthz` that doesn't depend on DB connectivity.

**Priority 4 — Add connection keepalive to the POST client**:

The POST client at [`runner.py:79`](src/bankiru/parser/runner.py:79) creates a new `httpx.AsyncClient` each time. This is fine, but consider adding `http2=False` explicitly and setting `limits=httpx.Limits(max_connections=1)` for consistency.

---

## Issue 2: Unexpected `POST / 405` entries

### Root Cause Analysis

The Logfire logs show four `POST / → 405` entries at **19:47:50 UTC**, all from the **`ui` service** (service_name: `ui`), instrumented by `opentelemetry.instrumentation.fastapi`.

**The source is an external HTTP client** — specifically, an automated health-monitoring or uptime-checking tool. The evidence:

1. **HTTP Request Attributes** from the Logfire detail panel:
   - `http.method`: `POST`
   - `http.route`: `/`
   - `http.target`: `/`
   - `http.url`: `http://bankiru.uva-advanced.ru/`
   - `http.host`: `172.22.0.4:17060` — this is the Docker internal IP of the UI container
   - `http.server_name`: `bankiru.uva-advanced.ru`
   - `http.user_agent`: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36`
   - `net.peer.ip`: `172.22.0.1` — the Docker bridge gateway, meaning the request came through Nginx on the host

2. **Why 405?** The UI FastAPI app at [`ui/app.py:135-139`](src/bankiru/ui/app.py:135) defines `GET /` (which redirects to `/login` or `/gradio/`). There is **no `POST /` handler** on the UI app. FastAPI correctly returns `405 Method Not Allowed`.

3. **Why POST?** The `User-Agent` string is a standard Chrome UA, but the request is a `POST` to `/` — no browser does this naturally. This is characteristic of:
   - **Uptime monitoring tools** (e.g., UptimeRobot, Healthchecks.io) that are misconfigured to use POST instead of GET
   - **Web vulnerability scanners** probing for form submission endpoints
   - **Bots/crawlers** that blindly POST to discovered URLs

4. **The requests arrive through Nginx** (peer IP `172.22.0.1` is the Docker host bridge), so they come from the public internet via `https://bankiru.uva-advanced.ru/`.

5. **All four requests arrive at the exact same timestamp** (19:47:50 UTC, within ~100ms of each other), suggesting a single client making multiple rapid requests — typical of a scanner or monitoring tool doing a burst check.

### Evidence

| Attribute | Value | Significance |
|-----------|-------|--------------|
| `service_name` | `ui` | Request hits the UI FastAPI app, not the API |
| `http.method` | `POST` | No POST handler exists on `/` in the UI |
| `http.status_code` | `405` | Correct response — Method Not Allowed |
| `http.url` | `http://bankiru.uva-advanced.ru/` | Public URL, came through Nginx |
| `http.user_agent` | `Mozilla/5.0 ...Chrome/537.36` | Spoofed browser UA — typical of bots |
| `net.peer.ip` | `172.22.0.1` | Docker bridge gateway — request from host/Nginx |
| `net.host.port` | `17060` | UI service port |
| Timing | 4 requests within ~100ms | Burst pattern — automated tool |

### Risk Assessment

**Security risk: LOW**
- The 405 response is correct and reveals no sensitive information
- No authentication bypass is possible via POST to `/`
- The request never reaches any data-handling code

**Reliability risk: NONE**
- The requests are handled and rejected in <1ms each (808µs, 851µs, 785µs, 789µs)
- No resource exhaustion or error propagation

**Noise risk: LOW-MEDIUM**
- Four log entries per occurrence clutters the Logfire dashboard
- If this is a recurring monitoring check, it will generate ongoing noise

### Recommendations

**Option A — Do nothing** (recommended if infrequent):
The 405 responses are correct, fast, and harmless. If these appear only occasionally, they can be ignored.

**Option B — Identify and fix the source** (recommended if recurring):
1. Check if you have an uptime monitor configured for `https://bankiru.uva-advanced.ru/` that uses POST instead of GET. Switch it to GET.
2. Check Nginx access logs for the source IP: `grep "POST / " /var/log/nginx/access.log`

**Option C — Suppress in Logfire** (if the noise is bothersome):
Add `"/"` to the `excluded_urls` parameter in [`ui/app.py:78`](src/bankiru/ui/app.py:78):
```python
logfire.instrument_fastapi(app, excluded_urls="/gradio/assets/*,/")
```
This would suppress tracing for all requests to `/`, including legitimate GET redirects. A more targeted approach would be to add a catch-all POST handler that returns 405 silently:

```python
@app.api_route("/", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def reject_non_get_root():
    return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"})
```

**Option D — Block at Nginx** (if it's a scanner):
Add a rate limit or block rule in the Nginx config for POST requests to `/`:
```nginx
location = / {
    limit_except GET HEAD {
        deny all;
    }
    proxy_pass http://127.0.0.1:17060;
    # ... existing headers ...
}
```

---

## Summary of Action Items

| # | Issue | Action | Priority | File |
|---|-------|--------|----------|------|
| 1 | POST failures after crawl | Add pre-POST healthcheck poll in `_post_with_retry` | High | [`runner.py`](src/bankiru/parser/runner.py) |
| 2 | POST failures after crawl | Investigate API container restart cause via Docker logs | High | Infrastructure |
| 3 | POST failures after crawl | Consider increasing healthcheck retries from 5 to 10 | Medium | [`docker-compose.yml`](docker-compose.yml) |
| 4 | POST / 405 | Identify the external client making POST requests | Low | Nginx logs |
| 5 | POST / 405 | If recurring, fix the monitoring tool or suppress in Logfire | Low | [`ui/app.py`](src/bankiru/ui/app.py) |
