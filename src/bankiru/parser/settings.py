"""Static parser settings: product catalog, URLs, regexes, base headers, UA/Accept-Language pools.

This module contains all the constants that the parser needs to scrape banki.ru.
Nothing here is configurable at runtime (unlike config.py settings) — these are
structural properties of the banki.ru website that only change when banki.ru
redesigns its pages.

If banki.ru changes its HTML structure, the regex patterns here will need to be
updated. The product catalog may also need updating if banki.ru adds or removes
product categories.

Connection to other modules:
  - bankiru.parser.client   — imports BASE_HEADERS, USER_AGENTS, ACCEPT_LANGUAGES,
                              BASE_URL for HTTP requests
  - bankiru.parser.crawler  — imports PRODUCTS, PAGE_URL, REVIEW_URL_PATTERN,
                              REVIEW_CONTENT_PATTERN, LOC_PATTERN for page parsing
"""

from __future__ import annotations

import re

# ── Product catalog ──────────────────────────────────────────────────────────
# Maps banki.ru URL slugs to human-readable Russian product labels.
# The keys are used in the PAGE_URL template to construct listing page URLs.
# The values become the `product` field in the Review database record.
#
# The crawler iterates through ALL products in this dict on every run,
# so adding a new product here automatically includes it in future crawls.
PRODUCTS: dict[str, str] = {
    # ── Retail banking products (for individuals) ────────────────────────
    "autocredits":         "Автокредит",
    "deposits":            "Вклад",
    "debitcards":          "Дебетовая карта",
    "transfers":           "Денежный перевод",
    "remote":              "Дистанционное обслуживание физических лиц",
    "hypothec":            "Ипотека",
    "creditcards":         "Кредитная карта",
    "mobile_app":          "Мобильное приложение",
    "individual":          "Обслуживание физических лиц",
    "credits":             "Потребительский кредит",
    "restructing":         "Реструктуризация/рефинансирование",
    "other":               "Другое (физические лица)",
    # ── Business banking products (for legal entities) ───────────────────
    "bank_guarantee":      "Банковская гарантия",
    "businessdeposits":    "Депозит",
    "business_remote":     "Дистанционное обслуживание юридических лиц",
    "salary_project":      "Зарплатный проект",
    "businesscredits":     "Кредитование бизнеса",
    "leasing":             "Лизинг",
    "business_mobile_app": "Мобильное приложение для бизнеса",
    # banki.ru uses both "corporate" and "legal" slugs for the same product;
    # keeping both ensures no reviews are missed. Duplicates are dropped by
    # the crawler's `_deduplicated()` pass.
    "corporate":           "Обслуживание юридических лиц",
    "legal":               "Обслуживание юридических лиц",
    "rko":                 "Расчетно-кассовое обслуживание",
    "acquiring":           "Эквайринг",
    "business_other":      "Другое (юридические лица)",
}

# ── URL templates ────────────────────────────────────────────────────────────
# Base URL for all banki.ru requests (including warmup and detail pages).
BASE_URL = "https://www.banki.ru"

# Listing page URL template. Parameters:
#   {base}    — BASE_URL
#   {product} — URL slug from PRODUCTS dict (e.g. "creditcards")
#   {page}    — page number (1-based, incremented by the crawler)
# Query params:
#   type=all       — include all review types (not just "official responses")
#   rate[]=1&rate[]=2 — only negative reviews (ratings 1 and 2 out of 5)
PAGE_URL = (
    "{base}/services/responses/list/product/{product}/"
    "?page={page}&type=all&rate[]=1&rate[]=2"
)

# ── Regex patterns for HTML parsing ─────────────────────────────────────────
# These patterns extract structured data from banki.ru's HTML pages.
# They are fragile by nature (tied to the site's HTML structure) and will
# need updating if banki.ru redesigns its pages.

# Matches the review detail page URL in listing page HTML.
# Captures the path portion (e.g. "/services/responses/bank/response/12345")
# which is prepended with BASE_URL to form the full detail page URL.
REVIEW_URL_PATTERN = re.compile(
    r"(?:<a href=\")(/services/responses/bank/response/\d+)(?:/\" data)"
)

# Matches the JSON-LD structured data block embedded in listing page HTML.
# banki.ru embeds Schema.org Review objects in the page source. This regex
# captures specific fields via capture groups while skipping others:
#   Group 1: opening brace "{"
#   Group 2: datePublished + reviewBody
#   Group 3: opening of itemReviewed object
#   Group 4: bank name (itemReviewed.name)
#   Group 5: closing braces
# The captured groups are concatenated and parsed as JSON by the crawler.
# Non-capturing groups (?:...) skip fields we don't need (author, rating,
# address, telephone) to keep the JSON minimal.
REVIEW_CONTENT_PATTERN = re.compile(
    r"(\{)(?: \"@type\":\"Review\", "
    r"\"author\":\"[^\"]*\", )"
    r"(\"datePublished\":\"[^\"]*\", "
    r"\"reviewBody\":\"[^\"]*\", )"
    r"(?:\"name\":\"[^\"]*\", "
    r"\"reviewRating\": "
    r"\{ \"@type\":\"Rating\", "
    r"\"bestRating\":\"[^\"]*\", "
    r"\"ratingValue\":\"[^\"]*\", "
    r"\"worstRating\":\"[^\"]*\" \}, )"
    r"(\"itemReviewed\": "
    r"\{)(?: \"@type\":\"BankOrCreditUnion\", )"
    r"(\"name\":\"[^\"]*\")(?:, "
    r"\"telephone\":\"[^\"]*\", "
    r"\"address\": "
    r"\{ \"@type\":\"PostalAddress\", "
    r"\"streetAddress\":\"[^\"]*\", "
    r"\"addressCountry\":\"[^\"]*\", "
    r"\"postalCode\":\"[^\"]*\" \})( \} \})"
)

# Matches the author's city on the review detail page.
# The CSS class "l3a372298" is a minified class name that may change on
# site redesigns. Captures the text content of the span (the city name).
LOC_PATTERN = re.compile(r"(?:<span class=\"l3a372298\">)([^<]+)(?:</span>)")

# ── Browser fingerprint pools ────────────────────────────────────────────────
# Realistic desktop User-Agent strings. Rotated per request by the client
# to make traffic blend in with normal browser visits and to avoid trivial
# fingerprinting by banki.ru's WAF (Web Application Firewall).
# Includes Chrome, Firefox, Edge, and Yandex Browser variants.
USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 YaBrowser/25.2.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
)

# Accept-Language header pool. All variants prefer Russian (ru) as the
# primary language, which is appropriate for a Russian-language website.
# Rotated per request alongside User-Agent for fingerprint diversity.
ACCEPT_LANGUAGES: tuple[str, ...] = (
    "ru,en;q=0.9",
    "ru-RU,ru;q=0.9,en;q=0.7",
    "ru,en-US;q=0.8,en;q=0.6",
)

# ── Base HTTP headers ────────────────────────────────────────────────────────
# These headers are sent with every request and mimic a real desktop browser.
# They are set once on the httpx client (client.py) and merged with the
# per-request User-Agent and Accept-Language headers.
# The Sec-Fetch-* headers are part of the Fetch Metadata specification and
# tell the server this is a top-level navigation request from a browser.
BASE_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
