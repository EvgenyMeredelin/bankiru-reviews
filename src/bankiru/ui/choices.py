"""Static dropdown choices for the Gradio UI: locations, banks, products, file formats.

This module contains hardcoded lists used to populate the multi-select dropdown
components in the Gradio UI (blocks.py). The lists are static because:
  - Locations: administrative capitals of Russian federal subjects (stable)
  - Banks: top-50 banks by complaint volume in 2025 (updated annually)
  - Products: banking product categories matching the parser's PRODUCTS dict
  - File formats: supported export formats matching the API's handler classes

These lists are intentionally separate from the parser's PRODUCTS dict
(settings.py) because:
  - The parser uses URL slugs as keys; the UI needs human-readable labels
  - The UI may show a subset of products or banks
  - The location list is UI-only (the parser doesn't filter by location)

Connection to other modules:
  - bankiru.ui.blocks — imports BANKS, PRODUCTS, LOCATIONS, FILE_FORMATS
                        for the Gradio dropdown components
"""

# ── Locations ────────────────────────────────────────────────────────────────
# Administrative capitals of Russian federal subjects (субъекты РФ).
# Used for the Location filter dropdown. The API matches these as prefixes
# (startswith), so "Москва" matches "Москва", "Москва, район Хамовники", etc.
# Sorted alphabetically in Russian.
LOCATIONS = [
    "Абакан",
    "Анадырь",
    "Архангельск",
    "Астрахань",
    "Барнаул",
    "Белгород",
    "Биробиджан",
    "Благовещенск",
    "Брянск",
    "Великий Новгород",
    "Владивосток",
    "Владикавказ",
    "Владимир",
    "Волгоград",
    "Вологда",
    "Воронеж",
    "Гатчина",
    "Горно-Алтайск",
    "Грозный",
    "Донецк",
    "Екатеринбург",
    "Иваново",
    "Ижевск",
    "Иркутск",
    "Йошкар-Ола",
    "Казань",
    "Калининград",
    "Калуга",
    "Кемерово",
    "Киров",
    "Кострома",
    "Краснодар",
    "Красноярск",
    "Курган",
    "Курск",
    "Кызыл",
    "Липецк",
    "Луганск",
    "Магадан",
    "Магас",
    "Майкоп",
    "Махачкала",
    "Мелитополь",
    "Москва",
    "Мурманск",
    "Нальчик",
    "Нарьян-Мар",
    "Нижний Новгород",
    "Новосибирск",
    "Омск",
    "Орел",
    "Оренбург",
    "Пенза",
    "Пермь",
    "Петрозаводск",
    "Петропавловск-Камчатский",
    "Псков",
    "Ростов-на-Дону",
    "Рязань",
    "Салехард",
    "Самара",
    "Санкт-Петербург",
    "Саранск",
    "Саратов",
    "Севастополь",
    "Симферополь",
    "Смоленск",
    "Ставрополь",
    "Сыктывкар",
    "Тамбов",
    "Тверь",
    "Томск",
    "Тула",
    "Тюмень",
    "Улан-Удэ",
    "Ульяновск",
    "Уфа",
    "Хабаровск",
    "Ханты-Мансийск",
    "Херсон",
    "Чебоксары",
    "Челябинск",
    "Черкесск",
    "Чита",
    "Элиста",
    "Южно-Сахалинск",
    "Якутск",
    "Ярославль"
]

# ── Banks ────────────────────────────────────────────────────────────────────
# Top-50 Russian banks by complaint volume in 2025, sorted alphabetically.
# Used for the Bank filter dropdown. The API matches these exactly (IN clause),
# so the names must match the bankName values stored in the database (which
# come from banki.ru's JSON-LD structured data).
BANKS = [
    "Ozon Банк",
    "Абсолют Банк",
    "Авто Финанс Банк",
    "Азиатско-Тихоокеанский банк (АТБ)",
    "Ак Барс Банк",
    "Альфа-Банк",
    "БКС Банк",
    "Банк «Левобережный»",
    "Банк «Санкт-Петербург»",
    "Банк ДОМ.РФ",
    "Банк ЗЕНИТ",
    "Банк Синара",
    "Банк ТКБ",
    "ВБРР",
    "ВТБ",
    "Вайлдберриз Банк",
    "Газпромбанк",
    "Драйв Клик Банк",
    "Инго Банк",
    "КАМКОМБАНК",
    "Кредит Европа Банк",
    "Локо-Банк",
    "МТС Банк",
    "МТС Деньги (ЭКСИ-Банк)",
    "Модульбанк",
    "Московский кредитный банк (МКБ)",
    "НОВИКОМ",
    "ОТП Банк",
    "ПСБ",
    "Почта Банк",
    "Примсоцбанк",
    "РНКБ",
    "Райффайзен Банк",
    "Ренессанс Банк",
    "Россельхозбанк",
    "Русский Стандарт",
    "Сбербанк",
    "Свой Банк",
    "Совкомбанк",
    "Солид Банк",
    "Т-Банк",
    "Точка",
    "Уралсиб",
    "Уральский банк реконструкции и развития (УБРиР)",
    "Фора-Банк",
    "Цифра банк",
    "ЮMoney",
    "ЮниКредит Банк",
    "Юнистрим",
    "Яндекс Банк"
]

# ── Products ─────────────────────────────────────────────────────────────────
# Banking product categories. These labels must match the values in the
# parser's PRODUCTS dict (settings.py) because they are stored as-is in the
# database and matched exactly by the API's product filter (IN clause).
# Split into retail (individuals) and business (legal entities) sections.
PRODUCTS = [
    # ── Retail banking products (for individuals) ────────────────────
    "Автокредит",
    "Вклад",
    "Дебетовая карта",
    "Денежный перевод",
    "Дистанционное обслуживание физических лиц",
    "Ипотека",
    "Кредитная карта",
    "Мобильное приложение",
    "Обслуживание физических лиц",
    "Потребительский кредит",
    "Реструктуризация/рефинансирование",
    "Другое (физические лица)",

    # ── Business banking products (for legal entities) ───────────────
    "Банковская гарантия",
    "Депозит",
    "Дистанционное обслуживание юридических лиц",
    "Зарплатный проект",
    "Кредитование бизнеса",
    "Лизинг",
    "Мобильное приложение для бизнеса",
    "Обслуживание юридических лиц",
    "Расчетно-кассовое обслуживание",
    "Эквайринг",
    "Другое (юридические лица)"
]

# ── File formats ─────────────────────────────────────────────────────────────
# Supported export formats. These must match the `extension` attribute of the
# handler classes in bankiru.api.handlers (CSVMaker, JSONMaker, etc.).
# The API's schemas.py auto-discovers available formats from the handlers module,
# so this list should stay in sync with the handler classes.
FILE_FORMATS = [
    "csv",
    "json",
    "parquet",
    "xlsx"
]
