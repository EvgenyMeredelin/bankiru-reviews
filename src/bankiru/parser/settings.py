"""Static parser settings: product catalog, URLs, regexes, base headers, UA/Accept-Language pools."""

from __future__ import annotations

import re

PRODUCTS: dict[str, str] = {
    # услуги для физических лиц
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
    # услуги для юридических лиц
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

BASE_URL = "https://www.banki.ru"
PAGE_URL = (
    "{base}/services/responses/list/product/{product}/"
    "?page={page}&type=all&rate[]=1&rate[]=2"
)

REVIEW_URL_PATTERN = re.compile(
    r"(?:<a href=\")(/services/responses/bank/response/\d+)(?:/\" data)"
)

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

LOC_PATTERN = re.compile(r"(?:<span class=\"l3a372298\">)([^<]+)(?:</span>)")

# Realistic desktop User-Agent pool. Rotated per request to make traffic
# blend in with normal browsers and to avoid trivial fingerprinting.
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

ACCEPT_LANGUAGES: tuple[str, ...] = (
    "ru,en;q=0.9",
    "ru-RU,ru;q=0.9,en;q=0.7",
    "ru,en-US;q=0.8,en;q=0.6",
)

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
