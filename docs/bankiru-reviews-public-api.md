# Публичный API bankiru-reviews

Инструкция для внешних клиентов, которые обращаются к REST API по адресу
**`https://bankiru.uva-advanced.ru`**, без веб-интерфейса Gradio.

Веб-UI на том же хосте защищён Authentik (OIDC) и **не требует** заголовка
`API-Token`: после входа UI ходит во внутренний сервис `api` по Docker-сети.
Format по умолчанию — `parquet`; если выпадающий список Format очистить,
`outputFormat` не уходит и ответ приходит inline. `summarize` UI шлёт явно
(по умолчанию в интерфейсе `<no summary>` → `false`). API при отсутствии
`summarize` всегда считает его **`false`**. Эта инструкция описывает
**прямой HTTP-доступ** с интернета.

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
| **Гостевой** | секрет из пары `owner@example.org:token` в `GUEST_API_TOKEN` (выдаёт администратор) | только `GET /reviews` |
| **Привилегированный** | `API_TOKEN` (секрет Infisical; не раздаётся гостям) | `GET` / `POST` / `DELETE` |

Гостевой токен **не** даёт права на вставку или удаление отзывов: при
`POST`/`DELETE` с гостевым токеном API отвечает **403 Forbidden**.

Если заголовок отсутствует или токен неверный — для запросов через публичный
шлюз к `GET /reviews` ответ **403**.

> В `GUEST_API_TOKEN` пары `owner@example.org:token` перечисляются через
> запятую. В заголовке `API-Token` клиент шлёт **только секрет**, не email.
> Токен **не должен** содержать запятых.

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
5. Если **`summarize=true`** — строит LLM-сводку в `comment`.

**Breaking change:** раньше отсутствие `outputFormat` означало экспорт в
Parquet. Теперь отсутствие параметра означает **inline JSON** в `reviews`.

**Дефолт `summarize`:** если параметр не передан — всегда **`false`**
(публичный шлюз, localhost, Gradio). Чтобы получить сводку, укажите
`summarize=true`. В ответе поле `summarize` повторяет принятое значение.

**Неизвестные query-параметры** (например `date_from`, `limit`) →
**422** (`extra_forbidden`).

**Пустые `startDate` / `endDate`:** опущенная граница всегда подставляется
из имеющихся данных — пустой `startDate` = **самая ранняя** `datePublished`
в таблице, пустой `endDate` = **самая поздняя**. Правило действует при любом
значении `summarize` и одинаково для **публичного Nginx**, **localhost
(мимо шлюза)** и **Gradio** (UI ходит во внутренний `api`). Границы берутся
по всей таблице, без учёта `bankName` / `product` / `location` / `keywords`,
и один раз питают SQL-фильтр, лимит суммаризации и поля-эхо ответа. Текущая
дата в расчёте не участвует: интервал описывает данные, а не календарь.

**Breaking change:** в JSON-ответе **200** поля `startDate` / `endDate`
содержат **эффективные** даты и при непустой таблице больше не бывают
`null`. На пустой таблице неразрешённая граница остаётся `null` (обе
опущены → обе `null`; одна задана → она отражается в ответе, вторая —
`null`). Кроме того, перевёрнутый диапазон теперь **400**, а не 200 с
пустым результатом.

**Перевёрнутый диапазон** (`endDate` раньше `startDate` — в том числе после
подстановки, например `startDate=2030-01-01` при пустом `endDate`) →
**400**, независимо от `summarize`:

```json
{
  "detail": "Empty date range: endDate is earlier than startDate (an omitted bound falls back to the earliest / latest review date stored in the database)."
}
```

**Лимит суммаризации по датам:** при `summarize=true` эффективный интервал
не длиннее **трёх календарных месяцев** (**400** иначе) — проверка идёт до
основной выборки отзывов и до LLM.

Примеры: `2026-01-01`…`2026-04-01` допустимо; `2026-01-01`…`2026-04-02` —
нет. Если граница опущена, интервал считается уже после подстановки:
`startDate=2024-04-01` без `endDate` пройдёт, только если самый поздний
отзыв в базе не позже `2024-07-01`; **оба параметра опущены** при
`summarize=true` — почти всегда нет (весь период данных ≫ 3 месяцев).

Тело ответа **400** (поле `detail` — строка; тот же текст показывает Gradio
как **error toast**, не как текст в панели Summary):

```json
{
  "detail": "Summarization is only allowed for date ranges of at most three calendar months. Narrow startDate/endDate (an omitted bound falls back to the earliest / latest review date stored in the database), or set summarize=false."
}
```

**Отказ семантического поиска:** если задан `keywords`, но получить эмбеддинг
запроса не удалось — провайдер недоступен, вернул ошибку или ответил без
вектора, — запрос завершается **503**: поиск не выполнялся.

```json
{
  "detail": "Semantic search is temporarily unavailable: the query could not be embedded. Retry later, or repeat the request without keywords."
}
```

**Breaking change:** раньше этот случай возвращал **200** с пояснением в
`comment` и пустым результатом. Такой ответ невозможно отличить от «по вашему
запросу ничего не найдено», и клиент мог записать «жалоб по теме нет» для
поиска, который не состоялся. Повторите запрос позже или без `keywords` —
остальные фильтры работают независимо от провайдера эмбеддингов.

Запрос без фильтров теоретически вернёт **все** отзывы — для inline это
может быть очень большой JSON. Всегда задавайте разумный диапазон дат
и/или другие фильтры.

#### Параметры запроса (все необязательны)

| Параметр | Тип | Описание |
|----------|-----|----------|
| `startDate` | `YYYY-MM-DD` или `YYYYMMDD` | Отзывы с этой даты включительно. Если опущен — самая ранняя `datePublished` в таблице (при любом `summarize`). |
| `endDate` | `YYYY-MM-DD` или `YYYYMMDD` | Отзывы по эту дату включительно. Если опущен — самая поздняя `datePublished` в таблице (при любом `summarize`). |
| `bankName` | строка, повторяемый | Точное совпадение названия банка. |
| `product` | строка, повторяемый | Точное совпадение продукта (см. [справочник](#справочник-product)). |
| `location` | строка, повторяемый | Префикс города: `Москва` совпадёт с `Москва, район Хамовники`. |
| `keywords` | строка | Семантический поиск (эмбеддинг + HNSW). Лимит по умолчанию **200**; слишком далёкие по смыслу отзывы отсекаются, а отзывы без посчитанного эмбеддинга в такой поиск не попадают. Строка из одних пробелов равносильна отсутствию параметра. Если эмбеддинг запроса получить не удалось — **503** (см. ниже). |
| `outputFormat` | `csv` \| `json` \| `parquet` \| `xlsx` | Если **опущен** — inline `reviews`. Если задан — файл в S3 и `url`. |
| `summarize` | `true` / `false` | По умолчанию **`false`** (любой клиент). Явно `true` — LLM-сводка в `comment`; эффективный интервал дат ≤ 3 календарных месяцев. |
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

#### Ошибки (`GET /reviews`)

| HTTP | Когда | Тело |
|------|--------|------|
| **400** | эффективный `endDate` раньше эффективного `startDate` (при любом `summarize`) | `{"detail":"Empty date range: endDate is earlier than startDate …"}` (полный текст — выше) |
| **400** | `summarize=true` и эффективный интервал дат &gt; 3 календарных месяцев (в т.ч. если `startDate` / `endDate` не заданы) | `{"detail":"Summarization is only allowed for date ranges of at most three calendar months. …"}` (полный текст — выше) |
| **403** | через публичный шлюз без `API-Token` или с неверным токеном | стандартное тело FastAPI |
| **422** | неизвестный query-параметр (`extra_forbidden`) или невалидный тип | стандартный validation error FastAPI |
| **500** | задан `outputFormat`, но объектное хранилище отказало при выгрузке или выдаче ссылки | стандартное тело FastAPI; ссылки на файл в ответе не будет |
| **503** | задан `keywords`, но эмбеддинг запроса не получен — поиск **не выполнялся** | `{"detail":"Semantic search is temporarily unavailable: …"}` (полный текст — выше) |

#### Пример ответа: inline (без `outputFormat`, дефолт `summarize=false`)

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

Поля `startDate` / `endDate` — эффективные границы. Если запрос их не
содержал, здесь будут подставленные даты первого и последнего отзыва в базе,
то есть по ответу всегда видно, за какой период он построен.

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

## Что вернётся на такой запрос

Сводка исходов для запросов через публичный шлюз: что стоит в запросе, какой
придёт код и что окажется в теле. Каждая строка закреплена автотестом, поэтому
изменить исход, не заметив этого, нельзя.

### Токен

| В запросе | HTTP | Тело |
|-----------|------|------|
| Гостевой или привилегированный `API-Token` на `GET /reviews` | **200** | обычный ответ |
| На `GET /reviews` заголовка `API-Token` нет или токен пустой / неверный | **403** | стандартное тело FastAPI |
| Гостевой токен на `POST` / `DELETE` | **403** | гостям запись недоступна |
| На `POST` / `DELETE` заголовка нет или он пустой | **401** | «не аутентифицирован» — до проверки значения |
| На `POST` / `DELETE` токен есть, но неверный (в т.ч. гостевой) | **403** | отличается от отсутствующего / пустого |

Подделать «внутренний» вызов нельзя: заголовок `X-Bankiru-Gateway` Nginx
выставляет сам, перекрывая присланный клиентом.

### Параметры

| В запросе | HTTP | Тело |
|-----------|------|------|
| Неизвестный параметр (`limit`, `date_from`, опечатка) | **422** | `extra_forbidden` с именем параметра; перечислены все неизвестные |
| `startDate` / `endDate` как `2026-03-01` или `20260301` | **200** | обе записи означают одну дату |
| Пустая строка в дате | **200** | считается «не задано» и подставляется из данных |
| `2026-13-45`, `01-03-2026`, `not-a-date`, дата со временем | **422** | ошибка валидации |
| `outputFormat` = `csv`, `json`, `parquet`, `xlsx` | **200** | файл соответствующего формата |
| `outputFormat` = что-то ещё, в том числе `CSV` | **422** | список допустимых значений |
| `summarize` = `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` | **200** | значение отражается в ответе |
| `summarize` = `maybe`, `2` | **422** | не булево |
| `summarize` не передан | **200** | считается `false` |
| `cloudModel` без `summarize=true` | **200** | отражается в ответе, но сводки не будет |
| `bankName` / `product` / `location` повторены | **200** | учитываются все значения |

### Даты

| В запросе | HTTP | Тело |
|-----------|------|------|
| Обе границы заданы | **200** | ровно этот диапазон, включительно с обеих сторон |
| `startDate` опущен | **200** | подставляется самый ранний отзыв в базе; он же в ответе |
| `endDate` опущен | **200** | подставляется самый поздний отзыв; текущая дата не участвует |
| Обе опущены | **200** | весь период имеющихся данных |
| Эффективный `endDate` раньше `startDate` | **400** | `Empty date range: …` — отзывы не читаются |
| То же плюс `summarize=true` | **400** | сообщение о перевёрнутом диапазоне, а не о длине |
| `summarize=true`, интервал ровно 3 календарных месяца | **200** | сводка |
| `summarize=true`, интервал длиннее | **400** | `Summarization is only allowed …` — до выборки и до LLM |
| `summarize=true` без дат, в таблице есть данные | **400** | весь период данных почти всегда длиннее трёх месяцев |
| Таблица пуста, даты опущены (в т.ч. с `summarize=true`) | **200** | `Your search did not match any reviews`; обе даты в ответе — `null` (проверки длины нет — границ не существует) |
| Таблица пуста, обе даты заданы | **200** | «no results», даты отражены как в запросе; при `summarize=true` и интервале &gt; 3 месяцев — всё же **400** |

Три календарных месяца считаются календарно, а не как 90 дней: при переходе в
более короткий месяц дата прижимается к его последнему числу — 30 ноября плюс
три месяца даёт 28 февраля (29 в високосный год), поэтому предельный интервал
в таких случаях на день-два короче ожидаемого.

### Результат

| В запросе | HTTP | Тело |
|-----------|------|------|
| `outputFormat` не задан, отзывы нашлись | **200** | `reviews` списком, `url` и `filename` — `null` |
| `outputFormat` задан, отзывы нашлись | **200** | `url` (около часа) и `filename`, `reviews` — `null` |
| Ничего не найдено | **200** | `comment` = `Your search did not match any reviews`, ссылки нет — файл не создавался |
| `summarize=true` и отзывы нашлись | **200** | `comment` начинается с `**Summary model:**` и названия модели |
| `summarize=true`, но отзывов нет | **200** | сводки нет, LLM не вызывается |
| `summarize=true` и `outputFormat` | **200** | и сводка, и ссылка на файл |
| `keywords` задан, эмбеддинг получен | **200** | до 200 отзывов, ранжированных по близости; слишком далёкие по смыслу отсекаются |
| `keywords` задан, эмбеддинг получить не удалось | **503** | `Semantic search is temporarily unavailable: …`; пустого **200** не будет |
| `keywords` из одних пробелов | **200** | считается незаданным — обычная выборка без ранжирования |
| `outputFormat` задан, но хранилище отказало | **500** | ссылки не будет; успешного ответа с недоступным файлом не бывает |

Любой ответ **200** повторяет принятые параметры, включая подставленные
границы дат, поэтому по одному телу ответа видно, какая выборка получена.

---

## Ограничения и советы

- **`summarize` по умолчанию выключен** (omit → `false` везде); для сводки —
  `summarize=true`.
- **Пустая граница дат = граница данных** при любом `summarize`: пустой
  `startDate` — самый ранний отзыв в базе, пустой `endDate` — самый поздний.
  Эти же даты возвращаются в ответе. Перевёрнутый эффективный диапазон —
  **400** при любом `summarize`.
- При `summarize=true` эффективный интервал ≤ **3 календарных месяца** (оба
  параметра опущены — тоже считается, если в таблице есть данные). Иначе
  **400** с фиксированным `detail` (см. выше). Пустая таблица при опущенных
  датах — **200** «no results»: границ нет, проверка длины не запускается.
  В Gradio любой такой **400** — это **error toast** с тем же текстом;
  URL/Summary очищаются; info «Download your file» — только после успешного
  Submit с URL экспорта.
- Неизвестные query-параметры → **422**.
- Без **`outputFormat`** ответ — inline `reviews` (может быть очень большим).
- С **`outputFormat`** ссылка на файл живёт около **часа**.
- **`keywords`** ограничивает выдачу (по умолчанию до 200 отзывов). Если
  эмбеддинг запроса не удался, ответ — **503** с фиксированным `detail`
  (см. выше), а не пустой **200**: поиск не выполнялся, и пустой результат
  ввёл бы в заблуждение. Повторите позже или без `keywords`.
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
