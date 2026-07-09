# Публичный API bankiru-reviews

Инструкция для внешних клиентов, которые обращаются к REST API по адресу
**`https://bankiru.uva-advanced.ru`**, без веб-интерфейса Gradio.

Веб-UI на том же хосте защищён Authentik (OIDC) и **не требует** заголовка
`API-Token`: после входа UI ходит во внутренний сервис `api` по Docker-сети
(там по умолчанию включается LLM-суммаризация и всегда передаётся
`outputFormat`). Эта инструкция описывает **прямой HTTP-доступ** с интернета.

Примеры ниже даны в двух вариантах: **curl** и **HTTPie** (`https`).

---

## Базовый URL

```
https://bankiru.uva-advanced.ru
```

Nginx на хосте завершает TLS и проксирует API-пути на сервис `api`
(`127.0.0.1:1706`). Остальные пути (`/`, `/login`, `/gradio/`, …) обслуживает UI.

Интерактивная схема OpenAPI (Swagger):  
[https://bankiru.uva-advanced.ru/docs](https://bankiru.uva-advanced.ru/docs)

---

## Аутентификация

Все защищённые методы используют один и тот же HTTP-заголовок:

```http
API-Token: <ваш_токен>
```

| Тип токена | Откуда берётся | Что можно |
|------------|----------------|-----------|
| **Гостевой** | значение из списка `GUEST_API_TOKEN` (выдаёт администратор) | только `GET /reviews` |
| **Привилегированный** | `API_TOKEN` (секрет Infisical; не раздаётся гостям) | `GET` / `POST` / `DELETE` |

Гостевой токен **не** даёт права на вставку или удаление отзывов: при
`POST`/`DELETE` с гостевым токеном API отвечает **403 Forbidden**.

Если заголовок отсутствует или токен неверный — для запросов через публичный
шлюз к `GET /reviews` ответ **403**.

> Токены в `GUEST_API_TOKEN` перечисляются через запятую и **не должны**
> содержать запятых внутри значения.

```bash
export GUEST_TOKEN='ваш-гостевой-токен'
```

---

## Эндпоинты на поддомене

### `GET /healthz` — проверка доступности

**Auth:** не требуется. Эндпоинт **не** входит в OpenAPI/Swagger
(`include_in_schema=False`); вызывайте его напрямую.

```bash
curl -sS "https://bankiru.uva-advanced.ru/healthz"
# {"status":"ok"}
```

```bash
https -b bankiru.uva-advanced.ru/healthz
```

---

### `GET /docs`, `GET /redoc`, `GET /openapi.json`

**Auth:** не требуется для просмотра схемы.

Используйте пути **без** завершающего слэша (`/docs`, не `/docs/`).
В Swagger UI («Authorize») укажите гостевой или привилегированный токен,
чтобы вызывать `GET /reviews` из браузера.

---

### `GET /reviews` — выборка, inline или файл, опциональная суммаризация

**Auth:** обязателен заголовок `API-Token` (гостевой или привилегированный).

Основной метод для получения отзывов из PostgreSQL. Сервис:

1. Применяет фильтры к таблице `bankiru.reviews`.
2. При необходимости ранжирует результаты семантическим поиском (pgvector).
3. Если **`outputFormat` не передан** — возвращает отзывы **inline** в поле
   `reviews` (без выгрузки в S3; `url` / `filename` = `null`).
4. Если **`outputFormat` задан** — выгружает файл в объектное хранилище и
   возвращает pre-signed `url` (`reviews` = `null`).
5. Если эффективный **`summarize=true`** — строит LLM-сводку в `comment`.

**Breaking change:** раньше отсутствие `outputFormat` означало экспорт в
Parquet. Теперь отсутствие параметра означает **inline JSON** в `reviews`.

**Дефолт `summarize` на публичном URL:** если параметр не передан —
**`false`** (LLM не вызывается). Чтобы получить сводку, укажите
`summarize=true`. (Во внутреннем UI, без маркера шлюза, дефолт наоборот —
`true`.)

В ответе поле `summarize` всегда содержит **эффективное** значение
(после подстановки дефолта).

Запрос без фильтров теоретически вернёт **все** отзывы — для inline это
может быть очень большой JSON. Всегда задавайте разумный диапазон дат
и/или другие фильтры.

#### Параметры запроса (все необязательны)

| Параметр | Тип | Описание |
|----------|-----|----------|
| `startDate` | `YYYY-MM-DD` или `YYYYMMDD` | Отзывы с этой даты включительно. |
| `endDate` | `YYYY-MM-DD` или `YYYYMMDD` | Отзывы по эту дату включительно. |
| `bankName` | строка, повторяемый | Точное совпадение названия банка. |
| `product` | строка, повторяемый | Точное совпадение продукта (см. [справочник](#справочник-product)). |
| `location` | строка, повторяемый | Префикс города: `Москва` совпадёт с `Москва, район Хамовники`. |
| `keywords` | строка | Семантический поиск (эмбеддинг + HNSW). Лимит по умолчанию **200**. |
| `outputFormat` | `csv` \| `json` \| `parquet` \| `xlsx` | Если **опущен** — inline `reviews`. Если задан — файл в S3 и `url`. |
| `summarize` | `true` / `false` | На публичном API по умолчанию **`false`**. Явно `true` — LLM-сводка в `comment`. |
| `cloudModel` | строка | Модель суммаризации; учитывается только при `summarize=true`. |

#### Поля отзыва (`reviews[]` или колонки файла)

| Поле | Смысл |
|------|--------|
| `id` | Внутренний идентификатор строки |
| `datePublished` | Дата/время публикации (`YYYY-MM-DD HH:MM:SS`) |
| `reviewBody` | Текст отзыва |
| `bankName` | Название банка |
| `url` | URL страницы отзыва |
| `location` | Город автора (может быть пустой строкой) |
| `product` | Продуктовая категория |

#### Режимы ответа

| Условие | `reviews` | `url` / `filename` | `comment` |
|---------|-----------|--------------------|-----------|
| Нет `outputFormat` | список объектов | `null` | сводка только при `summarize=true`, иначе `null` |
| Есть `outputFormat` | `null` | pre-signed URL + key | как выше |
| Нет совпадений | `null` | `null` | `Your search did not match any reviews` |
| Ошибка эмбеддинга `keywords` | `null` | `null` | `Semantic search unavailable: …` (HTTP 200, не 500) |

#### Пример ответа: inline (без `outputFormat`, дефолт гостя)

```json
{
  "startDate": "2026-06-01",
  "endDate": "2026-06-07",
  "bankName": null,
  "product": null,
  "location": null,
  "keywords": null,
  "outputFormat": null,
  "summarize": false,
  "cloudModel": null,
  "filename": null,
  "url": null,
  "comment": null,
  "reviews": [
    {
      "id": 42,
      "datePublished": "2026-06-03 14:32:00",
      "reviewBody": "…",
      "bankName": "Сбербанк",
      "url": "https://www.banki.ru/services/responses/bank/response/123456/",
      "location": "Москва",
      "product": "Кредитная карта"
    }
  ]
}
```

#### Пример ответа: файл + сводка

```json
{
  "startDate": "2026-06-01",
  "endDate": "2026-06-07",
  "outputFormat": "xlsx",
  "summarize": true,
  "cloudModel": "anthropic/claude-sonnet-4.6",
  "filename": "a1b2c3d4-e5f6-7890-abcd-ef1234567890.xlsx",
  "url": "https://obs.example/…?X-Amz-Expires=3600…",
  "comment": "**Summary model:** `anthropic/claude-sonnet-4.6`\n\n## Наиболее острые темы…",
  "reviews": null
}
```

При экспорте и/или `summarize=true` запрос может идти **долго** (минуты).
Имеет смысл увеличить таймаут (`curl --max-time 600`, HTTPie `--timeout 600`).

#### Примеры

**1. Типичный гостевой запрос** — inline, без LLM (параметры `outputFormat` и
`summarize` не нужны):

```bash
curl -sS --max-time 120 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-06-01" \
  --data-urlencode "endDate=2026-06-07"
```

```bash
https --timeout 120 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-06-01 \
  endDate==2026-06-07
```

**2. Inline + LLM-сводка:**

```bash
curl -sS --max-time 600 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-06-01" \
  --data-urlencode "endDate=2026-06-07" \
  --data-urlencode "summarize=true"
```

```bash
https --timeout 600 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-06-01 \
  endDate==2026-06-07 \
  summarize==true
```

**3. Файл JSON без сводки** (явный `outputFormat`, дефолт `summarize=false`):

```bash
curl -sS --max-time 600 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-06-01" \
  --data-urlencode "endDate=2026-06-07" \
  --data-urlencode "bankName=Сбербанк" \
  --data-urlencode "outputFormat=json"
```

```bash
https --timeout 600 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-06-01 \
  endDate==2026-06-07 \
  bankName=='Сбербанк' \
  outputFormat==json
```

**4. Файл XLSX + сводка:**

```bash
curl -sS --max-time 600 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-05-01" \
  --data-urlencode "endDate=2026-05-31" \
  --data-urlencode "bankName=Сбербанк" \
  --data-urlencode "bankName=ВТБ" \
  --data-urlencode "product=Кредитная карта" \
  --data-urlencode "outputFormat=xlsx" \
  --data-urlencode "summarize=true"
```

```bash
https --timeout 600 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-05-01 \
  endDate==2026-05-31 \
  bankName=='Сбербанк' \
  bankName=='ВТБ' \
  product=='Кредитная карта' \
  outputFormat==xlsx \
  summarize==true
```

**5. Семантический поиск + префикс города, CSV без сводки:**

```bash
curl -sS --max-time 600 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-01-01" \
  --data-urlencode "endDate=2026-06-30" \
  --data-urlencode "location=Москва" \
  --data-urlencode "keywords=долгое ожидание в отделении" \
  --data-urlencode "outputFormat=csv"
```

```bash
https --timeout 600 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-01-01 \
  endDate==2026-06-30 \
  location=='Москва' \
  keywords=='долгое ожидание в отделении' \
  outputFormat==csv
```

**6. Скачать файл по ссылке из ответа** (`url`):

```bash
DOWNLOAD_URL=$(curl -sS --max-time 600 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-06-01" \
  --data-urlencode "endDate=2026-06-02" \
  --data-urlencode "outputFormat=parquet" \
  | jq -r '.url')

curl -sS -L -o reviews.parquet "$DOWNLOAD_URL"
```

```bash
DOWNLOAD_URL=$(https --timeout 600 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-06-01 \
  endDate==2026-06-02 \
  outputFormat==parquet \
  | jq -r '.url')

curl -sS -L -o reviews.parquet "$DOWNLOAD_URL"
# (pre-signed OBS URL удобнее скачивать curl; HTTPie выше только для получения JSON)
```

---

### Запись и удаление (`POST` / `DELETE`)

Пути `/reviews` и `/reviews/…` тоже проксируются на API, но для изменения
данных нужен **привилегированный** `API_TOKEN`. Гостевой токен получит **403**.

| Метод | Назначение | Auth |
|-------|------------|------|
| `POST /reviews` | Вставка пакета отзывов (использует парсер) | только `API_TOKEN` |
| `DELETE /reviews` | Удаление по списку `id` | только `API_TOKEN` |
| `DELETE /reviews/by-date` | Удаление по диапазону дат | только `API_TOKEN` |
| `DELETE /reviews/duplicates` | Дедупликация таблицы | только `API_TOKEN` |

Гостям эти операции **недоступны** и в обычной работе не нужны.

---

## Справочник `product`

В фильтре `product` указывайте **человекочитаемую** метку (как в базе), а не
slug banki.ru:

| Значение `product` |
|--------------------|
| Автокредит |
| Вклад |
| Дебетовая карта |
| Денежный перевод |
| Дистанционное обслуживание физических лиц |
| Ипотека |
| Кредитная карта |
| Мобильное приложение |
| Обслуживание физических лиц |
| Потребительский кредит |
| Реструктуризация/рефинансирование |
| Другое (физические лица) |
| Банковская гарантия |
| Депозит |
| Дистанционное обслуживание юридических лиц |
| Зарплатный проект |
| Кредитование бизнеса |
| Лизинг |
| Мобильное приложение для бизнеса |
| Обслуживание юридических лиц |
| Расчетно-кассовое обслуживание |
| Эквайринг |
| Другое (юридические лица) |

---

## Ограничения и советы

- На публичном API **`summarize` по умолчанию выключен**; для сводки —
  `summarize=true`.
- Без **`outputFormat`** ответ — inline `reviews` (может быть очень большим).
- С **`outputFormat`** ссылка на файл живёт около **часа**.
- **`keywords`** ограничивает выдачу (по умолчанию до 200 отзывов). Если
  эмбеддинг запроса не удался, в `comment` будет
  `Semantic search unavailable: …` (без HTTP 500) — повторите без `keywords`
  или позже.
- Не путайте REST с Gradio: UI на `/gradio/` требует Authentik.
- Корень `https://bankiru.uva-advanced.ru/` — UI (логин); документация API — `/docs`.

---

## Шпаргалка

### curl

```bash
export GUEST_TOKEN='…'

# Проверка доступности
curl -sS "https://bankiru.uva-advanced.ru/healthz"

# Inline за неделю (без LLM) — типичный гостевой вызов
curl -sS --max-time 120 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-06-01" \
  --data-urlencode "endDate=2026-06-07"

# Файл JSON без сводки
curl -sS --max-time 600 \
  -H "API-Token: $GUEST_TOKEN" \
  -G "https://bankiru.uva-advanced.ru/reviews" \
  --data-urlencode "startDate=2026-06-01" \
  --data-urlencode "endDate=2026-06-07" \
  --data-urlencode "outputFormat=json"

# Без токена — ожидается 403
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://bankiru.uva-advanced.ru/reviews?startDate=2026-06-01&endDate=2026-06-02"
```

### HTTPie

```bash
export GUEST_TOKEN='…'

# Проверка доступности
https -b bankiru.uva-advanced.ru/healthz

https --timeout 120 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-06-01 \
  endDate==2026-06-07

https --timeout 600 -b \
  bankiru.uva-advanced.ru/reviews \
  "API-Token:$GUEST_TOKEN" \
  startDate==2026-06-01 \
  endDate==2026-06-07 \
  outputFormat==json

https -h bankiru.uva-advanced.ru/reviews \
  startDate==2026-06-01 endDate==2026-06-02 \
  | head -n1
# ожидается HTTP/1.1 403 …
```
